$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$repoRoot = Get-DivineRepositoryRoot
$dotnet = Get-DivineDotnet
$host = Join-Path $repoRoot 'src\Sts2.NativeSim.Host'
$assembly = Get-DivineGameAssembly

& $dotnet run --project $host -c Release -- $assembly 1000000
exit $LASTEXITCODE
