$ErrorActionPreference = 'Stop'
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$toolRoot = Join-Path $repositoryRoot '.tools'
$archive = Join-Path $toolRoot 'Godot_v4.5.1-stable_mono_win64.zip'
$destination = Join-Path $toolRoot 'godot-4.5.1-mono'
$download = 'https://github.com/godotengine/godot/releases/download/4.5.1-stable/Godot_v4.5.1-stable_mono_win64.zip'

New-Item -ItemType Directory -Path $toolRoot -Force | Out-Null
if (-not (Test-Path -LiteralPath $archive)) {
    curl.exe -L --fail --retry 3 -o $archive $download
}
if (-not (Test-Path -LiteralPath $destination)) {
    New-Item -ItemType Directory -Path $destination | Out-Null
    Expand-Archive -LiteralPath $archive -DestinationPath $destination
}

$godot = Get-ChildItem -LiteralPath $destination -Recurse -File -Filter 'Godot_v4.5.1-stable_mono_win64.exe' |
    Select-Object -First 1 -ExpandProperty FullName
if (-not $godot) {
    throw 'The Godot archive was downloaded, but its executable was not found after extraction.'
}

Write-Output $godot
