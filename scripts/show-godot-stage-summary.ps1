param(
    [string]$GameAssembly = '',
    [int]$Iterations = 1
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$repositoryRoot = Get-DivineRepositoryRoot
$GameAssembly = Get-DivineGameAssembly $GameAssembly
$projectPath = Join-Path $repositoryRoot 'src\Sts2.NativeSim.GodotHost'
$projectFile = Join-Path $projectPath 'Sts2.NativeSim.GodotHost.csproj'
$godot = Get-DivineGodot
$dotnet = Get-DivineDotnet

& $dotnet build $projectFile -c Debug --nologo | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Godot host build failed with exit code $LASTEXITCODE"
}

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
$stdoutTask = $process.StandardOutput.ReadToEndAsync()
$stderrTask = $process.StandardError.ReadToEndAsync()
$process.WaitForExit()
$stdout = $stdoutTask.GetAwaiter().GetResult()
$stderr = $stderrTask.GetAwaiter().GetResult()
$match = [regex]::Match($stdout, 'NATIVE_SIM_REPORT_BEGIN\s*(\{.*\})\s*NATIVE_SIM_REPORT_END', 'Singleline')
if (-not $match.Success) {
    throw "NativeSim report markers were not found (exit $($process.ExitCode)).`n$stderr"
}

$report = $match.Groups[1].Value | ConvertFrom-Json -Depth 100
[ordered]@{
    exit_code = $process.ExitCode
    all_required_stages_passed = $report.AllRequiredStagesPassed
    stages = @($report.Stages | Where-Object Name -in @(
        'initialize_run_manager_services',
        'construct_native_combat',
        'execute_full_play_card_action',
        'execute_native_turn_cycle',
        'extract_next_turn_observation',
        'benchmark_reconstruct_step_observe'
    ) | Select-Object Name, Success, Detail, FailureType, FailureMessage)
    native_turn_cycle = $report.Facts.native_turn_cycle
    next_turn = $report.Facts.canonical_observation_next_turn
} | ConvertTo-Json -Depth 20
