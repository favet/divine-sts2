param([ValidateSet('Debug','Release')][string]$Configuration = 'Release')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$dotnet = Get-DivineDotnet
$dotnetRoot = Split-Path -Parent $dotnet
$env:DOTNET_ROOT = $dotnetRoot
$env:DOTNET_ROOT_X64 = $dotnetRoot
$env:PATH = "$dotnetRoot;$env:PATH"
$gameRoot = Get-DivineGameRoot
$gameData = Join-Path $gameRoot 'data_sts2_windows_x86_64'

# Build the pure .NET 9 persistent host
& $dotnet build (Join-Path $PSScriptRoot '..\src\Sts2.NativeSim.Host\Sts2.NativeSim.Host.csproj') -c $Configuration -p:GameDataDir=$gameData
if ($LASTEXITCODE -ne 0) { throw "Persistent pure .NET server build failed with exit code $LASTEXITCODE" }

# Optionally build GodotHost if Godot is configured
if (Test-Path (Join-Path $PSScriptRoot '..\src\Sts2.NativeSim.GodotHost\Sts2.NativeSim.GodotHost.csproj')) {
    try {
        $godot = Get-DivineGodot
        if ($godot) {
            & $dotnet build (Join-Path $PSScriptRoot '..\src\Sts2.NativeSim.GodotHost\Sts2.NativeSim.GodotHost.csproj') -c $Configuration -p:GameDataDir=$gameData
        }
    } catch {
        # Godot is optional for the pure .NET 9 runner
    }
}
