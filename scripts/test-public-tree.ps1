$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$root = Get-DivineRepositoryRoot
$dotnet = Get-DivineDotnet

python -m compileall -q (Join-Path $root 'python')
if ($LASTEXITCODE -ne 0) { throw 'Python compilation failed.' }
& $dotnet build (Join-Path $root 'src\Sts2.NativeSim.Protocol\Sts2.NativeSim.Protocol.csproj') -c Release --nologo
if ($LASTEXITCODE -ne 0) { throw 'Protocol build failed.' }
& $dotnet build (Join-Path $root 'src\Sts2.NativeSim.Core\Sts2.NativeSim.Core.csproj') -c Release --nologo
if ($LASTEXITCODE -ne 0) { throw 'Core build failed.' }

$forbidden = git -C $root grep -n -E '(C:\\Users\\|F:\\SteamLibrary|ghp_[A-Za-z0-9_]+)' -- .
if ($LASTEXITCODE -eq 0) { throw "Machine-specific path or token found:`n$forbidden" }
if ($LASTEXITCODE -gt 1) { throw 'Source scan failed.' }

$binaries = Get-ChildItem -LiteralPath $root -Recurse -File | Where-Object {
    $_.Extension -in @('.dll','.exe','.pck','.pt','.pth','.ckpt') -and $_.FullName -notmatch '[\\/](bin|obj|\.godot|artifacts|models|data|datasets)[\\/]'
}
if ($binaries) { throw "Forbidden distributable binaries found:`n$($binaries.FullName -join "`n")" }
Write-Host 'Public-tree checks passed.'
