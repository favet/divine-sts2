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
    if ($LASTEXITCODE -ne 0) { throw ".NET SDK installation failed with exit code $LASTEXITCODE" }
}
Write-Output (Join-Path $destination 'dotnet.exe')
