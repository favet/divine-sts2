param([ValidateSet('Debug','Release')][string]$Configuration = 'Debug')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$dotnet = Get-DivineDotnet
$dotnetRoot = Split-Path -Parent $dotnet
$env:DOTNET_ROOT = $dotnetRoot
$env:DOTNET_ROOT_X64 = $dotnetRoot
$env:PATH = "$dotnetRoot;$env:PATH"
$gameRoot = Get-DivineGameRoot
$gameData = Join-Path $gameRoot 'data_sts2_windows_x86_64'
& $dotnet build (Join-Path $PSScriptRoot '..\src\Sts2.NativeSim.GodotHost\Sts2.NativeSim.GodotHost.csproj') -c $Configuration -p:GameDataDir=$gameData
if ($LASTEXITCODE -ne 0) { throw "Persistent server build failed with exit code $LASTEXITCODE" }
$godot = Get-DivineGodot
$project = Join-Path $PSScriptRoot '..\src\Sts2.NativeSim.GodotHost'
& $godot --headless --editor --path $project --build-solutions --quit-after 8
if ($LASTEXITCODE -ne 0) { throw "Godot C# solution import failed with exit code $LASTEXITCODE" }
