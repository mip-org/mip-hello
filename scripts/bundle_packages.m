% Bundle all prepared packages using mip bundle.
%
% This script discovers all prepared directories in build/prepared/
% and calls mip.bundle() on each to produce .mhl files in build/bundled/.
%
% Expected to be run from the repository root directory.

fprintf('=== Bundle Packages ===\n');

preparedDir = fullfile(pwd, 'build', 'prepared');
outputDir = fullfile(pwd, 'build', 'bundled');

architecture = getenv('BUILD_ARCHITECTURE');
if isempty(architecture)
    % err
    error('mip:missingArchitecture', 'Environment variable BUILD_ARCHITECTURE is not set');
end

if ~exist(preparedDir, 'dir')
    fprintf('No prepared directory found at %s. Nothing to bundle.\n', preparedDir);
    return;
end

if ~exist(outputDir, 'dir')
    mkdir(outputDir);
end

% List prepared directories
items = dir(preparedDir);
bundled = 0;
failed = 0;

for i = 1:length(items)
    if ~items(i).isdir || startsWith(items(i).name, '.')
        continue;
    end

    pkgDir = fullfile(preparedDir, items(i).name);

    % Check for mip.yaml
    if ~exist(fullfile(pkgDir, 'mip.yaml'), 'file')
        fprintf('Skipping %s (no mip.yaml)\n', items(i).name);
        continue;
    end

    fprintf('\n--- Bundling: %s ---\n', items(i).name);

    try
        mip.bundle(pkgDir, '--output', outputDir, '--arch', architecture);
        bundled = bundled + 1;
    catch ME
        fprintf('Error bundling %s: %s\n', items(i).name, ME.message);
        failed = failed + 1;
    end
end

fprintf('\n=== Bundle Summary ===\n');
fprintf('Bundled: %d\n', bundled);
fprintf('Failed: %d\n', failed);

if failed > 0
    error('mip:bundleFailed', '%d package(s) failed to bundle', failed);
end
