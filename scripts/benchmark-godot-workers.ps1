param(
    [string]$GameAssembly = '',
    [ValidateRange(1, 64)][int]$Workers = 4,
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

$running = @()
$wallClock = [Diagnostics.Stopwatch]::StartNew()
for ($worker = 0; $worker -lt $Workers; $worker++) {
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $godot
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    foreach ($argument in @('--headless', '--path', $projectPath, '--', $GameAssembly, $Iterations.ToString())) {
        $startInfo.ArgumentList.Add($argument)
    }
    $process = [Diagnostics.Process]::Start($startInfo)
    $running += [pscustomobject]@{
        Process = $process
        Stdout = $process.StandardOutput.ReadToEndAsync()
        Stderr = $process.StandardError.ReadToEndAsync()
    }
}

$reports = @()
foreach ($worker in $running) {
    $worker.Process.WaitForExit()
    $stdout = $worker.Stdout.GetAwaiter().GetResult()
    $stderr = $worker.Stderr.GetAwaiter().GetResult()
    if ($worker.Process.ExitCode -ne 0) {
        throw "NativeSim worker exited $($worker.Process.ExitCode)`n$stdout`n$stderr"
    }
    $match = [regex]::Match($stdout, 'NATIVE_SIM_REPORT_BEGIN\s*(\{.*\})\s*NATIVE_SIM_REPORT_END', 'Singleline')
    if (-not $match.Success) {
        throw "NativeSim report markers were not found.`n$stdout`n$stderr"
    }
    $reports += $match.Groups[1].Value | ConvertFrom-Json -Depth 100
}
$wallClock.Stop()

$cycleRates = @($reports | ForEach-Object { $_.Facts.reconstruct_step_observe_benchmark.throughput_per_second })
$checksums = @($reports | ForEach-Object { $_.Facts.reconstruct_step_observe_benchmark.checksum })
$result = [ordered]@{
    success = -not ($reports.AllRequiredStagesPassed -contains $false) -and (($checksums | Select-Object -Unique).Count -eq 1)
    workers = $Workers
    logical_processors = [Environment]::ProcessorCount
    cycles_per_worker = [int]($Iterations / 1000)
    aggregate_internal_cycles_per_second = ($cycleRates | Measure-Object -Sum).Sum
    minimum_worker_cycles_per_second = ($cycleRates | Measure-Object -Minimum).Minimum
    maximum_worker_cycles_per_second = ($cycleRates | Measure-Object -Maximum).Maximum
    process_wall_seconds = $wallClock.Elapsed.TotalSeconds
    deterministic_checksum = $checksums[0]
}
$result | ConvertTo-Json
if (-not $result.success) {
    throw 'Parallel NativeSim workers failed or diverged.'
}
