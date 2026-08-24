using System.Text.Json;
using System.Runtime.InteropServices;
using System.Reflection;
using Sts2.NativeSim.Core;

string assemblyPath = args.FirstOrDefault()
    ?? Environment.GetEnvironmentVariable("STS2_ASSEMBLY")
    ?? throw new ArgumentException("Usage: Sts2.NativeSim.Host <path-to-sts2.dll> [iterations]");
int iterations = args.Length > 1 && int.TryParse(args[1], out int parsedIterations) ? parsedIterations : 100_000;
bool attemptUnsafeAction = args.Contains("--unsafe-action", StringComparer.Ordinal);

NativeFeasibilityReport report = await new NativeFeasibilityProbe().RunAsync(assemblyPath, iterations, attemptUnsafeAction);
Console.WriteLine(JsonSerializer.Serialize(report, new JsonSerializerOptions { WriteIndented = true }));
return report.AllRequiredStagesPassed ? 0 : 1;

file static class NativeSimWindowsErrorMode
{
    [DllImport("kernel32.dll")]
    internal static extern uint SetErrorMode(uint mode);
}
