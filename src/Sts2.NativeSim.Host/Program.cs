using System.Text.Json;
using System.Text.Json.Serialization;
using System.Collections;
using System.Reflection;
using System.Runtime.Loader;
using System.Runtime.InteropServices;
using Sts2.NativeSim.Core;
using Sts2.NativeSim.Protocol;

const uint ErrorMode = 0x0001 | 0x0002 | 0x8000;
JsonSerializerOptions Json = new() { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower, DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull };

if (OperatingSystem.IsWindows()) NativeSimWindowsErrorMode.SetErrorMode(ErrorMode);
int exitCode = 1;

try
{
    bool server = args.Contains("--server", StringComparer.Ordinal);
    bool traceExporterSmoke = args.Contains("--trace-exporter-smoke", StringComparer.Ordinal);
    bool optionChoiceAcceptance = args.Contains("--option-choice-acceptance", StringComparer.Ordinal);
    string? configuredAssembly = Environment.GetEnvironmentVariable("STS2_ASSEMBLY");
    string assemblyPath = args.FirstOrDefault(x => !x.StartsWith("--", StringComparison.Ordinal))
        ?? configuredAssembly
        ?? throw new ArgumentException("Pass the sts2.dll path or set STS2_ASSEMBLY.");
    
    string gameRoot = Directory.GetParent(Path.GetDirectoryName(assemblyPath)!)!.FullName;
    string pckPath = Path.Combine(gameRoot, "SlayTheSpire2.pck");

    if (traceExporterSmoke)
    {
        string exporterPath = args.Single(x => x.EndsWith("trace-exporter.dll", StringComparison.OrdinalIgnoreCase));
        exitCode = await RunTraceExporterSmokeAsync(assemblyPath, pckPath, exporterPath);
    }
    else if (optionChoiceAcceptance)
    {
        exitCode = await RunOptionChoiceAcceptanceAsync(assemblyPath, pckPath);
    }
    else if (server)
    {
        using PersistentNativeCombatEnvironment environment = new(assemblyPath, pckPath);
        exitCode = await ServeAsync(environment);
    }
    else
    {
        int iterations = args.Length > 1 && int.TryParse(args[1], out int parsed) ? parsed : 100_000;
        bool attemptUnsafeAction = args.Contains("--unsafe-action", StringComparer.Ordinal);
        NativeFeasibilityReport report = await new NativeFeasibilityProbe().RunAsync(assemblyPath, iterations, attemptUnsafeAction);
        Console.WriteLine("NATIVE_SIM_REPORT_BEGIN");
        Console.WriteLine(JsonSerializer.Serialize(report, new JsonSerializerOptions { WriteIndented = true }));
        Console.WriteLine("NATIVE_SIM_REPORT_END");
        exitCode = report.AllRequiredStagesPassed ? 0 : 1;
    }
}
catch (Exception exception)
{
    Console.Error.WriteLine($"NATIVE_SIM_HOST_FAILURE\n{exception}");
}

return exitCode;

async Task<int> RunOptionChoiceAcceptanceAsync(string assemblyPath, string pckPath)
{
    using PersistentNativeCombatEnvironment environment = new(assemblyPath, pckPath);
    CardSpec[] deck = Enumerable.Range(0, 5).Select(i => new CardSpec($"strike-{i}", "STRIKE_IRONCLAD"))
        .Concat(Enumerable.Range(0, 5).Select(i => new CardSpec($"defend-{i}", "DEFEND_IRONCLAD"))).ToArray();
    ResetRequest request = new(new(), "NATIVE-OPTION-CHOICE", new Dictionary<string, int>(), "IRONCLAD", 0, "first", 80, 80, deck, ["strike-0"], [], [], 99);
    EnvironmentResult initial = environment.Reset(request);

    Type environmentType = typeof(PersistentNativeCombatEnvironment);
    MethodInfo mutable = environmentType.GetMethod("Mutable", BindingFlags.NonPublic | BindingFlags.Instance)!;
    MethodInfo startTransition = environmentType.GetMethod("StartTransitionAsync", BindingFlags.NonPublic | BindingFlags.Instance)!;
    async Task<(EnvironmentResult Pending, object Player)> BeginChoiceAsync()
    {
        object currentPlayer = environmentType.GetField("_player", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(environment)!;
        object currentRelic = mutable.Invoke(environment, ["AllRelics", "SCROLL_BOXES"])!;
        currentPlayer.GetType().GetMethod("AddRelicInternal")!.Invoke(currentPlayer, [currentRelic, -1, true]);
        Func<Task> obtain = () => (Task)currentRelic.GetType().GetMethod("AfterObtained")!.Invoke(currentRelic, null)!;
        await (Task)startTransition.Invoke(environment, [obtain])!;
        return (environment.Observe(), currentPlayer);
    }

    (EnvironmentResult pending, object player) = await BeginChoiceAsync();
    LegalAction[] options = pending.LegalActions.Where(action => action.Kind == "choose_option").ToArray();
    if (options.Length != 2) throw new InvalidOperationException($"Expected two native Scroll Boxes bundle choices, obtained {options.Length}.");
    using JsonDocument pendingJson = JsonDocument.Parse(JsonSerializer.Serialize(pending.Observation, Json));
    JsonElement choice = pendingJson.RootElement.GetProperty("outstanding_choice");
    if (choice.GetProperty("kind").GetString() != "choose_option" || choice.GetProperty("options").GetArrayLength() != 2)
        throw new InvalidOperationException("Native option choice snapshot was incomplete.");

    EnvironmentResult resolved = await environment.StepAsync(options[0].ActionId);
    object runDeck = player.GetType().GetProperty("Deck")!.GetValue(player)!;
    int finalDeckCount = ((IEnumerable)runDeck.GetType().GetProperty("Cards")!.GetValue(runDeck)!).Cast<object>().Count();
    if (resolved.LegalActions.Any(action => action.Kind == "choose_option") || finalDeckCount != 13)
        throw new InvalidOperationException($"Native bundle continuation did not add exactly three cards; combat card count is {finalDeckCount}.");

    await environment.RestoreAsync(initial.StateHandle);
    (EnvironmentResult reconstructedPending, object reconstructedPlayer) = await BeginChoiceAsync();
    string[] reconstructedOptions = reconstructedPending.LegalActions.Where(action => action.Kind == "choose_option").Select(action => action.ActionId).ToArray();
    if (!options.Select(action => action.ActionId).SequenceEqual(reconstructedOptions, StringComparer.Ordinal))
        throw new InvalidOperationException("Native option actions changed after reconstructing the owning run.");
    await environment.StepAsync(reconstructedOptions[0]);
    object reconstructedDeck = reconstructedPlayer.GetType().GetProperty("Deck")!.GetValue(reconstructedPlayer)!;
    int reconstructedDeckCount = ((IEnumerable)reconstructedDeck.GetType().GetProperty("Cards")!.GetValue(reconstructedDeck)!).Cast<object>().Count();
    if (reconstructedDeckCount != 13) throw new InvalidOperationException("Reconstructed native option continuation did not resume against its owning run.");

    object[] relicCandidates = [mutable.Invoke(environment, ["AllRelics", "ANCHOR"])!, mutable.Invoke(environment, ["AllRelics", "BAG_OF_PREPARATION"])!];
    Type relicType = relicCandidates[0].GetType().BaseType!;
    while (relicType.BaseType is not null && relicType.BaseType.Name != "AbstractModel") relicType = relicType.BaseType;
    IList typedRelics = (IList)Activator.CreateInstance(typeof(List<>).MakeGenericType(relicType))!;
    foreach (object candidate in relicCandidates) typedRelics.Add(candidate);
    MethodInfo selectRelic = environmentType.Assembly == null
        ? throw new InvalidOperationException()
        : relicCandidates[0].GetType().Assembly.GetType("MegaCrit.Sts2.Core.Commands.RelicSelectCmd", true)!.GetMethod("FromChooseARelicScreen")!;
    int selectedRelicIndex = -1;
    Func<Task> relicContinuation = async () =>
    {
        Task selection = (Task)selectRelic.Invoke(null, [reconstructedPlayer, typedRelics])!;
        await selection;
        object? selected = selection.GetType().GetProperty("Result")!.GetValue(selection);
        selectedRelicIndex = Array.IndexOf(relicCandidates, selected);
    };
    await (Task)startTransition.Invoke(environment, [relicContinuation])!;
    EnvironmentResult relicPending = environment.Observe();
    LegalAction[] relicActions = relicPending.LegalActions.Where(action => action.Kind == "choose_option").ToArray();
    if (relicActions.Length != 3) throw new InvalidOperationException($"Expected two native relic options plus Skip, obtained {relicActions.Length} actions.");
    LegalAction firstRelic = relicActions.Single(action => ((string[])action.Parameters["option_ids"]!).SingleOrDefault()?.EndsWith("-option-0", StringComparison.Ordinal) == true);
    await environment.StepAsync(firstRelic.ActionId);
    if (selectedRelicIndex != 0) throw new InvalidOperationException("Native relic-option continuation did not receive the selected shipped relic instance.");
    Console.WriteLine(JsonSerializer.Serialize(new { success = true, bundle_choices = options.Length, relic_actions = relicActions.Length, final_card_count = finalDeckCount, reconstructed_card_count = reconstructedDeckCount }, Json));
    return 0;
}

async Task<int> RunTraceExporterSmokeAsync(string assemblyPath, string pckPath, string exporterPath)
{
    string appData = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
    string traceDirectory = Path.Combine(appData, "SlayTheSpire2", "native_sim_traces");
    HashSet<string> before = Directory.Exists(traceDirectory) ? Directory.GetFiles(traceDirectory, "*.jsonl").ToHashSet(StringComparer.OrdinalIgnoreCase) : [];
    Environment.SetEnvironmentVariable("STS2_NATIVE_TRACE", "1");
    Environment.SetEnvironmentVariable("STS2_NATIVE_TRACE_SOURCE", "simulator_self_smoke");
    using PersistentNativeCombatEnvironment environment = new(assemblyPath, pckPath);
    Assembly exporter = (AssemblyLoadContext.GetLoadContext(typeof(Program).Assembly) ?? AssemblyLoadContext.Default).LoadFromAssemblyPath(Path.GetFullPath(exporterPath));
    exporter.GetType("Sts2.NativeSim.TraceExporter.TraceExporterMod", true)!.GetMethod("Initialize", BindingFlags.Public | BindingFlags.Static)!.Invoke(null, null);

    CardSpec[] deck = Enumerable.Range(0, 5).Select(i => new CardSpec($"strike-{i}", "STRIKE_IRONCLAD"))
        .Concat(Enumerable.Range(0, 5).Select(i => new CardSpec($"defend-{i}", "DEFEND_IRONCLAD"))).ToArray();
    ResetRequest request = new(new(), "TRACE-EXPORTER-SMOKE", new Dictionary<string, int>(), "IRONCLAD", 0, "first", 80, 80, deck, ["strike-0"], [], [], 99);
    EnvironmentResult initial = environment.Reset(request);
    object combat = typeof(PersistentNativeCombatEnvironment).GetField("_combat", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(environment)!;
    Type exporterType = exporter.GetType("Sts2.NativeSim.TraceExporter.NativeTraceExporter", true)!;
    exporterType.GetMethod("Attach", BindingFlags.Public | BindingFlags.Static)!.Invoke(null, [combat]);
    exporterType.GetMethod("OnTurnStarted", BindingFlags.NonPublic | BindingFlags.Static)!.Invoke(null, [combat]);

    object player = ((IEnumerable)combat.GetType().GetProperty("Players")!.GetValue(combat)!).Cast<object>().Single();
    object pcs = player.GetType().GetProperty("PlayerCombatState")!.GetValue(player)!;
    object hand = pcs.GetType().GetProperty("Hand")!.GetValue(pcs)!;
    object card = ((IEnumerable)hand.GetType().GetProperty("Cards")!.GetValue(hand)!).Cast<object>().Single();
    object target = ((IEnumerable)combat.GetType().GetProperty("Enemies")!.GetValue(combat)!).Cast<object>().First();
    Type playType = combat.GetType().Assembly.GetType("MegaCrit.Sts2.Core.GameActions.PlayCardAction", true)!;
    object nativeAction = Activator.CreateInstance(playType, [card, target])!;
    exporterType.GetMethod("BeforeAction", BindingFlags.NonPublic | BindingFlags.Static)!.Invoke(null, [nativeAction]);
    string actionId = initial.LegalActions.First(x => x.Kind == "play_card").ActionId;
    await environment.StepAsync(actionId);
    exporterType.GetMethod("AfterAction", BindingFlags.NonPublic | BindingFlags.Static)!.Invoke(null, [nativeAction]);
    exporterType.GetMethod("Close", BindingFlags.NonPublic | BindingFlags.Static)!.Invoke(null, null);
    Environment.SetEnvironmentVariable("STS2_NATIVE_TRACE", null);
    Environment.SetEnvironmentVariable("STS2_NATIVE_TRACE_SOURCE", null);

    string trace = Directory.GetFiles(traceDirectory, "*.jsonl").Where(x => !before.Contains(x)).Single();
    string[] lines = File.ReadAllLines(trace);
    if (lines.Length != 3) throw new InvalidOperationException($"Expected header plus two checkpoints, obtained {lines.Length} records.");
    using JsonDocument header = JsonDocument.Parse(lines[0]);
    using JsonDocument first = JsonDocument.Parse(lines[1]);
    using JsonDocument second = JsonDocument.Parse(lines[2]);
    if (header.RootElement.GetProperty("comparison").GetString() != "exact"
        || header.RootElement.GetProperty("source").GetString() != "simulator_self_smoke"
        || first.RootElement.GetProperty("sequence").GetInt32() != 0 || second.RootElement.GetProperty("sequence").GetInt32() != 1)
        throw new InvalidOperationException("Trace records did not follow the differential format.");
    JsonElement observation = first.RootElement.GetProperty("observation");
    JsonElement combatSnapshot = observation.GetProperty("combat");
    JsonElement firstCard = combatSnapshot.GetProperty("piles")[0].GetProperty("cards")[0];
    JsonElement enemy = combatSnapshot.GetProperty("creatures")[1];
    JsonElement legalAction = observation.GetProperty("decision").GetProperty("legal_actions")[0];
    if (observation.GetProperty("schema_version").GetInt32() != ProtocolConstants.ObservationSchemaVersion
        || !observation.TryGetProperty("game_build", out _)
        || !firstCard.TryGetProperty("net_id", out _)
        || !firstCard.TryGetProperty("energy_cost", out _)
        || !firstCard.TryGetProperty("target_type", out _)
        || !enemy.TryGetProperty("next_move", out _)
        || !enemy.TryGetProperty("powers", out _)
        || legalAction.GetProperty("kind").GetString() != "play_card")
        throw new InvalidOperationException("Trace omitted an expanded canonical projection field.");
    Console.WriteLine(JsonSerializer.Serialize(new { success = true, certifying = false, trace, records = lines.Length }, Json));
    return 0;
}

async Task<int> ServeAsync(PersistentNativeCombatEnvironment environment)
{
    while (await Console.In.ReadLineAsync() is { } line)
    {
        RpcRequest? request = null; RpcResponse response;
        try
        {
            request = JsonSerializer.Deserialize<RpcRequest>(line, Json) ?? throw new ProtocolException("invalid_request", "Empty request.");
            object? result = request.Method switch
            {
                "hello" => environment.Hello(),
                "catalog" => environment.Catalog(),
                "reset" => environment.Reset(Read<ResetRequest>(request.Parameters)),
                "run_reset" => environment.RunReset(Read<ResetRequest>(request.Parameters)),
                "map_reset" => environment.MapReset(Read<ResetRequest>(request.Parameters)),
                "reward_reset" => environment.RewardReset(Read<ResetRequest>(request.Parameters)),
                "item_reward_reset" => environment.ItemRewardReset(Read<ItemRewardResetRequest>(request.Parameters)),
                "custom_reward_reset" => await environment.CustomRewardResetAsync(Read<CustomRewardResetRequest>(request.Parameters)),
                "rest_reset" => environment.RestReset(Read<ResetRequest>(request.Parameters)),
                "event_reset" => await environment.EventResetAsync(Read<EventResetRequest>(request.Parameters)),
                "observe" => environment.Observe(),
                "run_observe" => environment.Observe(),
                "map_observe" => environment.Observe(),
                "reward_observe" => environment.Observe(),
                "rest_observe" => environment.Observe(),
                "event_observe" => environment.Observe(),
                "custom_reward_observe" => environment.Observe(),
                "legal_actions" => environment.LegalActions(),
                "step" => await environment.StepAsync(Read<StepRequest>(request.Parameters).ActionId),
                "run_step" => await environment.StepAsync(Read<StepRequest>(request.Parameters).ActionId),
                "map_step" => await environment.StepAsync(Read<StepRequest>(request.Parameters).ActionId),
                "reward_step" => await environment.StepAsync(Read<StepRequest>(request.Parameters).ActionId),
                "rest_step" => await environment.StepAsync(Read<StepRequest>(request.Parameters).ActionId),
                "event_step" => await environment.StepAsync(Read<StepRequest>(request.Parameters).ActionId),
                "custom_reward_step" => await environment.StepAsync(Read<StepRequest>(request.Parameters).ActionId),
                "fork" => new { state_handle = environment.Fork() },
                "restore" => await environment.RestoreAsync(Read<RestoreRequest>(request.Parameters).StateHandle),
                "diagnostics" => environment.Diagnostics(),
                "close" => new { closed = true },
                _ => throw new ProtocolException("unknown_method", request.Method)
            };
            response = new(request.Id, true, result);
        }
        catch (Exception raw)
        {
            Exception error = raw;
            while (error is System.Reflection.TargetInvocationException { InnerException: not null } invocation) error = invocation.InnerException!;
            response = error is ProtocolException protocol ? new(request?.Id ?? "", false, Error: new(protocol.Code, protocol.Message, protocol.Details)) : new(request?.Id ?? "", false, Error: new("internal_error", error.Message, new { type = error.GetType().FullName, stack = error.ToString() }));
            Console.Error.WriteLine(error);
        }
        Console.WriteLine(JsonSerializer.Serialize(response, Json)); Console.Out.Flush();
        if (request?.Method == "close" && response.Ok) return 0;
    }
    return 0;
}

T Read<T>(JsonElement element) => element.Deserialize<T>(Json) ?? throw new ProtocolException("invalid_params", typeof(T).Name);

file static class NativeSimWindowsErrorMode
{
    [DllImport("kernel32.dll")]
    internal static extern uint SetErrorMode(uint mode);
}
