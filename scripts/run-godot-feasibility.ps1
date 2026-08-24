param(
    [string]$GameAssembly = '',
    [int]$Iterations = 100000
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$repositoryRoot = Get-DivineRepositoryRoot
$GameAssembly = Get-DivineGameAssembly $GameAssembly
$projectPath = Join-Path $repositoryRoot 'src\Sts2.NativeSim.GodotHost'
$projectFile = Join-Path $projectPath 'Sts2.NativeSim.GodotHost.csproj'
$godot = Get-DivineGodot
$dotnet = Get-DivineDotnet

& $dotnet build $projectFile -c Debug | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "Godot host build failed with exit code $LASTEXITCODE"
}

& $godot --headless --path $projectPath -- $GameAssembly $Iterations | Out-Host
exit $LASTEXITCODE
