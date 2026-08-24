param([ValidateSet('Debug','Release')][string]$Configuration = 'Debug')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$dotnet = Get-DivineDotnet
$gameRoot = Get-DivineGameRoot
$gameData = Join-Path $gameRoot 'data_sts2_windows_x86_64'
& $dotnet build (Join-Path $PSScriptRoot '..\src\Sts2.NativeSim.GodotHost\Sts2.NativeSim.GodotHost.csproj') -c $Configuration -p:GameDataDir=$gameData
if ($LASTEXITCODE -ne 0) { throw "Persistent server build failed with exit code $LASTEXITCODE" }
