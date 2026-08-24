$ErrorActionPreference = 'Stop'
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$toolRoot = Join-Path $repositoryRoot '.tools'
$destination = Join-Path $toolRoot 'dotnet9'
$installer = Join-Path $toolRoot 'dotnet-install.ps1'

New-Item -ItemType Directory -Path $toolRoot -Force | Out-Null
if (-not (Test-Path -LiteralPath $installer)) {
    Invoke-WebRequest -UseBasicParsing -Uri 'https://dot.net/v1/dotnet-install.ps1' -OutFile $installer
}
if (-not (Test-Path -LiteralPath (Join-Path $destination 'dotnet.exe'))) {
    & $installer -Channel 9.0 -InstallDir $destination -NoPath
    $installerExit = 0
    if (Test-Path variable:LASTEXITCODE) { $installerExit = [int]$LASTEXITCODE }
    if ($installerExit -ne 0) { throw ".NET SDK installation failed with exit code $installerExit" }
    if (-not (Test-Path -LiteralPath (Join-Path $destination 'dotnet.exe'))) {
        throw ".NET SDK installation reported success, but dotnet.exe was not found under $destination."
    }
}
Write-Output (Join-Path $destination 'dotnet.exe')
