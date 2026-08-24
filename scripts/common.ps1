Set-StrictMode -Version Latest

function Get-DivineRepositoryRoot {
    return (Split-Path -Parent $PSScriptRoot)
}

function Get-DivineDotnet {
    $bundled = Join-Path (Get-DivineRepositoryRoot) '.tools\dotnet9\dotnet.exe'
    if (Test-Path -LiteralPath $bundled) { return $bundled }
    $command = Get-Command dotnet -ErrorAction SilentlyContinue
    if ($command) {
        $sdks = & $command.Source --list-sdks 2>$null
        if ($LASTEXITCODE -eq 0 -and ($sdks | Where-Object { $_ -match '^9\.' })) { return $command.Source }
    }
    throw '.NET 9 SDK was not found. Install it or place it under .tools\dotnet9.'
}

function Get-DivineGameRoot([string]$Explicit = '') {
    $override = if ($Explicit) { $Explicit } else { $env:STS2_GAME_ROOT }
    if ($override) {
        $candidate = (Resolve-Path -LiteralPath $override -ErrorAction SilentlyContinue).Path
        if (-not $candidate) { throw "Configured STS2_GAME_ROOT does not exist: $override" }
        $exe = Join-Path $candidate 'SlayTheSpire2.exe'
        $pck = Join-Path $candidate 'SlayTheSpire2.pck'
        $dll = Join-Path $candidate 'data_sts2_windows_x86_64\sts2.dll'
        if (-not ((Test-Path -LiteralPath $exe) -and (Test-Path -LiteralPath $pck) -and (Test-Path -LiteralPath $dll))) {
            throw "Configured STS2_GAME_ROOT is not a complete game install: $candidate"
        }
        return $candidate
    }
    $candidates = [Collections.Generic.List[string]]::new()
    foreach ($programFiles in @(${env:ProgramFiles(x86)}, $env:ProgramFiles)) {
        if (-not $programFiles) { continue }
        $steam = Join-Path $programFiles 'Steam'
        $candidates.Add((Join-Path $steam 'steamapps\common\Slay the Spire 2'))
        $manifest = Join-Path $steam 'steamapps\libraryfolders.vdf'
        if (Test-Path -LiteralPath $manifest) {
            $text = Get-Content -Raw -LiteralPath $manifest
            foreach ($match in [regex]::Matches($text, '"path"\s+"([^"]+)"')) {
                $library = $match.Groups[1].Value -replace '\\\\', '\'
                $candidates.Add((Join-Path $library 'steamapps\common\Slay the Spire 2'))
            }
        }
    }
    foreach ($candidate in $candidates | Select-Object -Unique) {
        $exe = Join-Path $candidate 'SlayTheSpire2.exe'
        $pck = Join-Path $candidate 'SlayTheSpire2.pck'
        $dll = Join-Path $candidate 'data_sts2_windows_x86_64\sts2.dll'
        if ((Test-Path -LiteralPath $exe) -and (Test-Path -LiteralPath $pck) -and (Test-Path -LiteralPath $dll)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    throw 'Slay the Spire 2 was not found. Set STS2_GAME_ROOT to its installed directory.'
}

function Get-DivineGameAssembly([string]$Explicit = '') {
    if ($Explicit) { $candidate = $Explicit }
    elseif ($env:STS2_ASSEMBLY) { $candidate = $env:STS2_ASSEMBLY }
    else { $candidate = Join-Path (Get-DivineGameRoot) 'data_sts2_windows_x86_64\sts2.dll' }
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { throw "STS2 assembly not found: $candidate" }
    return (Resolve-Path -LiteralPath $candidate).Path
}

function Get-DivineGodot {
    if ($env:GODOT -and (Test-Path -LiteralPath $env:GODOT -PathType Leaf)) { return $env:GODOT }
    $root = Join-Path (Get-DivineRepositoryRoot) '.tools\godot-4.5.1-mono'
    foreach ($name in @('Godot_v4.5.1-stable_mono_win64_console.exe', 'Godot_v4.5.1-stable_mono_win64.exe')) {
        $match = Get-ChildItem -LiteralPath $root -Recurse -File -Filter $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($match) { return $match.FullName }
    }
    throw 'Godot 4.5.1 .NET was not found. Run scripts\bootstrap.ps1 or set GODOT.'
}
