param(
    [string]$GameAssembly = '',
    [int]$Iterations = 1000
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

function Invoke-NativeSimWorker {
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
    if ($process.ExitCode -ne 0) {
        throw "NativeSim worker exited $($process.ExitCode)`n$stdout`n$stderr"
    }

    $match = [regex]::Match($stdout, 'NATIVE_SIM_REPORT_BEGIN\s*(\{.*\})\s*NATIVE_SIM_REPORT_END', 'Singleline')
    if (-not $match.Success) {
        throw "NativeSim report markers were not found.`n$stdout`n$stderr"
    }
    return $match.Groups[1].Value | ConvertFrom-Json -Depth 100
}

$first = Invoke-NativeSimWorker
$second = Invoke-NativeSimWorker
$checks = [ordered]@{
    all_stages_passed = $first.AllRequiredStagesPassed -and $second.AllRequiredStagesPassed
    assembly_sha = $first.Sha256 -eq $second.Sha256
    initial_hash = $first.Facts.canonical_observation_initial.state_hash -eq $second.Facts.canonical_observation_initial.state_hash
    post_action_hash = $first.Facts.canonical_observation_post_action.state_hash -eq $second.Facts.canonical_observation_post_action.state_hash
    next_turn_hash = $first.Facts.canonical_observation_next_turn.state_hash -eq $second.Facts.canonical_observation_next_turn.state_hash
    initial_actions = (($first.Facts.canonical_observation_initial.legal_actions.action_id -join ',') -eq ($second.Facts.canonical_observation_initial.legal_actions.action_id -join ','))
    post_action_actions = (($first.Facts.canonical_observation_post_action.legal_actions.action_id -join ',') -eq ($second.Facts.canonical_observation_post_action.legal_actions.action_id -join ','))
    next_turn_actions = (($first.Facts.canonical_observation_next_turn.legal_actions.action_id -join ',') -eq ($second.Facts.canonical_observation_next_turn.legal_actions.action_id -join ','))
    full_transition = (($first.Facts.full_play_card_action | ConvertTo-Json -Compress) -eq ($second.Facts.full_play_card_action | ConvertTo-Json -Compress))
    full_turn_cycle = (($first.Facts.native_turn_cycle | ConvertTo-Json -Compress) -eq ($second.Facts.native_turn_cycle | ConvertTo-Json -Compress))
    reconstruct_step_observe_checksum = $first.Facts.reconstruct_step_observe_benchmark.checksum -eq $second.Facts.reconstruct_step_observe_benchmark.checksum
    rng_prefix = (($first.Facts.rng_prefix -join ',') -eq ($second.Facts.rng_prefix -join ','))
    rng_checksum = $first.Facts.rng_benchmark.checksum -eq $second.Facts.rng_benchmark.checksum
}

$result = [ordered]@{
    success = -not ($checks.Values -contains $false)
    checks = $checks
    initial_state_hash = $first.Facts.canonical_observation_initial.state_hash
    post_action_state_hash = $first.Facts.canonical_observation_post_action.state_hash
    next_turn_state_hash = $first.Facts.canonical_observation_next_turn.state_hash
    initial_legal_actions = $first.Facts.canonical_observation_initial.legal_actions.action_id
    post_action_legal_actions = $first.Facts.canonical_observation_post_action.legal_actions.action_id
    next_turn_legal_actions = $first.Facts.canonical_observation_next_turn.legal_actions.action_id
}
$result | ConvertTo-Json -Depth 10
if (-not $result.success) {
    throw 'Independent NativeSim workers diverged.'
}
