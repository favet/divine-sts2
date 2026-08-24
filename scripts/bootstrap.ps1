param([switch]$Train, [string]$TorchIndexUrl = '')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$root = Get-DivineRepositoryRoot
& (Join-Path $PSScriptRoot 'install-dotnet-9.ps1') | Out-Host
& (Join-Path $PSScriptRoot 'install-godot-4.5.1.ps1') | Out-Host
$bundledDotnet = Join-Path $root '.tools\dotnet9'
$env:DOTNET_ROOT = $bundledDotnet
$env:DOTNET_ROOT_X64 = $bundledDotnet
$env:PATH = "$bundledDotnet;$env:PATH"
$extras = if ($Train) { '[dev,train]' } else { '[dev]' }
python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'pip upgrade failed.' }
if ($Train -and $TorchIndexUrl) {
    python -m pip install 'torch>=2.6' --index-url $TorchIndexUrl --extra-index-url 'https://pypi.org/simple'
    if ($LASTEXITCODE -ne 0) { throw 'CUDA Torch installation failed.' }
}
python -m pip install -e "$root$extras"
if ($LASTEXITCODE -ne 0) { throw 'Python environment installation failed.' }

$gameRoot = Get-DivineGameRoot
$env:STS2_GAME_ROOT = $gameRoot
$env:STS2_ASSEMBLY = Join-Path $gameRoot 'data_sts2_windows_x86_64\sts2.dll'
& (Join-Path $PSScriptRoot 'build-persistent-server.ps1') -Configuration Release
if ($LASTEXITCODE -ne 0) { throw 'Native host build failed.' }
python -m sts2_native_sim.cli doctor --deep
if ($LASTEXITCODE -ne 0) { throw 'Deep doctor failed. Resolve the reported checks before using the native worker.' }
