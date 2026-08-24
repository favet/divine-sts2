$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$repoRoot = Get-DivineRepositoryRoot
$dotnet = Get-DivineDotnet
$solution = Join-Path $repoRoot 'Sts2.NativeSim.sln'
$host = Join-Path $repoRoot 'src\Sts2.NativeSim.Host'
$assembly = Get-DivineGameAssembly
$env:STS2_GAME_ROOT = Get-DivineGameRoot

& $dotnet build $solution -c Release --nologo
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $dotnet run --project $host -c Release --no-build -- $assembly 100000
exit $LASTEXITCODE
