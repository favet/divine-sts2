using System.Collections;
using System.Diagnostics;
using System.Linq.Expressions;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Security.Cryptography;
using System.Text.Json;

namespace Sts2.NativeSim.Core;

public sealed class NativeFeasibilityProbe
{
    private readonly List<ProbeStage> _stages = [];
    private readonly Dictionary<string, object?> _facts = new(StringComparer.Ordinal);
    private NativeAssemblyContext? _context;
    private object? _runState;
    private object? _player;
    private object? _combatState;
    private object? _strike;
    private object? _target;
    private object? _combatManager;
    private object? _playerCombat;
    private string? _initialStateHash;

    public async Task<NativeFeasibilityReport> RunAsync(string assemblyPath, int iterations = 100_000, bool attemptUnsafeAction = false)
    {
        string fullPath = Path.GetFullPath(assemblyPath);
        FileInfo file = new(fullPath);
        string sha256 = ReflectionTools.HashFile(fullPath);
        string productVersion = FileVersionInfo.GetVersionInfo(fullPath).ProductVersion ?? "unknown";

        RunStage("load_assembly", () =>
        {
            _context = new NativeAssemblyContext(fullPath);
            _facts["type_count"] = _context.Assembly.GetTypes().Length;
            return _context.Assembly.FullName;
        });
        RunStage("initialize_model_db", InitializeModelDb);
        RunStage("install_inert_save_manager", InstallInertSaveManager);
        if (attemptUnsafeAction)
            RunStage("initialize_localization", InitializeLocalization);
        if (attemptUnsafeAction)
            RunStage("install_headless_presentation_seam", InstallHeadlessPresentationSeam);
        RunStage("rng_determinism_and_clone", ProbeRngDeterminism);
        RunStage("construct_native_run", ConstructNativeRun);
        if (attemptUnsafeAction)
            RunStage("initialize_run_manager_services", InitializeRunManagerServices);
        RunStage("construct_native_combat", ConstructNativeCombat);
        RunStage("inspect_native_snapshot_surfaces", InspectNativeSnapshotSurfaces);
        RunStage("inspect_native_turn_surfaces", InspectNativeTurnSurfaces);
        if (attemptUnsafeAction)
            RunStage("initialize_card_action_services", InitializeCardActionServices);
        if (attemptUnsafeAction)
            RunStage("initialize_turn_coordinator_services", InitializeTurnCoordinatorServices);
        if (attemptUnsafeAction)
            RunStage("prepare_player_decision_state", PreparePlayerDecisionState);
        if (attemptUnsafeAction)
            RunStage("extract_canonical_observation", () => ExtractCanonicalObservation("initial", requirePlayableStrike: true));
        if (attemptUnsafeAction)
            await RunStageAsync("execute_native_strike", ExecuteNativeStrikeAsync);
        else
            RunStage("execute_native_strike", () => throw new GodotInitializationRequiredException(
                "Unsafe subprobe reached StrikeIronclad.OnPlay -> AttackCommand.Execute -> CreatureCmd.TriggerAnim -> Log.Error -> Logger.GetIsRunningFromGodotEditor -> Godot.OS.GetCmdlineArgs, which terminates a plain console process with 0xC0000005. A valid headless Godot engine context is required."));
        if (attemptUnsafeAction)
            await RunStageAsync("execute_full_play_card_action", ExecuteFullPlayCardActionAsync);
        if (attemptUnsafeAction)
            RunStage("extract_post_action_observation", () => ExtractCanonicalObservation("post_action", requirePlayableStrike: false));
        if (attemptUnsafeAction)
            await RunStageAsync("execute_native_turn_cycle", ExecuteNativeTurnCycleAsync);
        if (attemptUnsafeAction)
            RunStage("extract_next_turn_observation", () => ExtractCanonicalObservation("next_turn", requirePlayableStrike: false));
        if (attemptUnsafeAction)
            await RunStageAsync("benchmark_reconstruct_step_observe", () => BenchmarkReconstructStepObserveAsync(Math.Clamp(iterations / 1_000, 10, 250)));
        if (attemptUnsafeAction)
            await RunStageAsync("benchmark_native_strike_transition", () => BenchmarkNativeStrikeAsync(Math.Clamp(iterations / 10, 100, 10_000)));
        RunStage("serialize_and_clone", ProbeSerializationAndClone);
        RunStage("benchmark_native_rng_transition", () => BenchmarkRng(iterations));

        _context?.Dispose();
        return new NativeFeasibilityReport(
            DateTime.UtcNow.ToString("O"), fullPath, sha256, productVersion, file.Length, _stages, _facts);
    }

    private string InitializeModelDb()
    {
        Type modManager = Require("MegaCrit.Sts2.Core.Modding.ModManager");
        Type modManagerState = Require("MegaCrit.Sts2.Core.Modding.ModManagerState");
        ReflectionTools.SetStatic(modManager, "State", Enum.Parse(modManagerState, "Skipped"));
        Type modelDb = Require("MegaCrit.Sts2.Core.Models.ModelDb");
        ReflectionTools.InvokeStatic(modelDb, "Init");
        int cards = ReflectionTools.Enumerate(ReflectionTools.GetStatic(modelDb, "AllCards")).Count;
        int encounters = ReflectionTools.Enumerate(ReflectionTools.GetStatic(modelDb, "AllEncounters")).Count;
        int relics = ReflectionTools.Enumerate(ReflectionTools.GetStatic(modelDb, "AllRelics")).Count;
        _facts["model_counts"] = new { cards, encounters, relics };
        return $"cards={cards}; encounters={encounters}; relics={relics}";
    }

    private string InitializeLocalization()
    {
        Type locManagerType = Require("MegaCrit.Sts2.Core.Localization.LocManager");
        ReflectionTools.InvokeStatic(locManagerType, "Initialize");
        object locManager = ReflectionTools.GetStatic(locManagerType, "Instance")!;
        string language = (string)ReflectionTools.Get(locManager, "Language")!;
        int languageCount = ReflectionTools.Enumerate(ReflectionTools.Get(locManager, "Languages")).Count;
        _facts["localization"] = new { language, language_count = languageCount };
        return $"language={language}; languages={languageCount}";
    }

    private string InstallHeadlessPresentationSeam()
    {
        NativeAssemblyContext context = _context ?? throw new InvalidOperationException("Assembly is not loaded.");
        Assembly harmonyAssembly = context.LoadDependency("0Harmony.dll");
        Type harmonyType = harmonyAssembly.GetType("HarmonyLib.Harmony", throwOnError: true)!;
        Type harmonyMethodType = harmonyAssembly.GetType("HarmonyLib.HarmonyMethod", throwOnError: true)!;
        object harmony = Activator.CreateInstance(harmonyType, "sts2.native-sim.headless-presentation")!;
        MethodInfo patchMethod = harmonyType.GetMethods(BindingFlags.Public | BindingFlags.Instance)
            .Single(method => method.Name == "Patch" && method.GetParameters().Length == 5);

        int patched = 0;
        MethodInfo triggerAnimation = Require("MegaCrit.Sts2.Core.Commands.CreatureCmd")
            .GetMethod("TriggerAnim", BindingFlags.Public | BindingFlags.Static)
            ?? throw new MissingMethodException("CreatureCmd.TriggerAnim");
        Patch(triggerAnimation, nameof(SkipTask));

        foreach (MethodInfo audioMethod in Require("MegaCrit.Sts2.Core.Commands.SfxCmd")
                     .GetMethods(BindingFlags.Public | BindingFlags.Static | BindingFlags.DeclaredOnly))
        {
            if (audioMethod.ReturnType != typeof(void))
                throw new InvalidOperationException($"Unexpected state-bearing SfxCmd method: {audioMethod}");
            Patch(audioMethod, nameof(SkipVoid));
        }

        foreach (MethodInfo thoughtMethod in Require("MegaCrit.Sts2.Core.Commands.ThinkCmd")
                     .GetMethods(BindingFlags.Public | BindingFlags.Static | BindingFlags.DeclaredOnly))
        {
            if (thoughtMethod.ReturnType != typeof(void))
                throw new InvalidOperationException($"Unexpected state-bearing ThinkCmd method: {thoughtMethod}");
            Patch(thoughtMethod, nameof(SkipVoid));
        }

        foreach (MethodInfo infoMethod in Require("MegaCrit.Sts2.Core.Logging.Log")
                     .GetMethods(BindingFlags.Public | BindingFlags.Static | BindingFlags.DeclaredOnly)
                     .Where(method => method.Name == "Info"))
        {
            if (infoMethod.ReturnType != typeof(void))
                throw new InvalidOperationException($"Unexpected state-bearing Log.Info method: {infoMethod}");
            Patch(infoMethod, nameof(SkipVoid));
        }

        Type cardQueueType = Require("MegaCrit.Sts2.Core.Nodes.Combat.NCardPlayQueue");
        foreach (string methodName in new[]
                 {
                     "UpdateCardBeforeExecution",
                     "RemoveCardFromQueueForCancellation",
                     "RemoveCardFromQueueForExecution"
                 })
        {
            foreach (MethodInfo queueMethod in cardQueueType.GetMethods(BindingFlags.Public | BindingFlags.Instance | BindingFlags.DeclaredOnly)
                         .Where(method => method.Name == methodName))
                Patch(queueMethod, nameof(SkipVoid));
        }

        Type cardPileCmdType = Require("MegaCrit.Sts2.Core.Commands.CardPileCmd");
        foreach (MethodInfo pileMethod in cardPileCmdType.GetMethods(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static | BindingFlags.DeclaredOnly)
                     .Where(method => method.Name is "Add" or "RemoveFromCombat")
                     .Where(method => method.GetParameters().Any(parameter => parameter.Name == "skipVisuals")))
            Patch(pileMethod, nameof(ForceSkipVisuals));
        Patch(cardPileCmdType.GetMethod("AddDuringManualCardPlay", BindingFlags.Public | BindingFlags.Static)!, nameof(HeadlessManualCardPlay));

        _facts["presentation_seam"] = new
        {
            patched_methods = patched,
            scope = "audio, animations, thought-bubble VFX, informational play logging, card-node queue bookkeeping, and card-pile visuals; warnings/errors and native pile mutations/hooks remain active"
        };
        return $"patched_methods={patched}; state-bearing native commands remain unmodified";

        void Patch(MethodInfo original, string prefixName)
        {
            MethodInfo prefix = typeof(NativeFeasibilityProbe).GetMethod(prefixName, BindingFlags.NonPublic | BindingFlags.Static)!;
            object harmonyMethod = Activator.CreateInstance(harmonyMethodType, prefix)!;
            patchMethod.Invoke(harmony, [original, harmonyMethod, null, null, null]);
            patched++;
        }
    }

    private static bool SkipTask(ref Task __result)
    {
        __result = Task.CompletedTask;
        return false;
    }

    private static bool SkipVoid() => false;

    private static void ForceSkipVisuals(ref bool skipVisuals) => skipVisuals = true;

    private static bool HeadlessManualCardPlay(object card, ref Task __result)
    {
        Assembly assembly = card.GetType().Assembly;
        Type cardPileCmd = assembly.GetType("MegaCrit.Sts2.Core.Commands.CardPileCmd", throwOnError: true)!;
        object playPile = Enum.Parse(assembly.GetType("MegaCrit.Sts2.Core.Entities.Cards.PileType", throwOnError: true)!, "Play");
        object top = Enum.Parse(assembly.GetType("MegaCrit.Sts2.Core.Entities.Cards.CardPilePosition", throwOnError: true)!, "Top");
        __result = (Task)ReflectionTools.InvokeStatic(cardPileCmd, "Add", card, playPile, top, null, true)!;
        return false;
    }

    private string ProbeRngDeterminism()
    {
        Type rngType = Require("MegaCrit.Sts2.Core.Random.Rng");
        object first = ReflectionTools.Create(rngType, (uint)0x5EED1234, 0);
        int[] prefix = Enumerable.Range(0, 8).Select(_ => (int)ReflectionTools.Invoke(first, "NextInt", 1_000_000)!).ToArray();
        int counter = (int)ReflectionTools.Get(first, "Counter")!;
        object clone = ReflectionTools.Create(rngType, (uint)0x5EED1234, counter);
        int[] left = Enumerable.Range(0, 32).Select(_ => (int)ReflectionTools.Invoke(first, "NextInt", 1_000_000)!).ToArray();
        int[] right = Enumerable.Range(0, 32).Select(_ => (int)ReflectionTools.Invoke(clone, "NextInt", 1_000_000)!).ToArray();
        if (!left.SequenceEqual(right))
            throw new InvalidOperationException("Native RNG clone diverged.");
        _facts["rng_prefix"] = prefix;
        _facts["rng_clone_counter"] = counter;
        return $"seed=0x5EED1234; counter={counter}; next32_exact=true";
    }

    private string ConstructNativeRun()
    {
        Type modelDb = Require("MegaCrit.Sts2.Core.Models.ModelDb");
        Type ironcladType = Require("MegaCrit.Sts2.Core.Models.Characters.Ironclad");
        object ironclad = ReflectionTools.Enumerate(ReflectionTools.GetStatic(modelDb, "AllCharacters"))
            .Single(character => character?.GetType() == ironcladType)!;
        object unlockState = ReflectionTools.Create(
            Require("MegaCrit.Sts2.Core.Unlocks.UnlockState"),
            new List<string>(),
            ToTypedList(Require("MegaCrit.Sts2.Core.Models.ModelId"), []),
            0);
        Type playerType = Require("MegaCrit.Sts2.Core.Entities.Players.Player");
        _player = playerType.GetMethods(BindingFlags.Public | BindingFlags.Static)
            .Single(method => method.Name == "CreateForNewRun" && method.GetParameters().Length == 3)
            .Invoke(null, [ironclad, unlockState, (ulong)1])!;

        Type actModel = Require("MegaCrit.Sts2.Core.Models.ActModel");
        object acts = ToTypedList(actModel, ReflectionTools.Enumerate(ReflectionTools.GetStatic(modelDb, "Acts")));
        object modifiers = ToTypedList(Require("MegaCrit.Sts2.Core.Models.ModifierModel"), []);
        Type gameMode = Require("MegaCrit.Sts2.Core.Runs.GameMode");
        object mode = Enum.GetValues(gameMode).GetValue(0)!;
        Type runState = Require("MegaCrit.Sts2.Core.Runs.RunState");
        _runState = runState.GetMethods(BindingFlags.Public | BindingFlags.Static)
            .Single(method => method.Name == "CreateForTest")
            .Invoke(null, [ToTypedList(playerType, [_player]), acts, modifiers, mode, 0, "NATIVESIMSPIKE"])!;
        _facts["character_type"] = ironclad.GetType().FullName;
        _facts["run_rng_seed"] = ReflectionTools.Get(ReflectionTools.Get(_runState, "Rng")!, "Seed");
        return $"character={ironclad.GetType().Name}; acts={ReflectionTools.Enumerate(acts).Count}; seed=NATIVESIMSPIKE";
    }

    private string InstallInertSaveManager()
    {
        Type saveManagerType = Require("MegaCrit.Sts2.Core.Saves.SaveManager");
        object saveManager = RuntimeHelpers.GetUninitializedObject(saveManagerType);

        Type settingsManagerType = Require("MegaCrit.Sts2.Core.Saves.Managers.SettingsSaveManager");
        object settingsManager = RuntimeHelpers.GetUninitializedObject(settingsManagerType);
        object settings = ReflectionTools.Create(Require("MegaCrit.Sts2.Core.Saves.SettingsSave"));
        ReflectionTools.Set(settings, "Language", "eng");
        ReflectionTools.Set(settingsManager, "Settings", settings);
        ReflectionTools.Set(saveManager, "_settingsSaveManager", settingsManager);

        Type prefsManagerType = Require("MegaCrit.Sts2.Core.Saves.Managers.PrefsSaveManager");
        object prefsManager = RuntimeHelpers.GetUninitializedObject(prefsManagerType);
        object prefs = ReflectionTools.Create(Require("MegaCrit.Sts2.Core.Saves.PrefsSave"));
        object instantMode = Enum.Parse(Require("MegaCrit.Sts2.Core.Settings.FastModeType"), "Instant");
        ReflectionTools.Set(prefs, "FastMode", instantMode);
        ReflectionTools.Set(prefsManager, "Prefs", prefs);
        ReflectionTools.Set(saveManager, "_prefsSaveManager", prefsManager);

        Type progressManagerType = Require("MegaCrit.Sts2.Core.Saves.Managers.ProgressSaveManager");
        object progressManager = RuntimeHelpers.GetUninitializedObject(progressManagerType);
        object progress = ReflectionTools.InvokeStatic(Require("MegaCrit.Sts2.Core.Saves.ProgressState"), "CreateDefault")!;
        ReflectionTools.Set(progressManager, "Progress", progress);
        ReflectionTools.Set(saveManager, "_progressSaveManager", progressManager);
        ReflectionTools.InvokeStatic(saveManagerType, "MockInstanceForTesting", saveManager);
        _facts["save_store"] = "uninitialized test mock with in-memory English SettingsSave, Instant PrefsSave, and default ProgressState; no constructor/profile/cloud paths";
        return "in-memory SettingsSave(language=eng), PrefsSave(fast=Instant), and default ProgressState; no persistent store";
    }

    private string InitializeRunManagerServices()
    {
        object runState = _runState ?? throw new InvalidOperationException("Native run prerequisite is unavailable.");
        object runManager = ReflectionTools.GetStatic(Require("MegaCrit.Sts2.Core.Runs.RunManager"), "Instance")!;
        if (ReflectionTools.Get(runManager, "State") is not null)
            throw new InvalidOperationException("RunManager already owns a run; the feasibility worker requires process isolation.");
        object netService = ReflectionTools.Create(Require("MegaCrit.Sts2.Core.Multiplayer.NetSingleplayerGameService"));
        ReflectionTools.Invoke(runManager, "SetUpTest", runState, netService, true, false);
        ReflectionTools.SetStatic(Require("MegaCrit.Sts2.Core.Context.LocalContext"), "NetId", (ulong?)1);
        _facts["run_manager_services"] = new
        {
            mode = "native SetUpTest with NetSingleplayerGameService",
            local_net_id = 1,
            combat_state_sync_disabled = true,
            saving = false
        };
        return "native test services initialized; mode=Singleplayer; local_net_id=1; combat_sync=disabled; saving=false";
    }

    private string ConstructNativeCombat()
    {
        object runState = _runState ?? throw new InvalidOperationException("Native run prerequisite is unavailable.");
        object player = _player ?? throw new InvalidOperationException("Native player prerequisite is unavailable.");
        Type modelDb = Require("MegaCrit.Sts2.Core.Models.ModelDb");
        IReadOnlyList<object?> encounters = ReflectionTools.Enumerate(ReflectionTools.GetStatic(modelDb, "AllEncounters"));
        object canonicalEncounter = encounters.First(item => item is not null)!;
        object encounter = ReflectionTools.Invoke(canonicalEncounter, "ToMutable")!;
        ReflectionTools.Invoke(encounter, "GenerateMonstersWithSlots", runState);

        object modifiers = ToTypedList(Require("MegaCrit.Sts2.Core.Models.ModifierModel"), []);
        object badges = ToTypedList(Require("MegaCrit.Sts2.Core.Models.BadgeModel"), []);
        object scaling = ReflectionTools.Get(runState, "MultiplayerScalingModel")!;
        _combatState = ReflectionTools.Create(Require("MegaCrit.Sts2.Core.Combat.CombatState"), encounter, runState, modifiers, badges, scaling);
        ReflectionTools.Invoke(_combatState, "AddPlayer", player);

        _combatManager = ReflectionTools.GetStatic(Require("MegaCrit.Sts2.Core.Combat.CombatManager"), "Instance")!;
        ReflectionTools.Set(_combatManager, "_state", _combatState);
        ReflectionTools.Set(_combatManager, "IsInProgress", true);
        _facts["combat_manager_type"] = _combatManager.GetType().FullName;
        ReflectionTools.Invoke(player, "ResetCombatState");
        object runRng = ReflectionTools.Get(runState, "Rng")!;
        object shuffleRng = ReflectionTools.Get(runRng, "Shuffle")!;
        ReflectionTools.Invoke(player, "PopulateCombatState", shuffleRng, _combatState);

        object enemySide = Enum.Parse(Require("MegaCrit.Sts2.Core.Combat.CombatSide"), "Enemy", ignoreCase: true);
        foreach (object? pair in ReflectionTools.Enumerate(ReflectionTools.Get(encounter, "MonstersWithSlots")))
        {
            if (pair is null) continue;
            object monster = ReflectionTools.Get(pair, "Item1")!;
            string slot = (string)ReflectionTools.Get(pair, "Item2")!;
            object creature = ReflectionTools.Invoke(_combatState, "CreateCreature", monster, enemySide, slot)!;
            ReflectionTools.Invoke(_combatState, "AddCreature", creature);
            ReflectionTools.Invoke(_combatManager, "AddCreature", creature);
            object? afterAddedTask = ReflectionTools.Invoke(_combatManager, "AfterCreatureAdded", creature);
            if (afterAddedTask is Task task) task.GetAwaiter().GetResult();
        }
        _target = ReflectionTools.Enumerate(ReflectionTools.Get(_combatState, "Enemies")).FirstOrDefault();
        if (_target is null) throw new InvalidOperationException("Encounter generated no enemy creature.");

        object canonicalStrike = ReflectionTools.Enumerate(ReflectionTools.GetStatic(modelDb, "AllCards"))
            .First(card => card?.GetType().FullName == "MegaCrit.Sts2.Core.Models.Cards.StrikeIronclad")!;
        _strike = ReflectionTools.Invoke(_combatState, "CreateCard", canonicalStrike, player)!;
        _playerCombat = ReflectionTools.Get(player, "PlayerCombatState")!;
        object hand = ReflectionTools.Get(_playerCombat, "Hand")!;
        int handCount = ReflectionTools.Enumerate(ReflectionTools.Get(hand, "Cards")).Count;
        ReflectionTools.Invoke(hand, "AddInternal", _strike, handCount, true);
        return $"encounter={encounter.GetType().Name}; enemies={ReflectionTools.Enumerate(ReflectionTools.Get(_combatState, "Enemies")).Count}; strike={_strike.GetType().Name}";
    }

    private string InitializeCardActionServices()
    {
        object player = _player ?? throw new InvalidOperationException("Native player prerequisite is unavailable.");
        object strike = _strike ?? throw new InvalidOperationException("Native Strike prerequisite is unavailable.");

        Type cardDbType = Require("MegaCrit.Sts2.Core.GameActions.Multiplayer.NetCombatCardDb");
        object cardDb = ReflectionTools.GetStatic(cardDbType, "Instance")!;
        ReflectionTools.Invoke(cardDb, "ClearCardsForTesting");
        object players = ToTypedList(Require("MegaCrit.Sts2.Core.Entities.Players.Player"), [player]);
        ReflectionTools.Invoke(cardDb, "StartCombat", players);
        uint strikeId = (uint)ReflectionTools.Invoke(cardDb, "GetCardId", strike)!;

        Type queueType = Require("MegaCrit.Sts2.Core.Nodes.Combat.NCardPlayQueue");
        object? queue = ReflectionTools.GetStatic(queueType, "Instance");

        _facts["card_action_services"] = new { strike_net_id = strikeId, visual_queue = queue is null ? "absent; callsites patched" : "native instance" };
        return $"net_card_id={strikeId}; NCardPlayQueue={(queue is null ? "absent-patched" : "native")}";
    }

    private string InspectNativeSnapshotSurfaces()
    {
        Assembly assembly = (_context ?? throw new InvalidOperationException("Assembly is not loaded.")).Assembly;
        static bool Relevant(string value) =>
            value.Contains("Save", StringComparison.OrdinalIgnoreCase) ||
            value.Contains("Serial", StringComparison.OrdinalIgnoreCase) ||
            value.Contains("Clone", StringComparison.OrdinalIgnoreCase) ||
            value.Contains("Copy", StringComparison.OrdinalIgnoreCase);
        static string Signature(MethodInfo method) =>
            $"{method.ReturnType.Name} {method.DeclaringType?.FullName}.{method.Name}({string.Join(',', method.GetParameters().Select(parameter => parameter.ParameterType.Name))})";

        string[] methodSurfaces = new[]
            {
                Require("MegaCrit.Sts2.Core.Combat.CombatState"),
                Require("MegaCrit.Sts2.Core.Runs.RunState"),
                Require("MegaCrit.Sts2.Core.Entities.Players.Player"),
                Require("MegaCrit.Sts2.Core.Entities.Players.PlayerCombatState")
            }
            .SelectMany(type => type.GetMethods(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static)
                .Where(method => Relevant(method.Name))
                .Select(Signature))
            .OrderBy(signature => signature, StringComparer.Ordinal)
            .ToArray();
        string[] candidateTypes = assembly.GetTypes()
            .Where(type => type.FullName is not null)
            .Where(type => type.FullName!.Contains("Combat", StringComparison.OrdinalIgnoreCase))
            .Where(type => Relevant(type.FullName!))
            .Select(type => type.FullName!)
            .OrderBy(name => name, StringComparer.Ordinal)
            .ToArray();
        string[] serializationMethods = assembly.GetTypes()
            .SelectMany(type => type.GetMethods(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static | BindingFlags.DeclaredOnly))
            .Where(method => method.Name is "ToSerializable" or "FromSerializable" or "SyncWithSerializedPlayer")
            .Where(method => method.DeclaringType?.FullName?.Contains("SerializerContext", StringComparison.Ordinal) != true)
            .Select(Signature)
            .OrderBy(signature => signature, StringComparer.Ordinal)
            .ToArray();
        string[] combatSerializationMembers = assembly.GetTypes()
            .Where(type => type.FullName?.Contains("Serial", StringComparison.OrdinalIgnoreCase) == true)
            .SelectMany(type => type.GetMembers(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static | BindingFlags.DeclaredOnly)
                .Where(member => member.Name.Contains("Combat", StringComparison.OrdinalIgnoreCase))
                .Select(member => $"{type.FullName}.{member.Name}:{member.MemberType}"))
            .OrderBy(name => name, StringComparer.Ordinal)
            .ToArray();

        _facts["native_snapshot_surfaces"] = new
        {
            methods = methodSurfaces,
            candidate_types = candidateTypes,
            serialization_methods = serializationMethods,
            combat_serialization_members = combatSerializationMembers
        };
        return $"methods={methodSurfaces.Length}; candidate_types={candidateTypes.Length}; serialization_methods={serializationMethods.Length}; combat_members={combatSerializationMembers.Length}";
    }

    private string InspectNativeTurnSurfaces()
    {
        Assembly assembly = (_context ?? throw new InvalidOperationException("Assembly is not loaded.")).Assembly;
        static bool Relevant(string value) =>
            value.Contains("Turn", StringComparison.OrdinalIgnoreCase) ||
            value.Contains("Intent", StringComparison.OrdinalIgnoreCase) ||
            value.Contains("ReadyToEnd", StringComparison.OrdinalIgnoreCase);
        static string Signature(MethodInfo method) =>
            $"{method.ReturnType.Name} {method.DeclaringType?.FullName}.{method.Name}({string.Join(',', method.GetParameters().Select(parameter => $"{parameter.ParameterType.Name} {parameter.Name}"))})";

        HashSet<string> targetTypes = new(StringComparer.Ordinal)
        {
            "MegaCrit.Sts2.Core.Combat.CombatManager",
            "MegaCrit.Sts2.Core.GameActions.EndPlayerTurnAction",
            "MegaCrit.Sts2.Core.GameActions.NetEndPlayerTurnAction",
            "MegaCrit.Sts2.Core.GameActions.ReadyToBeginEnemyTurnAction",
            "MegaCrit.Sts2.Core.GameActions.NetReadyToBeginEnemyTurnAction",
            "MegaCrit.Sts2.Core.GameActions.UndoEndPlayerTurnAction",
            "MegaCrit.Sts2.Core.GameActions.NetUndoEndPlayerTurnAction"
        };
        Type[] types = assembly.GetTypes().Where(type => type.FullName is not null && targetTypes.Contains(type.FullName)).ToArray();
        string[] methods = types
            .SelectMany(type => type.GetMethods(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static | BindingFlags.DeclaredOnly))
            .Where(method => Relevant(method.Name) || method.Name is "ExecuteAction" or "Execute" or "Create")
            .Select(Signature)
            .OrderBy(signature => signature, StringComparer.Ordinal)
            .ToArray();
        string[] constructors = types
            .SelectMany(type => type.GetConstructors(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance)
                .Select(constructor => $"{type.FullName}({string.Join(',', constructor.GetParameters().Select(parameter => $"{parameter.ParameterType.Name} {parameter.Name}"))})"))
            .OrderBy(signature => signature, StringComparer.Ordinal)
            .ToArray();
        object combatManager = _combatManager ?? throw new InvalidOperationException("Combat manager prerequisite is unavailable.");
        List<object> managerFields = [];
        for (Type? current = combatManager.GetType(); current is not null; current = current.BaseType)
        {
            foreach (FieldInfo field in current.GetFields(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.DeclaredOnly)
                         .Where(field => Relevant(field.Name) || field.Name.Contains("player", StringComparison.OrdinalIgnoreCase)))
            {
                object? value = field.GetValue(combatManager);
                managerFields.Add(new { name = field.Name, type = field.FieldType.FullName, is_null = value is null });
            }
        }
        string[] typeNames = types.Select(type => type.FullName!).OrderBy(name => name, StringComparer.Ordinal).ToArray();
        _facts["native_turn_surfaces"] = new { types = typeNames, constructors, methods, combat_manager_fields = managerFields };
        string nullManagerFields = string.Join(',', managerFields
            .Where(field => (bool)field.GetType().GetProperty("is_null")!.GetValue(field)!)
            .Select(field => $"{field.GetType().GetProperty("name")!.GetValue(field)}:{field.GetType().GetProperty("type")!.GetValue(field)}"));
        return $"types={typeNames.Length}; constructors={constructors.Length}; methods={methods.Length}; manager_fields={managerFields.Count}; null_manager_fields={nullManagerFields}; ctors={string.Join(" | ", constructors)}";
    }

    private string PreparePlayerDecisionState()
    {
        object playerCombat = _playerCombat ?? throw new InvalidOperationException("Player combat state prerequisite is unavailable.");
        ReflectionTools.Set(playerCombat, "Energy", 3);
        object playingPhase = Enum.Parse(Require("MegaCrit.Sts2.Core.Combat.PlayerTurnPhase"), "Play");
        ReflectionTools.Set(playerCombat, "Phase", playingPhase);
        return $"phase={ReflectionTools.Get(playerCombat, "Phase")}; energy={ReflectionTools.Get(playerCombat, "Energy")}";
    }

    private string InitializeTurnCoordinatorServices()
    {
        object combatManager = _combatManager ?? throw new InvalidOperationException("Combat manager prerequisite is unavailable.");
        List<string> initialized = [];
        for (Type? current = combatManager.GetType(); current is not null; current = current.BaseType)
        {
            foreach (FieldInfo field in current.GetFields(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.DeclaredOnly))
            {
                if (field.GetValue(combatManager) is not null || !field.FieldType.IsGenericType) continue;
                Type genericDefinition = field.FieldType.GetGenericTypeDefinition();
                if (genericDefinition != typeof(Action<>) && genericDefinition != typeof(Action<,>)) continue;
                MethodInfo invoke = field.FieldType.GetMethod("Invoke")!;
                ParameterExpression[] parameters = invoke.GetParameters()
                    .Select(parameter => Expression.Parameter(parameter.ParameterType, parameter.Name))
                    .ToArray();
                Delegate noOp = Expression.Lambda(field.FieldType, Expression.Empty(), parameters).Compile();
                field.SetValue(combatManager, noOp);
                initialized.Add(field.Name);
            }
        }
        _facts["turn_coordinator_services"] = new
        {
            initialized_noop_events = initialized,
            scope = "absent coordinator/UI event subscribers only; native turn state and mechanics remain active"
        };
        return $"initialized_noop_events={string.Join(',', initialized)}";
    }

    private string ExtractCanonicalObservation(string label, bool requirePlayableStrike)
    {
        object runState = _runState ?? throw new InvalidOperationException("Native run prerequisite is unavailable.");
        object combatState = _combatState ?? throw new InvalidOperationException("Native combat prerequisite is unavailable.");
        object player = _player ?? throw new InvalidOperationException("Native player prerequisite is unavailable.");
        object playerCombat = _playerCombat ?? throw new InvalidOperationException("Player combat state prerequisite is unavailable.");
        object cardDb = ReflectionTools.GetStatic(Require("MegaCrit.Sts2.Core.GameActions.Multiplayer.NetCombatCardDb"), "Instance")!;

        object runRng = ReflectionTools.Get(runState, "Rng")!;
        object serializableRng = ReflectionTools.Invoke(runRng, "ToSerializable")!;
        SortedDictionary<string, int> counters = new(StringComparer.Ordinal);
        foreach (object? pair in ReflectionTools.Enumerate(ReflectionTools.Get(serializableRng, "Counters")))
        {
            if (pair is null) continue;
            counters[ReflectionTools.Get(pair, "Key")!.ToString()!] = (int)ReflectionTools.Get(pair, "Value")!;
        }

        object[] creatures = ReflectionTools.Enumerate(ReflectionTools.Get(combatState, "Creatures"))
            .Where(creature => creature is not null)
            .Select(creature => SnapshotCreature(creature!))
            .ToArray();
        object[] piles = new[] { "Hand", "DrawPile", "DiscardPile", "ExhaustPile", "PlayPile" }
            .Select(name => SnapshotPile(name, ReflectionTools.Get(playerCombat, name)!, cardDb))
            .ToArray();
        object[] legalActions = ExtractLegalActions(combatState, playerCombat, cardDb);
        string[] actionIds = legalActions
            .Select(action => (string)ReflectionTools.Get(action, "action_id")!)
            .ToArray();
        string phase = ReflectionTools.Get(playerCombat, "Phase")!.ToString()!;
        if (actionIds.Distinct(StringComparer.Ordinal).Count() != actionIds.Length)
            throw new InvalidOperationException($"Canonical observation '{label}' contains duplicate legal action IDs.");
        if (phase == "Play" && !actionIds.Contains("end_turn", StringComparer.Ordinal))
            throw new InvalidOperationException($"Canonical observation '{label}' omitted end_turn.");
        if (phase != "Play" && actionIds.Length != 0)
            throw new InvalidOperationException($"Canonical observation '{label}' exposed actions outside the Play phase.");
        if (requirePlayableStrike && !actionIds.Any(actionId => actionId.StartsWith("play:0:target:", StringComparison.Ordinal)))
            throw new InvalidOperationException("Initial canonical observation omitted the native legal Strike target action.");

        object state = new
        {
            schema_version = 1,
            run = new
            {
                seed = ReflectionTools.Get(runRng, "StringSeed"),
                rng_counters = counters
            },
            combat = new
            {
                player_id = ReflectionTools.Get(player, "NetId"),
                turn = ReflectionTools.Get(playerCombat, "TurnNumber"),
                phase,
                energy = ReflectionTools.Get(playerCombat, "Energy"),
                max_energy = ReflectionTools.Get(playerCombat, "MaxEnergy"),
                stars = ReflectionTools.Get(playerCombat, "Stars"),
                creatures,
                piles
            },
            decision = new { kind = "combat_action", legal_actions = legalActions }
        };
        byte[] canonicalJson = JsonSerializer.SerializeToUtf8Bytes(state);
        string hash = Convert.ToHexString(SHA256.HashData(canonicalJson));
        if (label == "initial")
            _initialStateHash = hash;
        else if (_initialStateHash == hash)
            throw new InvalidOperationException($"Canonical observation '{label}' did not change after the native transition.");
        _facts[$"canonical_observation_{label}"] = new { state_hash = hash, state, legal_actions = legalActions };
        return $"label={label}; state_hash={hash}; legal_actions={legalActions.Length}; actions={string.Join(',', actionIds)}; creatures={creatures.Length}";
    }

    private object SnapshotCreature(object creature)
    {
        object modelId = ReflectionTools.Get(creature, "ModelId")!;
        object? monster = ReflectionTools.Get(creature, "Monster");
        object? nextMove = monster is null ? null : ReflectionTools.Get(monster, "NextMove");
        object[] intents = nextMove is null
            ? []
            : ReflectionTools.Enumerate(ReflectionTools.Get(nextMove, "Intents"))
                .Where(intent => intent is not null)
                .Select(intent => SnapshotIntent(intent!, creature))
                .ToArray();
        object[] powers = ReflectionTools.Enumerate(ReflectionTools.Get(creature, "Powers"))
            .Where(power => power is not null)
            .Select(power => new
            {
                id = ReadModelEntry(power!),
                amount = ReflectionTools.Get(power!, "Amount")
            })
            .Cast<object>()
            .ToArray();
        return new
        {
            combat_id = ReflectionTools.Get(creature, "CombatId"),
            model_id = ReflectionTools.Get(modelId, "Entry"),
            side = ReflectionTools.Get(creature, "Side")!.ToString(),
            hp = ReflectionTools.Get(creature, "CurrentHp"),
            max_hp = ReflectionTools.Get(creature, "MaxHp"),
            block = ReflectionTools.Get(creature, "Block"),
            alive = ReflectionTools.Get(creature, "IsAlive"),
            next_move = nextMove is null ? null : new
            {
                id = ReflectionTools.Get(nextMove, "Id"),
                intents
            },
            powers
        };
    }

    private object SnapshotIntent(object intent, object owner)
    {
        string type = ReflectionTools.Get(intent, "IntentType")!.ToString()!;
        object? damage = null;
        object? repeats = null;
        if (intent.GetType().IsSubclassOf(Require("MegaCrit.Sts2.Core.MonsterMoves.Intents.AttackIntent")))
        {
            object combatState = ReflectionTools.Get(owner, "CombatState")!;
            object targets = ReflectionTools.Get(combatState, "Allies")!;
            damage = ReflectionTools.Invoke(intent, "GetSingleDamage", targets, owner);
            repeats = ReflectionTools.Get(intent, "Repeats");
        }
        return new
        {
            intent_type = type,
            implementation = intent.GetType().Name,
            damage,
            repeats
        };
    }

    private object SnapshotPile(string name, object pile, object cardDb)
    {
        object[] cards = ReflectionTools.Enumerate(ReflectionTools.Get(pile, "Cards"))
            .Where(card => card is not null)
            .Select(card =>
            {
                object energyCost = ReflectionTools.Get(card!, "EnergyCost")!;
                return (object)new
                {
                    net_id = ReflectionTools.Invoke(cardDb, "GetCardId", card!),
                    model_id = ReadModelEntry(card!),
                    card_type = ReflectionTools.Get(card!, "Type")!.ToString(),
                    target_type = ReflectionTools.Get(card!, "TargetType")!.ToString(),
                    energy_cost = ReflectionTools.Invoke(energyCost, "GetResolved"),
                    costs_x = ReflectionTools.Get(energyCost, "CostsX")
                };
            })
            .ToArray();
        return new { name, type = ReflectionTools.Get(pile, "Type")!.ToString(), cards };
    }

    private object[] ExtractLegalActions(object combatState, object playerCombat, object cardDb)
    {
        List<object> actions = [];
        if (ReflectionTools.Get(playerCombat, "Phase")!.ToString() != "Play")
            return actions.ToArray();
        object hand = ReflectionTools.Get(playerCombat, "Hand")!;
        IReadOnlyList<object?> creatures = ReflectionTools.Enumerate(ReflectionTools.Get(combatState, "Creatures"));
        HashSet<string> targetedTypes = new(StringComparer.Ordinal)
        {
            "Self", "AnyEnemy", "AnyPlayer", "AnyAlly"
        };

        foreach (object? card in ReflectionTools.Enumerate(ReflectionTools.Get(hand, "Cards")))
        {
            if (card is null || !(bool)ReflectionTools.Invoke(card, "CanPlay")!) continue;
            uint cardId = (uint)ReflectionTools.Invoke(cardDb, "GetCardId", card)!;
            string targetType = ReflectionTools.Get(card, "TargetType")!.ToString()!;
            if (!targetedTypes.Contains(targetType))
            {
                actions.Add(new { action_id = $"play:{cardId}:none", kind = "play_card", card_id = cardId, target_id = (uint?)null });
                continue;
            }

            foreach (object? creature in creatures)
            {
                if (creature is null || !(bool)ReflectionTools.Invoke(card, "IsValidTarget", creature)!) continue;
                object? targetId = ReflectionTools.Get(creature, "CombatId");
                actions.Add(new { action_id = $"play:{cardId}:target:{targetId}", kind = "play_card", card_id = cardId, target_id = targetId });
            }
        }

        actions.Add(new { action_id = "end_turn", kind = "end_turn", card_id = (uint?)null, target_id = (uint?)null });
        return actions.ToArray();
    }

    private static string ReadModelEntry(object model)
    {
        object modelId = ReflectionTools.Get(model, "Id") ?? ReflectionTools.Get(model, "ModelId")!;
        return (string)ReflectionTools.Get(modelId, "Entry")!;
    }

    private async Task<string> ExecuteNativeStrikeAsync()
    {
        object strike = _strike ?? throw new InvalidOperationException("Native Strike prerequisite is unavailable.");
        object target = _target ?? throw new InvalidOperationException("Native target prerequisite is unavailable.");
        object combatState = _combatState ?? throw new InvalidOperationException("Native combat prerequisite is unavailable.");
        object combatManager = _combatManager ?? throw new InvalidOperationException("Native combat manager prerequisite is unavailable.");
        ReflectionTools.Set(combatManager, "_state", combatState);
        ReflectionTools.Set(combatManager, "IsInProgress", true);
        int before = ReadCurrentHp(target);
        object cardPlay = ReflectionTools.Create(Require("MegaCrit.Sts2.Core.Entities.Cards.CardPlay"));
        ReflectionTools.Set(cardPlay, "Card", strike);
        ReflectionTools.Set(cardPlay, "Target", target);
        ReflectionTools.Set(cardPlay, "PlayIndex", 0);
        ReflectionTools.Set(cardPlay, "PlayCount", 1);
        object choiceContext = ReflectionTools.Create(Require("MegaCrit.Sts2.Core.GameActions.Multiplayer.BlockingPlayerChoiceContext"));
        object? taskObject = ReflectionTools.Invoke(strike, "OnPlay", choiceContext, cardPlay);
        if (taskObject is Task task) await task.ConfigureAwait(false);
        int after = ReadCurrentHp(target);
        if (after >= before) throw new InvalidOperationException($"Strike did not reduce target HP ({before} -> {after}).");
        _facts["strike_damage"] = before - after;
        return $"target_hp={before}->{after}; damage={before - after}; path=StrikeIronclad.OnPlay (PlayCardAction wrapper not executed)";
    }

    private async Task<string> ExecuteFullPlayCardActionAsync()
    {
        object strike = _strike ?? throw new InvalidOperationException("Native Strike prerequisite is unavailable.");
        object target = _target ?? throw new InvalidOperationException("Native target prerequisite is unavailable.");
        object playerCombat = _playerCombat ?? throw new InvalidOperationException("Player combat state prerequisite is unavailable.");
        object hand = ReflectionTools.Get(playerCombat, "Hand")!;
        object discardPile = ReflectionTools.Get(playerCombat, "DiscardPile")!;
        object playPile = ReflectionTools.Get(playerCombat, "PlayPile")!;

        int baselineHp = (int)ReflectionTools.Get(target, "MaxHp")!;
        ReflectionTools.Set(target, "CurrentHp", baselineHp);
        ReflectionTools.Set(playerCombat, "Energy", 3);
        object playingPhase = Enum.Parse(Require("MegaCrit.Sts2.Core.Combat.PlayerTurnPhase"), "Play");
        ReflectionTools.Set(playerCombat, "Phase", playingPhase);

        if (!ReflectionTools.Enumerate(ReflectionTools.Get(hand, "Cards")).Contains(strike))
            ReflectionTools.Invoke(hand, "AddInternal", strike, 0, true);

        int handBefore = ReflectionTools.Enumerate(ReflectionTools.Get(hand, "Cards")).Count;
        object action = ReflectionTools.Create(Require("MegaCrit.Sts2.Core.GameActions.PlayCardAction"), strike, target);
        object choiceContext = ReflectionTools.Create(Require("MegaCrit.Sts2.Core.GameActions.Multiplayer.BlockingPlayerChoiceContext"));
        ReflectionTools.Set(action, "PlayerChoiceContext", choiceContext);
        object? taskObject = ReflectionTools.Invoke(action, "ExecuteAction");
        if (taskObject is Task task) await task.ConfigureAwait(false);

        int afterHp = ReadCurrentHp(target);
        int energyAfter = (int)ReflectionTools.Get(playerCombat, "Energy")!;
        int handAfter = ReflectionTools.Enumerate(ReflectionTools.Get(hand, "Cards")).Count;
        int discardAfter = ReflectionTools.Enumerate(ReflectionTools.Get(discardPile, "Cards")).Count;
        int playAfter = ReflectionTools.Enumerate(ReflectionTools.Get(playPile, "Cards")).Count;
        string pile = ReflectionTools.Get(ReflectionTools.Get(strike, "Pile")!, "Type")!.ToString()!;

        if (afterHp != baselineHp - 6)
            throw new InvalidOperationException($"Full PlayCardAction damage diverged: {baselineHp} -> {afterHp}.");
        if (energyAfter != 2)
            throw new InvalidOperationException($"Full PlayCardAction energy diverged: 3 -> {energyAfter}.");
        if (handAfter != handBefore - 1)
            throw new InvalidOperationException($"Full PlayCardAction did not remove Strike from hand: {handBefore} -> {handAfter}.");

        _facts["full_play_card_action"] = new
        {
            target_hp_before = baselineHp,
            target_hp_after = afterHp,
            energy_before = 3,
            energy_after = energyAfter,
            hand_before = handBefore,
            hand_after = handAfter,
            discard_count = discardAfter,
            play_count = playAfter,
            final_pile = pile
        };
        return $"hp={baselineHp}->{afterHp}; energy=3->{energyAfter}; hand={handBefore}->{handAfter}; final_pile={pile}";
    }

    private async Task<string> BenchmarkReconstructStepObserveAsync(int iterations)
    {
        Dictionary<string, object?> preservedFacts = new(_facts, StringComparer.Ordinal);
        object? preservedRunState = _runState;
        object? preservedPlayer = _player;
        object? preservedCombatState = _combatState;
        object? preservedStrike = _strike;
        object? preservedTarget = _target;
        object? preservedCombatManager = _combatManager;
        object? preservedPlayerCombat = _playerCombat;
        string? preservedInitialStateHash = _initialStateHash;
        ulong checksum = 14695981039346656037UL;
        Stopwatch stopwatch = Stopwatch.StartNew();
        object? benchmarkFact = null;
        string? detail = null;
        try
        {
            for (int index = 0; index < iterations; index++)
            {
                ConstructNativeRun();
                ConstructNativeCombat();
                InitializeCardActionServices();
                PreparePlayerDecisionState();
                string initial = ExtractCanonicalObservation("initial", requirePlayableStrike: true);
                await ExecuteFullPlayCardActionAsync().ConfigureAwait(false);
                string after = ExtractCanonicalObservation("post_action", requirePlayableStrike: false);
                checksum = UpdateStableChecksum(checksum, initial);
                checksum = UpdateStableChecksum(checksum, after);
            }
            stopwatch.Stop();

            double throughput = iterations / stopwatch.Elapsed.TotalSeconds;
            benchmarkFact = new
            {
                iterations,
                throughput_per_second = throughput,
                mean_milliseconds = stopwatch.Elapsed.TotalMilliseconds / iterations,
                checksum,
                boundary = "warm deterministic run/combat reconstruction -> canonical observation/legal actions -> full native PlayCardAction -> canonical observation/legal actions"
            };
            detail = $"iterations={iterations}; cycles_per_second={throughput:F1}; mean_ms={stopwatch.Elapsed.TotalMilliseconds / iterations:F3}; checksum={checksum}";
        }
        finally
        {
            _runState = preservedRunState;
            _player = preservedPlayer;
            _combatState = preservedCombatState;
            _strike = preservedStrike;
            _target = preservedTarget;
            _combatManager = preservedCombatManager;
            _playerCombat = preservedPlayerCombat;
            _initialStateHash = preservedInitialStateHash;
            if (_combatManager is not null && _combatState is not null)
                ReflectionTools.Set(_combatManager, "_state", _combatState);
            _facts.Clear();
            foreach ((string key, object? value) in preservedFacts)
                _facts[key] = value;
        }
        _facts["reconstruct_step_observe_benchmark"] = benchmarkFact;
        return detail!;
    }

    private async Task<string> ExecuteNativeTurnCycleAsync()
    {
        object player = _player ?? throw new InvalidOperationException("Native player prerequisite is unavailable.");
        object playerCombat = _playerCombat ?? throw new InvalidOperationException("Player combat state prerequisite is unavailable.");
        object combatState = _combatState ?? throw new InvalidOperationException("Native combat prerequisite is unavailable.");
        object combatManager = _combatManager ?? throw new InvalidOperationException("Native combat manager prerequisite is unavailable.");
        ReflectionTools.Set(combatManager, "_state", combatState);
        ReflectionTools.Set(combatManager, "IsInProgress", true);

        int turnBefore = (int)ReflectionTools.Get(playerCombat, "TurnNumber")!;
        object playerCreature = ReflectionTools.Enumerate(ReflectionTools.Get(combatState, "Creatures"))
            .Single(creature => creature is not null && ReflectionTools.Get(creature, "Player") == player)!;
        int hpBefore = ReadCurrentHp(playerCreature);
        int drawBefore = ReflectionTools.Enumerate(ReflectionTools.Get(ReflectionTools.Get(playerCombat, "DrawPile")!, "Cards")).Count;
        int discardBefore = ReflectionTools.Enumerate(ReflectionTools.Get(ReflectionTools.Get(playerCombat, "DiscardPile")!, "Cards")).Count;

        object? phaseOneTask = ReflectionTools.Invoke(combatManager, "EndPlayerTurnPhaseOneInternal");
        if (phaseOneTask is Task phaseOne) await phaseOne.ConfigureAwait(false);
        object? phaseTwoTask = ReflectionTools.Invoke(combatManager, "EndPlayerTurnPhaseTwoInternal");
        if (phaseTwoTask is Task phaseTwo) await phaseTwo.ConfigureAwait(false);
        Func<Task> drainHeadlessActionQueue = () => Task.CompletedTask;
        object? switchTask = ReflectionTools.Invoke(combatManager, "SwitchFromPlayerToEnemySide", drainHeadlessActionQueue);
        if (switchTask is Task switchSides) await switchSides.ConfigureAwait(false);

        int turnAfter = (int)ReflectionTools.Get(playerCombat, "TurnNumber")!;
        int hpAfter = ReadCurrentHp(playerCreature);
        int energyAfter = (int)ReflectionTools.Get(playerCombat, "Energy")!;
        int maxEnergy = (int)ReflectionTools.Get(playerCombat, "MaxEnergy")!;
        string phaseAfter = ReflectionTools.Get(playerCombat, "Phase")!.ToString()!;
        int handAfter = ReflectionTools.Enumerate(ReflectionTools.Get(ReflectionTools.Get(playerCombat, "Hand")!, "Cards")).Count;
        int drawAfter = ReflectionTools.Enumerate(ReflectionTools.Get(ReflectionTools.Get(playerCombat, "DrawPile")!, "Cards")).Count;
        int discardAfter = ReflectionTools.Enumerate(ReflectionTools.Get(ReflectionTools.Get(playerCombat, "DiscardPile")!, "Cards")).Count;

        if (turnAfter <= turnBefore)
            throw new InvalidOperationException($"Native turn cycle did not advance turn number: {turnBefore} -> {turnAfter}.");
        if (phaseAfter != "Play")
            throw new InvalidOperationException($"Native turn cycle did not return to Play phase: {phaseAfter}.");
        if (energyAfter != maxEnergy)
            throw new InvalidOperationException($"Native turn cycle did not refresh energy: {energyAfter}/{maxEnergy}.");
        if (handAfter == 0)
            throw new InvalidOperationException("Native turn cycle did not draw a new hand.");

        _facts["native_turn_cycle"] = new
        {
            turn_before = turnBefore,
            turn_after = turnAfter,
            player_hp_before = hpBefore,
            player_hp_after = hpAfter,
            phase_after = phaseAfter,
            energy_after = energyAfter,
            hand_after = handAfter,
            draw_before = drawBefore,
            draw_after = drawAfter,
            discard_before = discardBefore,
            discard_after = discardAfter
        };
        return $"turn={turnBefore}->{turnAfter}; hp={hpBefore}->{hpAfter}; phase={phaseAfter}; energy={energyAfter}; hand={handAfter}; draw={drawBefore}->{drawAfter}; discard={discardBefore}->{discardAfter}";
    }

    private static ulong UpdateStableChecksum(ulong checksum, string value)
    {
        foreach (char character in value)
        {
            checksum ^= character;
            checksum *= 1099511628211UL;
        }
        return checksum;
    }

    private async Task<string> BenchmarkNativeStrikeAsync(int iterations)
    {
        object strike = _strike ?? throw new InvalidOperationException("Native Strike prerequisite is unavailable.");
        object target = _target ?? throw new InvalidOperationException("Native target prerequisite is unavailable.");
        int baselineHp = (int)ReflectionTools.Get(target, "MaxHp")!;
        object cardPlay = ReflectionTools.Create(Require("MegaCrit.Sts2.Core.Entities.Cards.CardPlay"));
        ReflectionTools.Set(cardPlay, "Card", strike);
        ReflectionTools.Set(cardPlay, "Target", target);
        ReflectionTools.Set(cardPlay, "PlayIndex", 0);
        ReflectionTools.Set(cardPlay, "PlayCount", 1);
        object choiceContext = ReflectionTools.Create(Require("MegaCrit.Sts2.Core.GameActions.Multiplayer.BlockingPlayerChoiceContext"));

        long checksum = 0;
        Stopwatch stopwatch = Stopwatch.StartNew();
        for (int index = 0; index < iterations; index++)
        {
            ReflectionTools.Set(target, "CurrentHp", baselineHp);
            object? taskObject = ReflectionTools.Invoke(strike, "OnPlay", choiceContext, cardPlay);
            if (taskObject is Task task) await task.ConfigureAwait(false);
            int after = ReadCurrentHp(target);
            if (after != baselineHp - 6)
                throw new InvalidOperationException($"Native Strike benchmark diverged at iteration {index}: {baselineHp} -> {after}.");
            checksum += after;
        }
        stopwatch.Stop();

        double throughput = iterations / stopwatch.Elapsed.TotalSeconds;
        _facts["strike_benchmark"] = new { iterations, throughput_per_second = throughput, checksum };
        return $"iterations={iterations}; transitions_per_second={throughput:F0}; checksum={checksum}; native_damage_path=true";
    }

    private string ProbeSerializationAndClone()
    {
        object runState = _runState ?? throw new InvalidOperationException("Native run prerequisite is unavailable.");
        object runRng = ReflectionTools.Get(runState, "Rng")!;
        object serializable = ReflectionTools.Invoke(runRng, "ToSerializable")!;
        object clone = ReflectionTools.InvokeStatic(Require("MegaCrit.Sts2.Core.Runs.RunRngSet"), "FromSave", serializable)!;
        object left = ReflectionTools.Get(runRng, "Shuffle")!;
        object right = ReflectionTools.Get(clone, "Shuffle")!;
        int[] a = Enumerable.Range(0, 64).Select(_ => (int)ReflectionTools.Invoke(left, "NextInt", 10_000)!).ToArray();
        int[] b = Enumerable.Range(0, 64).Select(_ => (int)ReflectionTools.Invoke(right, "NextInt", 10_000)!).ToArray();
        if (!a.SequenceEqual(b)) throw new InvalidOperationException("Serializable RunRngSet clone diverged.");
        return "RunRngSet.ToSerializable/FromSave; shuffle next64_exact=true";
    }

    private string BenchmarkRng(int iterations)
    {
        object rng = ReflectionTools.Create(Require("MegaCrit.Sts2.Core.Random.Rng"), (uint)123456, 0);
        MethodInfo next = rng.GetType().GetMethod("NextInt", [typeof(int)])!;
        _ = next.Invoke(rng, [int.MaxValue]);
        Stopwatch stopwatch = Stopwatch.StartNew();
        long checksum = 0;
        for (int index = 0; index < iterations; index++)
            checksum += (int)next.Invoke(rng, [int.MaxValue])!;
        stopwatch.Stop();
        double throughput = iterations / stopwatch.Elapsed.TotalSeconds;
        _facts["rng_benchmark"] = new { iterations, throughput_per_second = throughput, checksum };
        return $"iterations={iterations}; transitions_per_second={throughput:F0}; checksum={checksum}; reflection_dispatch=true";
    }

    private static int ReadCurrentHp(object creature)
    {
        object? value = ReflectionTools.Get(creature, "CurrentHp") ?? ReflectionTools.Get(creature, "Hp");
        return Convert.ToInt32(value);
    }

    private object ToTypedList(Type elementType, IReadOnlyList<object?> items)
    {
        Type listType = typeof(List<>).MakeGenericType(elementType);
        IList list = (IList)Activator.CreateInstance(listType)!;
        foreach (object? item in items) list.Add(item);
        return list;
    }

    private Type Require(string name) => (_context ?? throw new InvalidOperationException("Assembly is not loaded.")).RequireType(name);
    private void RunStage(string name, Func<string?> action)
    {
        Stopwatch stopwatch = Stopwatch.StartNew();
        try { string? evidence = action(); stopwatch.Stop(); _stages.Add(new(name, true, stopwatch.Elapsed.TotalMilliseconds, evidence)); }
        catch (Exception raw) { stopwatch.Stop(); Exception error = ReflectionTools.Unwrap(raw); _stages.Add(new(name, false, stopwatch.Elapsed.TotalMilliseconds, FailureType: error.GetType().FullName, FailureMessage: error.Message, FailureStack: error.StackTrace)); }
    }

    private async Task RunStageAsync(string name, Func<Task<string>> action)
    {
        Stopwatch stopwatch = Stopwatch.StartNew();
        try { string evidence = await action().ConfigureAwait(false); stopwatch.Stop(); _stages.Add(new(name, true, stopwatch.Elapsed.TotalMilliseconds, evidence)); }
        catch (Exception raw) { stopwatch.Stop(); Exception error = ReflectionTools.Unwrap(raw); _stages.Add(new(name, false, stopwatch.Elapsed.TotalMilliseconds, FailureType: error.GetType().FullName, FailureMessage: error.Message, FailureStack: error.StackTrace)); }
    }
}

public sealed class GodotInitializationRequiredException(string message) : Exception(message);
