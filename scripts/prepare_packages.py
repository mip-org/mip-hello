#!/usr/bin/env python3
"""
Prepare MATLAB packages from recipe.yaml + mip.yaml specifications.

This script reads recipe.yaml files from packages/ and:
1. Downloads/clones source code based on recipe.yaml
2. Overlays channel-provided files (mip.yaml, compile.m, etc.) on top
3. Reads mip.yaml from the merged result for package metadata
4. Computes all paths (including recursive paths with exclusions)
5. Collects exposed symbols
6. Creates load_package.m and unload_package.m scripts
7. Generates mip.json metadata
"""

import os
import sys
import json
from channel_config import get_base_url, release_tag_from_mhl
import shutil
import subprocess
import time
import requests
import zipfile
import yaml
import hashlib
from datetime import datetime
from typing import List, Dict, Any, Optional

import argparse


def download_and_extract_zip(url: str, destination: str):
    """
    Download a ZIP file from a URL and extract it to destination.

    Args:
        url: The URL to download the ZIP file from
        destination: The directory name to extract to
    """
    download_file = "temp_download.zip"

    print(f'  Downloading {url}...')
    response = requests.get(url, timeout=30)
    response.raise_for_status()

    with open(download_file, 'wb') as f:
        f.write(response.content)
    print('  Download complete.')

    print(f"  Extracting to {destination}...")
    with zipfile.ZipFile(download_file, 'r') as zip_ref:
        zip_ref.extractall(destination)

    os.remove(download_file)


def clone_git_repository(url: str, destination: str, subdirectory: str | None = None, branch: str | None = None):
    """
    Clone a git repository and remove .git directories.

    Args:
        url: The URL of the git repository to clone
        destination: The directory name to clone into
        subdirectory: If specified, only copy this subdirectory from the repo
        branch: If specified, clone this branch or tag (passed as --branch to git clone)
    """
    branch_args = ["--branch", branch] if branch else []
    if subdirectory:
        # Clone to a temp directory, then copy only the subdirectory
        temp_clone_dir = destination + "_temp_clone"
        branch_info = f", branch: {branch}" if branch else ""
        print(f'  Cloning {url} (subdirectory: {subdirectory}{branch_info})...')
        subprocess.run(
            ["git", "clone"] + branch_args + [url, temp_clone_dir],
            check=True,
            capture_output=True
        )
        subdir_path = os.path.join(temp_clone_dir, subdirectory)
        if not os.path.isdir(subdir_path):
            shutil.rmtree(temp_clone_dir)
            raise ValueError(f"Subdirectory '{subdirectory}' not found in cloned repository")
        if destination == '.':
            # Copy contents into current directory
            for item in os.listdir(subdir_path):
                s = os.path.join(subdir_path, item)
                d = os.path.join('.', item)
                if os.path.isdir(s):
                    shutil.copytree(s, d)
                else:
                    shutil.copy2(s, d)
        else:
            shutil.copytree(subdir_path, destination)
        shutil.rmtree(temp_clone_dir)
    else:
        branch_info = f" (branch: {branch})" if branch else ""
        print(f'  Cloning {url}{branch_info}...')
        subprocess.run(
            ["git", "clone"] + branch_args + [url, destination],
            check=True,
            capture_output=True
        )

    # Remove .git directories to reduce size
    print("  Removing .git directories...")
    for root, dirs, files in os.walk(destination):
        if ".git" in dirs:
            git_dir = os.path.join(root, ".git")
            shutil.rmtree(git_dir)
            dirs.remove(".git")


def resolve_git_commit_hash(url: str, ref: str) -> str:
    """
    Resolve a branch or tag to its commit hash using git ls-remote.

    Args:
        url: Git repository URL
        ref: Branch or tag name

    Returns:
        The commit hash string

    Raises:
        RuntimeError: If the ref cannot be resolved
    """
    try:
        result = subprocess.run(
            ["git", "ls-remote", url, ref],
            check=True, capture_output=True, text=True
        )
        for line in result.stdout.strip().splitlines():
            commit_hash, remote_ref = line.split('\t', 1)
            if remote_ref in (f"refs/heads/{ref}", f"refs/tags/{ref}", ref):
                return commit_hash
        raise RuntimeError(f"Could not resolve ref '{ref}' for {url}")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"git ls-remote failed for {url} {ref}: {e}") from e


def collect_exposed_symbols(base_dir: str, extensions: List[str]) -> List[str]:
    """
    Collect exposed symbols from a directory.

    Args:
        base_dir: The directory to scan
        extensions: List of file extensions to include (e.g., ['.m', '.c'])

    Returns:
        List of symbol names
    """
    symbols = []

    if not os.path.exists(base_dir):
        return symbols

    items = os.listdir(base_dir)

    for item in sorted(items):
        item_path = os.path.join(base_dir, item)

        if os.path.isfile(item_path):
            # Check if file has one of the specified extensions
            for ext in extensions:
                if item.endswith(ext):
                    # Remove the extension
                    symbols.append(item[:-len(ext)])
                    break
        elif os.path.isdir(item_path) and (item.startswith('+') or item.startswith('@')):
            # Add package or class directory (without + or @)
            symbols.append(item[1:])

    return symbols


def generate_recursive_paths(base_path: str, exclude_dirs: List[str]) -> List[str]:
    """
    Generate a list of all subdirectories recursively, excluding specified directories.

    Args:
        base_path: The base directory to start from
        exclude_dirs: List of directory names to exclude

    Returns:
        List of relative paths
    """
    paths = []

    for root, dirs, files in os.walk(base_path):
        # Remove excluded directories from the search
        dirs[:] = [d for d in dirs if d not in exclude_dirs]

        # Add this directory if it contains .m files
        m_files = [f for f in files if f.endswith('.m')]
        if m_files:
            # Get relative path from base_path parent
            rel_path = os.path.relpath(root, os.path.dirname(base_path))
            paths.append(rel_path)

    return sorted(paths)


def compute_directory_hash(directory: str) -> str:
    """
    Compute a recursive SHA1 hash of a directory's contents.

    This hash is deterministic and based on:
    - All file paths (relative to the directory)
    - All file contents

    Args:
        directory: The directory to hash

    Returns:
        SHA1 hash as a hexadecimal string
    """
    sha1 = hashlib.sha1()

    # Walk directory in sorted order for deterministic results
    for root, dirs, files in os.walk(directory):
        # Sort directories and files for deterministic ordering
        dirs.sort()
        files.sort()

        for filename in files:
            file_path = os.path.join(root, filename)
            relative_path = os.path.relpath(file_path, directory)

            # Hash the relative path
            sha1.update(relative_path.encode('utf-8'))
            sha1.update(b'\0')  # Separator

            # Hash the file contents
            try:
                with open(file_path, 'rb') as f:
                    while True:
                        chunk = f.read(8192)
                        if not chunk:
                            break
                        sha1.update(chunk)
            except (IOError, OSError) as e:
                # If we can't read a file, include the error in the hash
                sha1.update(f"ERROR:{e}".encode('utf-8'))

            sha1.update(b'\0')  # Separator between files

    return sha1.hexdigest()


def create_load_and_unload_scripts(mhl_dir: str, paths: List[str]):
    """
    Create load_package.m and unload_package.m scripts.

    Args:
        mhl_dir: The MHL package directory
        paths: List of paths to add to MATLAB path
    """
    # Create load_package.m
    load_script_path = os.path.join(mhl_dir, 'load_package.m')
    with open(load_script_path, 'w') as f:
        f.write("function load_package()\n")
        f.write("    % Add package directories to MATLAB path\n")
        f.write("    pkg_dir = fileparts(mfilename('fullpath'));\n")
        for path in paths:
            f.write(f"    addpath(fullfile(pkg_dir, '{path}'));\n")
        f.write("end\n")

    # Create unload_package.m
    unload_script_path = os.path.join(mhl_dir, 'unload_package.m')
    with open(unload_script_path, 'w') as f:
        f.write("function unload_package()\n")
        f.write("    % Remove package directories from MATLAB path\n")
        f.write("    pkg_dir = fileparts(mfilename('fullpath'));\n")
        for path in paths:
            f.write(f"    rmpath(fullfile(pkg_dir, '{path}'));\n")
        f.write("end\n")


def get_current_platform_tag() -> str:
    """Get the current platform tag."""
    import platform
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == 'linux':
        if 'x86_64' in machine or 'amd64' in machine:
            return 'linux_x86_64'
        elif 'aarch64' in machine or 'arm64' in machine:
            return 'linux_aarch64'
    elif system == 'darwin':
        if 'arm64' in machine:
            return 'macos_arm64'
        else:
            return 'macos_x86_64'
    elif system == 'windows':
        return 'windows_x86_64'

    return 'any'


def resolve_build_config(mip_yaml: Dict[str, Any], build: Dict[str, Any]) -> Dict[str, Any]:
    """
    Merge a build entry with top-level mip.yaml defaults.

    Top-level keys from mip.yaml (addpaths, symbol_extensions, etc.) serve
    as defaults. Fields in `build` override them.
    The `architectures` key is consumed separately and not merged.
    """
    # Start with top-level fields as defaults
    resolved = {}
    for key in ('addpaths', 'symbol_extensions', 'release_number',
                'compile_script', 'build_script', 'test_script',
                'build_env', 'build_on'):
        if key in mip_yaml:
            resolved[key] = mip_yaml[key]

    # Build-level overrides
    for key, value in build.items():
        if key == 'architectures':
            continue
        resolved[key] = value
    return resolved


def overlay_channel_files(release_folder: str, target_dir: str):
    """
    Copy channel-provided files from the release folder into the target directory.

    Copies everything except recipe.yaml. Files from the channel overlay
    (win over) files from the cloned source.

    Args:
        release_folder: The channel's release directory (packages/<name>/releases/<ver>/)
        target_dir: The working directory where source was cloned
    """
    for item in os.listdir(release_folder):
        if item == 'recipe.yaml':
            continue
        src = os.path.join(release_folder, item)
        dst = os.path.join(target_dir, item)
        if os.path.isdir(src):
            if os.path.exists(dst):
                # Merge directory contents (channel files win)
                for root, dirs, files in os.walk(src):
                    rel_root = os.path.relpath(root, src)
                    dst_root = os.path.join(dst, rel_root)
                    os.makedirs(dst_root, exist_ok=True)
                    for f in files:
                        shutil.copy2(os.path.join(root, f), os.path.join(dst_root, f))
            else:
                shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)


class PackagePreparer:
    """Handles preparing MATLAB packages from recipe.yaml + mip.yaml specifications."""

    def __init__(self, dry_run=False, force=False, output_dir=None):
        self.dry_run = dry_run
        self.force = force

        if output_dir:
            self.output_dir = output_dir
        else:
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            self.output_dir = os.path.join(project_root, 'build', 'prepared')

        if not self.dry_run:
            os.makedirs(self.output_dir, exist_ok=True)

    def _get_mhl_filename(self, mip_yaml: Dict[str, Any], architecture: str) -> str:
        """Generate the .mhl filename for a package build."""
        return (
            f"{mip_yaml['name']}-{mip_yaml['version']}-"
            f"{architecture}.mhl"
        )

    def _check_existing_package(self, mhl_filename: str, mip_yaml: Dict[str, Any],
                                resolved_config: Dict[str, Any],
                                source_hash: str) -> bool:
        """Check if package exists in bucket with matching metadata and source hash."""
        pkg_name = release_tag_from_mhl(mhl_filename)
        base_url = get_base_url(pkg_name)
        mip_json_url = f"{base_url}/{mhl_filename}.mip.json"

        try:
            response = requests.get(mip_json_url, timeout=10)
            if response.status_code == 404:
                print(f"  Package not found in bucket")
                return False

            response.raise_for_status()
            existing_metadata = response.json()

            # First check source_hash - this is the most important check
            existing_source_hash = existing_metadata.get('source_hash')
            if existing_source_hash != source_hash:
                print(f"  Source hash mismatch (existing: {existing_source_hash}, new: {source_hash})")
                return False

            # Compare key metadata fields
            identity_fields = [
                'name', 'description', 'version',
                'dependencies', 'homepage', 'repository', 'license'
            ]
            for field in identity_fields:
                if existing_metadata.get(field) != mip_yaml.get(field):
                    print(f"  Metadata mismatch in field '{field}'")
                    return False

            # Compare release_number from resolved config
            if existing_metadata.get('release_number') != resolved_config.get('release_number'):
                print(f"  Metadata mismatch in field 'release_number'")
                return False

            print(f"  Package exists with matching metadata and source hash")
            return True

        except requests.RequestException as e:
            print(f"  Error checking existing package: {e}")
            return False

    def _prepare_source(self, recipe: Dict[str, Any], mhl_dir: str):
        """
        Fetch source code based on recipe.yaml source specification.

        Clones/downloads source directly into mhl_dir.
        Also fetches any build_sources (additional repos needed at build time).

        Args:
            recipe: Parsed recipe.yaml data
            mhl_dir: The output directory to populate
        """
        source = recipe.get('source')
        if not source:
            return  # No remote source (inline package)

        original_dir = os.getcwd()
        os.chdir(mhl_dir)

        try:
            if 'git' in source:
                clone_git_repository(
                    url=source['git'],
                    destination='.',  # Clone directly into mhl_dir
                    subdirectory=source.get('subdirectory'),
                    branch=source.get('branch'),
                )
                # Remove specified directories after cloning
                for dir_name in source.get('remove_dirs', []):
                    dir_path = os.path.join(mhl_dir, dir_name)
                    if os.path.isdir(dir_path):
                        shutil.rmtree(dir_path)
                        print(f"    Removed directory: {dir_name}")
            elif 'zip' in source:
                download_and_extract_zip(source['zip'], '.')

            # Fetch additional build-time sources
            for build_src in recipe.get('build_sources', []):
                if 'git' in build_src:
                    dest = build_src.get('destination', os.path.basename(build_src['git']).replace('.git', ''))
                    clone_git_repository(
                        url=build_src['git'],
                        destination=dest,
                        subdirectory=build_src.get('subdirectory'),
                        branch=build_src.get('branch'),
                    )
                elif 'zip' in build_src:
                    dest = build_src.get('destination', 'build_source')
                    download_and_extract_zip(build_src['zip'], dest)
        finally:
            os.chdir(original_dir)

    def _compute_paths_and_symbols(self, mhl_dir: str, resolved_config: Dict[str, Any]):
        """
        Compute addpaths and collect exposed symbols from a prepared directory.

        Args:
            mhl_dir: The prepared package directory
            resolved_config: Resolved build config

        Returns:
            Tuple of (all_paths, exposed_symbols)
        """
        addpaths_config = resolved_config.get('addpaths', [])
        all_paths = []

        for path_item in addpaths_config:
            if isinstance(path_item, str):
                all_paths.append(path_item)
            elif isinstance(path_item, dict):
                path = path_item['path']
                if path_item.get('recursive', False):
                    exclude = path_item.get('exclude', [])
                    full_path = os.path.join(mhl_dir, path)
                    recursive_paths = generate_recursive_paths(full_path, exclude)
                    all_paths.extend(recursive_paths)
                else:
                    all_paths.append(path)

        print(f"  Computed {len(all_paths)} path(s)")

        # Remove all mex binaries from source tree, for security
        mex_extensions = ['.mexw64', '.mexa64', '.mexmaci64', '.mexmaca64', '.mexw32', '.mexglx', '.mexmac']
        print("  Removing mex binaries from source tree...")
        for root, dirs, files in os.walk(mhl_dir):
            for file in files:
                if any(file.endswith(ext) for ext in mex_extensions):
                    file_path = os.path.join(root, file)
                    os.remove(file_path)
                    print(f"    Removed mex binary: {file_path}")

        # Create load/unload scripts
        create_load_and_unload_scripts(mhl_dir, all_paths)

        # Collect exposed symbols
        symbol_extensions = resolved_config.get('symbol_extensions', ['.m'])
        exposed_symbols = []

        for path in all_paths:
            full_path = os.path.join(mhl_dir, path)
            if os.path.exists(full_path):
                symbols = collect_exposed_symbols(full_path, symbol_extensions)
                exposed_symbols.extend(symbols)

        print(f"  Collected {len(exposed_symbols)} exposed symbol(s)")

        return all_paths, exposed_symbols

    def _create_mip_json(self, mhl_dir: str, mip_yaml: Dict[str, Any],
                        resolved_config: Dict[str, Any], architecture: str,
                        exposed_symbols: List[str],
                        prepare_duration: float, mhl_filename: str, source_hash: str):
        """Create mip.json metadata file."""
        # Collect build_env for build_packages.py (env var name -> relative path)
        build_env = resolved_config.get('build_env', {})

        mip_data = {
            'name': mip_yaml['name'],
            'description': mip_yaml['description'],
            'version': mip_yaml['version'],
            'release_number': resolved_config.get('release_number', 1),
            'dependencies': mip_yaml.get('dependencies', []),
            'homepage': mip_yaml.get('homepage', ''),
            'repository': mip_yaml.get('repository', ''),
            'license': mip_yaml.get('license', ''),
            'architecture': architecture,
            'build_on': resolved_config.get('build_on', 'any'),
            'usage_examples': mip_yaml.get('usage_examples', []),
            'exposed_symbols': exposed_symbols,
            'source_hash': source_hash,
            'timestamp': datetime.utcnow().isoformat() + 'Z',
            'prepare_duration': round(prepare_duration, 2),
            'compile_duration': 0,
            'mhl_url': f"{get_base_url(mip_yaml['name'] + '-' + mip_yaml['version'])}/{mhl_filename}"
        }
        if build_env:
            mip_data['build_env'] = build_env

        mip_json_path = os.path.join(mhl_dir, 'mip.json')
        with open(mip_json_path, 'w') as f:
            json.dump(mip_data, f, indent=2)

    def prepare_package_dir(self, package_dir: str, *, release: Optional[str]) -> bool:
        """Prepare a single package directory."""
        package_name = os.path.basename(package_dir)
        print(f"\nProcessing package: {package_name}")

        # Check that package name doesn't contain hyphens
        if '-' in package_name:
            print(f"  Error: Package name '{package_name}' contains hyphens ('-').")
            print(f"  Please use underscores ('_') instead of hyphens in package names.")
            return False

        releases_folder_path = os.path.join(package_dir, 'releases')

        for release_version in os.listdir(releases_folder_path):
            if release is not None and release_version != release:
                print(f"  Skipping release '{release_version}' (looking for '{release}')")
                continue
            release_folder_path = os.path.join(releases_folder_path, release_version)
            if not os.path.isdir(release_folder_path):
                continue

            print(f"  Processing release: {release_version}")

            # Check that release version doesn't contain hyphens
            if '-' in release_version:
                print(f"  Error: Release version '{release_version}' contains hyphens ('-').")
                print(f"  Please use underscores ('_') instead of hyphens in release versions.")
                return False

            # Load recipe.yaml
            recipe_path = os.path.join(release_folder_path, 'recipe.yaml')
            if not os.path.exists(recipe_path):
                print(f"  Warning: No recipe.yaml found")
                return True

            with open(recipe_path, 'r') as f:
                recipe = yaml.safe_load(f) or {}

            # Get BUILD_ARCHITECTURE from environment
            architecture_env = os.environ.get('BUILD_ARCHITECTURE', 'any')

            # Compute source hash for the release folder
            print(f"  Computing source hash for {release_folder_path}...")
            source_hash = compute_directory_hash(release_folder_path)

            # Resolve remote commit hashes for branch-based git sources
            remote_commit_hashes = []
            source = recipe.get('source', {})
            if source and 'git' in source:
                branch = source.get('branch')
                if branch:
                    commit_hash = resolve_git_commit_hash(source['git'], branch)
                    print(f"  Resolved {source['git']} {branch} -> {commit_hash[:12]}")
                    remote_commit_hashes.append(commit_hash)

            # Also resolve build_sources branches
            for build_src in recipe.get('build_sources', []):
                if 'git' in build_src:
                    branch = build_src.get('branch')
                    if branch:
                        commit_hash = resolve_git_commit_hash(build_src['git'], branch)
                        print(f"  Resolved {build_src['git']} {branch} -> {commit_hash[:12]}")
                        remote_commit_hashes.append(commit_hash)

            if remote_commit_hashes:
                combined = hashlib.sha1()
                combined.update(source_hash.encode('utf-8'))
                for ch in sorted(remote_commit_hashes):
                    combined.update(ch.encode('utf-8'))
                source_hash = combined.hexdigest()

            print(f"  Source hash: {source_hash}")

            # --- Stage 1: Fetch source and overlay channel files into a temp dir ---
            # We need to read mip.yaml to know the builds, but mip.yaml may come
            # from either the source repo or the channel overlay. So we do a
            # preliminary fetch to get mip.yaml.

            temp_work_dir = os.path.join(self.output_dir, f"_temp_{package_name}_{release_version}")
            if os.path.exists(temp_work_dir):
                shutil.rmtree(temp_work_dir)
            os.makedirs(temp_work_dir)

            try:
                # Fetch source
                self._prepare_source(recipe, temp_work_dir)

                # Overlay channel-provided files (mip.yaml, compile.m, etc.)
                overlay_channel_files(release_folder_path, temp_work_dir)

                # Read mip.yaml from the merged directory
                mip_yaml_path = os.path.join(temp_work_dir, 'mip.yaml')
                if not os.path.exists(mip_yaml_path):
                    print(f"  Error: No mip.yaml found (not in source repo and not in channel)")
                    return False

                with open(mip_yaml_path, 'r') as f:
                    mip_yaml = yaml.safe_load(f)

            finally:
                # Clean up temp dir - we'll re-fetch per build if needed
                if os.path.exists(temp_work_dir):
                    shutil.rmtree(temp_work_dir)

            # Validate version matches release folder
            if str(mip_yaml.get('version')) != release_version:
                print(f"  Error: version in mip.yaml ({mip_yaml.get('version')}) does not match release folder name ({release_version}).")
                return False

            # Find matching (build, architecture) pairs
            builds = mip_yaml.get('builds', [])
            matching_pairs: List[tuple] = []
            for b in builds:
                archs = b.get('architectures', [])
                if architecture_env in archs:
                    matching_pairs.append((b, architecture_env))
                elif 'any' in archs and architecture_env == 'linux_x86_64':
                    matching_pairs.append((b, 'any'))

            if not matching_pairs:
                print(f"  No builds match ARCHITECTURE={architecture_env}, skipping")
                return True

            # Process each matching (build, architecture) pair
            for build, matched_architecture in matching_pairs:
                resolved_config = resolve_build_config(mip_yaml, build)

                mhl_filename = self._get_mhl_filename(mip_yaml, matched_architecture)
                wheel_name = mhl_filename[:-4]  # Remove .mhl
                print(f"  Wheel name: {wheel_name}")

                # Check if exists
                if not self.force and self._check_existing_package(mhl_filename, mip_yaml, resolved_config, source_hash):
                    print(f"  Skipping - package already up to date")
                    continue

                if self.dry_run:
                    print(f"  [DRY RUN] Would prepare {wheel_name}.dir")
                    continue

                # Create output directory
                output_dir_path = os.path.join(self.output_dir, f"{wheel_name}.dir")

                if os.path.exists(output_dir_path):
                    print(f"  Removing existing directory")
                    shutil.rmtree(output_dir_path)

                os.makedirs(output_dir_path)
                print(f"  Output directory: {output_dir_path}")

                try:
                    print(f"  Preparing package...")
                    prepare_start = time.time()

                    # Fetch source into the output directory
                    self._prepare_source(recipe, output_dir_path)

                    # Overlay channel-provided files
                    overlay_channel_files(release_folder_path, output_dir_path)

                    # Compute paths and symbols
                    all_paths, exposed_symbols = self._compute_paths_and_symbols(
                        output_dir_path, resolved_config
                    )

                    prepare_duration = time.time() - prepare_start
                    print(f"  Prepare completed in {prepare_duration:.2f} seconds")

                    # Create mip.json
                    print(f"  Creating mip.json...")
                    self._create_mip_json(
                        output_dir_path, mip_yaml, resolved_config,
                        matched_architecture, exposed_symbols,
                        prepare_duration, mhl_filename, source_hash
                    )

                    # Ensure build/compile scripts are available.
                    # They may already be present from the source or overlay.
                    # If not, check the release folder.
                    for script_key in ('compile_script', 'build_script', 'test_script'):
                        if script_key not in resolved_config:
                            continue
                        script_path = resolved_config[script_key]
                        script_in_prepared = os.path.join(output_dir_path, script_path)
                        if os.path.exists(script_in_prepared):
                            print(f"  {script_key} found in prepared source: {script_path}")
                        else:
                            print(f"  Warning: {script_key} '{script_path}' not found in prepared directory")
                        # Ensure shell scripts are executable
                        if os.path.exists(script_in_prepared) and script_path.endswith('.sh'):
                            os.chmod(script_in_prepared, 0o755)

                    print(f"  Successfully prepared {wheel_name}.dir")

                except Exception as e:
                    print(f"  Error preparing package: {e}")
                    import traceback
                    traceback.print_exc()

                    if os.path.exists(output_dir_path):
                        shutil.rmtree(output_dir_path, ignore_errors=True)

                    return False

        return True

    def prepare_all_packages(self) -> bool:
        """Prepare all packages in packages/."""
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        packages_dir = os.path.join(project_root, 'packages')

        if not os.path.exists(packages_dir):
            print(f"Error: packages directory not found at {packages_dir}")
            return False

        # Get all package directories
        package_dirs = [
            os.path.join(packages_dir, d)
            for d in os.listdir(packages_dir)
            if os.path.isdir(os.path.join(packages_dir, d))
        ]

        if len(package_dirs) == 0:
            print("No package directories found. Nothing to do.")
            return True

        print(f"Found {len(package_dirs)} package(s)")
        print(f"Output directory: {self.output_dir}")
        print(f"ARCHITECTURE: {os.environ.get('BUILD_ARCHITECTURE', 'any')}")

        # Prepare each package
        all_success = True
        for package_dir in sorted(package_dirs):
            success = self.prepare_package_dir(package_dir, release=None)
            if not success:
                print(f"\nError: Preparation failed for {os.path.basename(package_dir)}")
                all_success = False
                break

        return all_success


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Prepare MATLAB packages from recipe.yaml + mip.yaml specifications'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Simulate operations without building'
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='Rebuild packages even if they exist in the bucket'
    )
    parser.add_argument(
        '--output-dir',
        type=str,
        help='Directory where .dir packages will be created (default: build/prepared)'
    )
    parser.add_argument(
        '--package',
        type=str,
        help='Prepare only the specified package by name'
    )
    parser.add_argument(
        '--release',
        type=str,
        help='Prepare only the specified release of the package'
    )

    args = parser.parse_args()

    preparer = PackagePreparer(
        dry_run=args.dry_run,
        force=args.force,
        output_dir=args.output_dir
    )

    print("Starting package preparation process...")
    if args.dry_run:
        print("[DRY RUN MODE - No actual building will occur]")
    if args.force:
        print("[FORCE MODE - Will rebuild all packages]")

    if args.package:
        # Prepare single package
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        packages_dir = os.path.join(project_root, 'packages')
        package_dir = os.path.join(packages_dir, args.package)

        if not os.path.exists(package_dir):
            print(f"\n✗ Error: Package '{args.package}' not found at {package_dir}")
            return 1

        if not os.path.isdir(package_dir):
            print(f"\n✗ Error: '{args.package}' is not a directory")
            return 1

        print(f"Preparing single package: {args.package}")
        print(f"Output directory: {preparer.output_dir}")
        print(f"ARCHITECTURE: {os.environ.get('BUILD_ARCHITECTURE', 'any')}")

        success = preparer.prepare_package_dir(package_dir, release=args.release)
    else:
        # Prepare all packages
        success = preparer.prepare_all_packages()

    if success:
        print("\n✓ All packages prepared successfully")
        return 0
    else:
        print("\n✗ Preparation process failed")
        return 1


if __name__ == '__main__':
    sys.exit(main())
