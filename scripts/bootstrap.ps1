param([switch]$Train)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$root = Get-DivineRepositoryRoot
& (Join-Path $PSScriptRoot 'install-dotnet-9.ps1') | Out-Host
& (Join-Path $PSScriptRoot 'install-godot-4.5.1.ps1') | Out-Host
$extras = if ($Train) { '[dev,train]' } else { '[dev]' }
python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'pip upgrade failed.' }
python -m pip install -e "$root$extras"
if ($LASTEXITCODE -ne 0) { throw 'Python environment installation failed.' }

$gameRoot = Get-DivineGameRoot
$env:STS2_GAME_ROOT = $gameRoot
$env:STS2_ASSEMBLY = Join-Path $gameRoot 'data_sts2_windows_x86_64\sts2.dll'
& (Join-Path $PSScriptRoot 'build-persistent-server.ps1') -Configuration Release
if ($LASTEXITCODE -ne 0) { throw 'Native host build failed.' }
divine-sts2 doctor --deep
