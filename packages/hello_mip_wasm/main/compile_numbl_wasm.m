% Compile hello_mip_wasm for numbl WASM target
% Wraps the shell build script via system()

fprintf('=== Compiling hello_mip_wasm for numbl WASM ===\n');

scriptPath = fullfile(pwd, 'numbl', 'build_wasm.sh');
if ~exist(scriptPath, 'file')
    error('build_wasm.sh not found at %s', scriptPath);
end

[status, output] = system(sprintf('bash "%s"', scriptPath));
fprintf('%s', output);
if status ~= 0
    error('build_wasm.sh failed (exit code %d)', status);
end

fprintf('=== hello_mip_wasm numbl WASM build complete ===\n');
