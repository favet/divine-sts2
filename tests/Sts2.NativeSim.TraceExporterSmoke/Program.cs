using System.Reflection;
using System.Runtime.InteropServices;
using HarmonyLib;
using MegaCrit.Sts2.Core.Combat;
using Sts2.NativeSim.TraceExporter;

if (OperatingSystem.IsWindows()) NativeSimWindowsErrorMode.SetErrorMode(0x8003);

Environment.SetEnvironmentVariable("STS2_NATIVE_TRACE", null);
TraceExporterMod.Initialize();
MethodInfo target = typeof(CombatManager).GetMethod(nameof(CombatManager.StartCombatInternal))!;
if (Harmony.GetPatchInfo(target)?.Owners.Contains("sts2-native-sim.trace-exporter") == true)
    throw new InvalidOperationException("Exporter patched the game while opt-in was disabled.");

Environment.SetEnvironmentVariable("STS2_NATIVE_TRACE", "1");
TraceExporterMod.Initialize();
if (Harmony.GetPatchInfo(target)?.Owners.Contains("sts2-native-sim.trace-exporter") != true)
    throw new InvalidOperationException("Exporter did not install its combat-start observer when opted in.");

Console.WriteLine("trace exporter opt-in and Harmony patch smoke test: ok");

file static class NativeSimWindowsErrorMode
{
    [DllImport("kernel32.dll")]
    internal static extern uint SetErrorMode(uint mode);
}
