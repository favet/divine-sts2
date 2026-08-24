namespace Sts2.NativeSim.Core;

public sealed record ProbeStage(
    string Name,
    bool Success,
    double ElapsedMilliseconds,
    string? Evidence = null,
    string? FailureType = null,
    string? FailureMessage = null,
    string? FailureStack = null);

public sealed record NativeFeasibilityReport(
    string TimestampUtc,
    string AssemblyPath,
    string Sha256,
    string ProductVersion,
    long AssemblyLength,
    IReadOnlyList<ProbeStage> Stages,
    IReadOnlyDictionary<string, object?> Facts)
{
    public bool AllRequiredStagesPassed => Stages.All(stage => stage.Success);
}
