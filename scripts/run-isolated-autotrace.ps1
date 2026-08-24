param(
    [ValidatePattern('^[0-9A-HJ-NP-Z]{10}$')][string]$Seed = 'A1B2C3D4E5',
    [ValidateRange(1,100)][int]$CombatCount = 1,
    [ValidateSet('coverage','basics')][string]$Policy = 'coverage',
    [string]$GameRoot = '',
    [string]$SandboxRoot = '',
    [string]$CandidateDirectory = '',
    [string]$CertifiedDirectory = '',
    [string]$FailureDirectory = '',
    [ValidateRange(30,3600)][int]$BaseTimeoutSeconds = 180,
    [ValidateRange(30,900)][int]$PerCombatTimeoutSeconds = 180,
    [bool]$Resume = $true
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$repositoryRoot = Get-DivineRepositoryRoot
$GameRoot = Get-DivineGameRoot $GameRoot
if (-not $SandboxRoot) { $SandboxRoot = Join-Path (${env:LOCALAPPDATA} ?? [IO.Path]::GetTempPath()) 'divine-sts2\autotrace-sandboxes' }
$gameRootResolved = (Resolve-Path -LiteralPath $GameRoot).Path
$sandboxParent = [System.IO.Path]::GetFullPath($SandboxRoot)
if ($sandboxParent -eq $gameRootResolved -or $sandboxParent.StartsWith($gameRootResolved + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'SandboxRoot must be separate from the installed game directory.'
}
$safeSeed = ($Seed -replace '[^A-Za-z0-9_.-]', '_')
$runKey = "$safeSeed-c$CombatCount-$Policy"
$sandboxFull = Join-Path $sandboxParent $runKey
$manifestPath = Join-Path $sandboxFull 'native-sim-autotrace-run.json'
if (-not $CandidateDirectory) { $CandidateDirectory = Join-Path $repositoryRoot 'artifacts\shipped-autotraces\candidates' }
if (-not $CertifiedDirectory) { $CertifiedDirectory = Join-Path $repositoryRoot 'artifacts\shipped-autotraces\certified' }
if (-not $FailureDirectory) { $FailureDirectory = Join-Path $repositoryRoot 'artifacts\shipped-autotraces\failures' }

$dotnet = Get-DivineDotnet
$env:STS2_GAME_ROOT = $gameRootResolved
$exporterProject = Join-Path $repositoryRoot 'src\Sts2.NativeSim.TraceExporter\Sts2.NativeSim.TraceExporter.csproj'
$driverProject = Join-Path $repositoryRoot 'src\Sts2.NativeSim.AutoTraceDriver\Sts2.NativeSim.AutoTraceDriver.csproj'
$workerProject = Join-Path $repositoryRoot 'src\Sts2.NativeSim.GodotHost\Sts2.NativeSim.GodotHost.csproj'
& $dotnet build $exporterProject -c Release --nologo | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Trace exporter build failed with exit code $LASTEXITCODE" }
& $dotnet build $driverProject -c Release --nologo | Out-Host
if ($LASTEXITCODE -ne 0) { throw "AutoTrace driver build failed with exit code $LASTEXITCODE" }
& $dotnet build $workerProject -c Release --nologo | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Differential worker build failed with exit code $LASTEXITCODE" }

$existingManifest = $null
if (Test-Path -LiteralPath $sandboxFull) {
    if (-not $Resume -or -not (Test-Path -LiteralPath $manifestPath)) {
        throw "Refusing existing sandbox without a matching resumable manifest: $sandboxFull"
    }
    $existingManifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
    if ($existingManifest.seed -ne $Seed -or $existingManifest.combat_count -ne $CombatCount -or $existingManifest.policy -ne $Policy -or $existingManifest.game_root -ne $gameRootResolved) {
        throw "Refusing sandbox with a mismatched manifest: $sandboxFull"
    }
} else {
    New-Item -ItemType Directory -Path $sandboxFull -Force | Out-Null
    foreach ($file in Get-ChildItem -LiteralPath $gameRootResolved -File) {
        New-Item -ItemType HardLink -Path (Join-Path $sandboxFull $file.Name) -Target $file.FullName | Out-Null
    }
    foreach ($directoryName in @('controller_config', 'data_sts2_windows_x86_64')) {
        New-Item -ItemType Junction -Path (Join-Path $sandboxFull $directoryName) -Target (Join-Path $gameRootResolved $directoryName) | Out-Null
    }
    [pscustomobject]@{
        format_version = 1
        seed = $Seed
        combat_count = $CombatCount
        policy = $Policy
        game_root = $gameRootResolved
        status = 'prepared'
    } | ConvertTo-Json | Set-Content -LiteralPath $manifestPath -Encoding utf8
}

$shouldLaunch = $null -eq $existingManifest -or -not $existingManifest.status.StartsWith('captured', [System.StringComparison]::Ordinal)
$launchFailure = $null
if ($shouldLaunch) {
    $mods = Join-Path $sandboxFull 'mods'
    $exporterDestination = Join-Path $mods 'Sts2NativeTraceExporter'
    $driverDestination = Join-Path $mods 'Sts2NativeAutoTraceDriver'
    New-Item -ItemType Directory -Path $exporterDestination,$driverDestination -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path (Split-Path $exporterProject) 'bin\Release\net9.0\package\sts2-native-trace-exporter.dll'),(Join-Path (Split-Path $exporterProject) 'bin\Release\net9.0\package\sts2-native-trace-exporter.json') -Destination $exporterDestination -Force
    Copy-Item -LiteralPath (Join-Path (Split-Path $driverProject) 'bin\Release\net9.0\package\sts2-native-autotrace-driver.dll'),(Join-Path (Split-Path $driverProject) 'bin\Release\net9.0\package\sts2-native-autotrace-driver.json') -Destination $driverDestination -Force

    $isolatedAppData = Join-Path $sandboxFull 'userdata'
    $isolatedSettingsDirectory = Join-Path $isolatedAppData 'SlayTheSpire2\default\1'
    New-Item -ItemType Directory -Path $isolatedSettingsDirectory -Force | Out-Null
    $sourceSettings = Join-Path $env:APPDATA 'SlayTheSpire2\default\1\settings.save'
    if (-not (Test-Path -LiteralPath $sourceSettings)) { throw "A source settings schema was not found at $sourceSettings" }
    $settings = Get-Content -Raw -LiteralPath $sourceSettings | ConvertFrom-Json
    $settings.mod_settings = [pscustomobject]@{ mods_enabled = $true; mod_list = @() }
    $settings.fullscreen = $false
    $settings.skip_intro_logo = $true
    $settings | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath (Join-Path $isolatedSettingsDirectory 'settings.save') -Encoding utf8

    $savedAppData = $env:APPDATA
    $savedLocalAppData = $env:LOCALAPPDATA
    $savedIsolationRoot = $env:STS2_NATIVE_AUTOTRACE_ROOT
    try {
        $env:APPDATA = $isolatedAppData
        $env:LOCALAPPDATA = Join-Path $sandboxFull 'local_userdata'
        $env:STS2_NATIVE_AUTOTRACE_ROOT = $isolatedAppData
        $game = Join-Path $sandboxFull 'SlayTheSpire2.exe'
        $log = Join-Path $sandboxFull 'autotrace.log'
        $arguments = @('--headless', '--force-steam=off', '--native-sim-trace', '--native-sim-autotrace-driver', "--native-sim-combat-count=$CombatCount", "--native-sim-autotrace-policy=$Policy", "--seed=$Seed", "--log-file=$log")
        $gameProcess = Start-Process -FilePath $game -ArgumentList $arguments -PassThru -WindowStyle Hidden
        $timeoutMilliseconds = 1000 * ($BaseTimeoutSeconds + ($PerCombatTimeoutSeconds * $CombatCount))
        if (-not $gameProcess.WaitForExit($timeoutMilliseconds)) {
            Stop-Process -Id $gameProcess.Id -Force -ErrorAction SilentlyContinue
            $launchFailure = "Isolated shipped runtime exceeded the bounded $([Math]::Round($timeoutMilliseconds / 60000, 1))-minute timeout for $CombatCount combats. See $log"
        } elseif ($gameProcess.ExitCode -ne 0) {
            $launchFailure = "Isolated shipped runtime exited with code $($gameProcess.ExitCode). See $log"
        }
    }
    finally {
        $env:APPDATA = $savedAppData
        $env:LOCALAPPDATA = $savedLocalAppData
        $env:STS2_NATIVE_AUTOTRACE_ROOT = $savedIsolationRoot
    }
    [pscustomobject]@{
        format_version = 1
        seed = $Seed
        combat_count = $CombatCount
        policy = $Policy
        game_root = $gameRootResolved
        status = if ($launchFailure) { 'captured_failed' } else { 'captured' }
    } | ConvertTo-Json | Set-Content -LiteralPath $manifestPath -Encoding utf8
}

$isolatedAppData = Join-Path $sandboxFull 'userdata'
$traceDirectory = Join-Path $isolatedAppData 'SlayTheSpire2\native_sim_traces'
$traces = @(Get-ChildItem -LiteralPath $traceDirectory -File -Filter '*.jsonl' -ErrorAction SilentlyContinue | Sort-Object Name)
New-Item -ItemType Directory -Path $CandidateDirectory,$CertifiedDirectory,$FailureDirectory -Force | Out-Null
$replay = Join-Path $repositoryRoot 'python\differential_replay.py'
$certifiedPaths = @()
$failedPaths = @()
$duplicatePaths = @()
$certifiedHashes = @{}
$certifiedSemanticHashes = @{}
$inventoryScript = Join-Path $repositoryRoot 'python\trace_inventory.py'
foreach ($certifiedTrace in Get-ChildItem -LiteralPath $CertifiedDirectory -File -Filter '*.jsonl' -ErrorAction SilentlyContinue) {
    $certifiedHashes[(Get-FileHash -Algorithm SHA256 -LiteralPath $certifiedTrace.FullName).Hash] = $certifiedTrace.FullName
    $semanticOutput = & python $inventoryScript --semantic-hashes-only $certifiedTrace.FullName
    if ($LASTEXITCODE -ne 0) { throw "Semantic inventory failed for $($certifiedTrace.FullName)" }
    foreach ($semanticHash in (($semanticOutput | ConvertFrom-Json).PSObject.Properties.Value)) { $certifiedSemanticHashes[$semanticHash] = $true }
}
foreach ($trace in $traces) {
    $candidatePath = Join-Path ([System.IO.Path]::GetFullPath($CandidateDirectory)) $trace.Name
    if (Test-Path -LiteralPath $candidatePath) {
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $candidatePath).Hash -ne (Get-FileHash -Algorithm SHA256 -LiteralPath $trace.FullName).Hash) {
            throw "Candidate name collision with different content: $candidatePath"
        }
    } else {
        Copy-Item -LiteralPath $trace.FullName -Destination $candidatePath
    }
    $replayOutput = & python $replay --require-exact $candidatePath
    $replayExitCode = $LASTEXITCODE
    $replayOutput | Out-Host
    $diagnosticPath = Join-Path ([System.IO.Path]::GetFullPath($FailureDirectory)) ($trace.BaseName + '.replay.json')
    $replayOutput | Set-Content -LiteralPath $diagnosticPath -Encoding utf8
    if ($replayExitCode -ne 0) {
        $failedPaths += $candidatePath
        continue
    }
    $candidateHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $candidatePath).Hash
    if ($certifiedHashes.ContainsKey($candidateHash)) {
        Remove-Item -LiteralPath $diagnosticPath -ErrorAction SilentlyContinue
        $duplicatePaths += $candidatePath
        continue
    }
    $candidateSemanticOutput = & python $inventoryScript --semantic-hashes-only $candidatePath
    if ($LASTEXITCODE -ne 0) { throw "Semantic inventory failed for $candidatePath" }
    $candidateSemanticHashes = @(($candidateSemanticOutput | ConvertFrom-Json).PSObject.Properties.Value)
    if ($candidateSemanticHashes.Count -gt 0 -and @($candidateSemanticHashes | Where-Object { -not $certifiedSemanticHashes.ContainsKey($_) }).Count -eq 0) {
        Remove-Item -LiteralPath $diagnosticPath -ErrorAction SilentlyContinue
        $duplicatePaths += $candidatePath
        continue
    }
    $certifiedPath = Join-Path ([System.IO.Path]::GetFullPath($CertifiedDirectory)) $trace.Name
    if (Test-Path -LiteralPath $certifiedPath) {
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $certifiedPath).Hash -ne (Get-FileHash -Algorithm SHA256 -LiteralPath $candidatePath).Hash) {
            throw "Certified name collision with different content: $certifiedPath"
        }
    } else {
        $temporaryCertifiedPath = "$certifiedPath.tmp-$PID"
        Copy-Item -LiteralPath $candidatePath -Destination $temporaryCertifiedPath
        Move-Item -LiteralPath $temporaryCertifiedPath -Destination $certifiedPath
    }
    Remove-Item -LiteralPath $diagnosticPath -ErrorAction SilentlyContinue
    $certifiedHashes[$candidateHash] = $certifiedPath
    foreach ($semanticHash in $candidateSemanticHashes) { $certifiedSemanticHashes[$semanticHash] = $true }
    $certifiedPaths += $certifiedPath
}
$result = [pscustomobject]@{
    format_version = 1
    seed = $Seed
    requested_trace_count = $CombatCount
    policy = $Policy
    captured_trace_count = $traces.Count
    certified_trace_count = $certifiedPaths.Count
    failed_trace_count = $failedPaths.Count
    duplicate_or_semantically_covered_trace_count = $duplicatePaths.Count
    launch_failure = $launchFailure
    certified_paths = $certifiedPaths
    failed_candidate_paths = $failedPaths
    duplicate_candidate_paths = $duplicatePaths
}
$result | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $sandboxFull 'native-sim-autotrace-result.json') -Encoding utf8
$result | ConvertTo-Json -Depth 5 | Out-Host
if ($launchFailure) { throw "$launchFailure Captured traces were still quarantined and replayed." }
if ($traces.Count -ne $CombatCount) { throw "Expected $CombatCount isolated traces, found $($traces.Count) under $traceDirectory; captured traces were still quarantined and replayed." }
if ($failedPaths.Count -gt 0) { throw "$($failedPaths.Count) shipped trace(s) failed exact replay and remain non-certifying candidates with diagnostics under $FailureDirectory" }
$certifiedPaths
