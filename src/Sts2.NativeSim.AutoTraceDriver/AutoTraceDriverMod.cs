using System.Reflection;
using System.Text.RegularExpressions;
using Godot;
using HarmonyLib;
using MegaCrit.Sts2.Core.AutoSlay;
using MegaCrit.Sts2.Core.AutoSlay.Handlers.Rooms;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Context;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.GameActions;
using MegaCrit.Sts2.Core.Helpers;
using MegaCrit.Sts2.Core.Modding;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Multiplayer.Game.Lobby;
using MegaCrit.Sts2.Core.Random;
using MegaCrit.Sts2.Core.Runs;

namespace Sts2.NativeSim.AutoTraceDriver;

[ModInitializer(nameof(Initialize))]
public static class AutoTraceDriverMod
{
    private const string EnableArgument = "native-sim-autotrace-driver";
    private static AutoSlayer? _autoSlayer;
    private static int _completedCombats;
    private static int _combatLimit = 1;
    private static bool _coveragePolicy = true;
    private static string _requestedSeed = "A1B2C3D4E5";

    public static void Initialize()
    {
        if (!CommandLineHelper.HasArg(EnableArgument)) return;
        ValidateIsolation();
        string? combatLimit = CommandLineHelper.GetValue("native-sim-combat-count");
        if (combatLimit is not null && (!int.TryParse(combatLimit, out _combatLimit) || _combatLimit < 1 || _combatLimit > 100))
            throw new InvalidOperationException("--native-sim-combat-count must be an integer from 1 through 100.");
        string policy = CommandLineHelper.GetValue("native-sim-autotrace-policy") ?? "coverage";
        _coveragePolicy = policy switch
        {
            "coverage" => true,
            "basics" => false,
            _ => throw new InvalidOperationException("--native-sim-autotrace-policy must be coverage or basics."),
        };
        Harmony harmony = new("sts2-native-sim.autotrace-driver");
        harmony.Patch(
            typeof(CombatRoomHandler).GetMethod(nameof(CombatRoomHandler.HandleAsync))!,
            prefix: new HarmonyMethod(typeof(AutoTraceDriverMod), nameof(UseTraceableCombatPolicy)));
        harmony.Patch(
            typeof(CombatManager).GetMethod(nameof(CombatManager.EndCombatInternal))!,
            postfix: new HarmonyMethod(typeof(AutoTraceDriverMod), nameof(StopAfterVictory)));
        harmony.Patch(
            typeof(CombatManager).GetMethod("ProcessPendingLoss", BindingFlags.NonPublic | BindingFlags.Instance)!,
            postfix: new HarmonyMethod(typeof(AutoTraceDriverMod), nameof(StopAfterLoss)));
        harmony.Patch(
            typeof(MegaCrit.Sts2.Core.Nodes.NGame).GetMethod("LaunchMainMenu", BindingFlags.NonPublic | BindingFlags.Instance)!,
            postfix: new HarmonyMethod(typeof(AutoTraceDriverMod), nameof(StartAfterMainMenu)));
        harmony.Patch(
            typeof(StartRunLobby).GetMethod("BeginRunForAllPlayersIfAllReady", BindingFlags.NonPublic | BindingFlags.Instance)!,
            prefix: new HarmonyMethod(typeof(AutoTraceDriverMod), nameof(ReapplySeedOverride)));
    }

    private static void ValidateIsolation()
    {
        if (!StringComparer.OrdinalIgnoreCase.Equals(DisplayServer.GetName(), "headless"))
            throw new InvalidOperationException("AutoTrace driver requires Godot headless display mode.");
        if (!StringComparer.OrdinalIgnoreCase.Equals(CommandLineHelper.GetValue("force-steam"), "off"))
            throw new InvalidOperationException("AutoTrace driver requires --force-steam=off.");
        if (!CommandLineHelper.HasArg("native-sim-trace"))
            throw new InvalidOperationException("AutoTrace driver requires the read-only native trace exporter flag.");
        string? root = System.Environment.GetEnvironmentVariable("STS2_NATIVE_AUTOTRACE_ROOT");
        if (string.IsNullOrWhiteSpace(root))
            throw new InvalidOperationException("STS2_NATIVE_AUTOTRACE_ROOT must name the isolated user-data root.");
        string expected = Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        string actual = Path.GetFullPath(OS.GetUserDataDir()).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        if (!actual.StartsWith(expected, StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException($"Refusing AutoTrace outside isolated user data. Expected beneath {expected}; actual {actual}.");
    }

    private static bool UseTraceableCombatPolicy(Rng random, CancellationToken ct, ref Task __result)
    {
        __result = RunTraceableCombatPolicyAsync(random, ct);
        return false;
    }

    private static async Task RunTraceableCombatPolicyAsync(Rng random, CancellationToken ct)
    {
        CombatManager manager = CombatManager.Instance ?? throw new InvalidOperationException("CombatManager is unavailable.");
        RunManager runManager = RunManager.Instance ?? throw new InvalidOperationException("RunManager is unavailable.");
        await MegaCrit.Sts2.Core.AutoSlay.Helpers.WaitHelper.Until(
            () => manager.IsInProgress, ct, TimeSpan.FromSeconds(10), "Combat did not start");
        Player player = LocalContext.GetMe(runManager.DebugOnlyGetState()) ?? throw new InvalidOperationException("Local player is unavailable.");
        while (manager.IsInProgress)
        {
            await MegaCrit.Sts2.Core.AutoSlay.Helpers.WaitHelper.Until(
                () => player.PlayerCombatState?.Phase == PlayerTurnPhase.Play || !manager.IsInProgress,
                ct, TimeSpan.FromSeconds(30), "Player play phase did not begin");
            if (!manager.IsInProgress) break;
            if (_coveragePolicy)
                await UsePotionsAsync(player, random, runManager, manager, ct);
            HashSet<CardModel> attempted = [];
            int cardsPlayed = 0;
            while (player.PlayerCombatState?.Phase == PlayerTurnPhase.Play)
            {
                CardModel[] cards = PileType.Hand.GetPile(player).Cards
                    .Where(card => (_coveragePolicy || card.Id.Entry.StartsWith("STRIKE_", StringComparison.Ordinal) || card.Id.Entry.StartsWith("DEFEND_", StringComparison.Ordinal))
                        && !attempted.Contains(card) && card.CanPlay(out _, out _))
                    .ToArray();
                if (cards.Length == 0) break;
                if (++cardsPlayed > 50)
                    throw new InvalidOperationException("AutoTrace exceeded the bounded 50-card policy budget in one turn.");
                CardModel card = cards[0];
                attempted.Add(card);
                Creature? target = SelectTarget(card.TargetType, card.CombatState, random, selfAsCreature: false);
                if (!card.TryManualPlay(target)) continue;
                await runManager.ActionExecutor.FinishedExecutingActions();
                if (!manager.IsInProgress) break;
            }
            if (!manager.IsInProgress) break;
            PlayerCombatState state = player.PlayerCombatState!;
            runManager.ActionQueueSynchronizer.RequestEnqueue(new EndPlayerTurnAction(player, state.TurnNumber));
            await runManager.ActionExecutor.FinishedExecutingActions();
        }
    }

    private static async Task UsePotionsAsync(Player player, Rng random, RunManager runManager, CombatManager manager, CancellationToken ct)
    {
        foreach (PotionModel potion in player.Potions.ToArray())
        {
            ct.ThrowIfCancellationRequested();
            if (!manager.IsInProgress || player.PlayerCombatState?.Phase != PlayerTurnPhase.Play) return;
            Creature? target = SelectTarget(potion.TargetType, player.Creature.CombatState, random, selfAsCreature: true);
            potion.EnqueueManualUse(target);
            await runManager.ActionExecutor.FinishedExecutingActions();
        }
    }

    private static Creature? SelectTarget(TargetType targetType, ICombatState? combatState, Rng random, bool selfAsCreature)
    {
        if (combatState is null)
            throw new InvalidOperationException("A targeted native action has no combat state.");
        Creature? target = targetType switch
        {
            TargetType.AnyEnemy => combatState.HittableEnemies.OrderBy(creature => creature.CombatId).FirstOrDefault(),
            TargetType.AnyAlly or TargetType.AnyPlayer => combatState.PlayerCreatures.Where(creature => creature.IsAlive).OrderBy(creature => creature.CombatId).FirstOrDefault(),
            TargetType.Self when selfAsCreature => combatState.PlayerCreatures.FirstOrDefault(creature => creature.IsAlive),
            _ => null,
        };
        if (targetType.IsSingleTarget() && targetType != TargetType.Self && target is null)
            throw new InvalidOperationException($"No native target is available for {targetType}.");
        return target;
    }

    private static void StopAfterVictory(ref Task __result) => __result = ContinueOrQuitAfterAsync(__result);

    private static void StartAfterMainMenu(ref Task __result) => __result = StartAfterMainMenuAsync(__result);

    private static async Task StartAfterMainMenuAsync(Task startup)
    {
        await startup;
        if (_autoSlayer is not null) return;
        _requestedSeed = SeedHelper.CanonicalizeSeed(CommandLineHelper.GetValue("seed") ?? "A1B2C3D4E5");
        if (!Regex.IsMatch(_requestedSeed, "^[0-9A-HJ-NP-Z]{10}$", RegexOptions.CultureInvariant))
            throw new InvalidOperationException("AutoTrace --seed must be a canonical 10-character shipped seed using 0-9/A-H/J-N/P-Z.");
        string? logFile = CommandLineHelper.GetValue("log-file");
        _autoSlayer = new AutoSlayer();
        _autoSlayer.Start(_requestedSeed, logFile);
    }

    private static void ReapplySeedOverride()
    {
        MegaCrit.Sts2.Core.Nodes.NGame game = MegaCrit.Sts2.Core.Nodes.NGame.Instance
            ?? throw new InvalidOperationException("NGame is unavailable while applying the deterministic AutoTrace seed.");
        game.DebugSeedOverride = _requestedSeed;
    }

    private static void StopAfterLoss() => Callable.From(() => NGameTree()?.Quit(0)).CallDeferred();

    private static async Task ContinueOrQuitAfterAsync(Task transition)
    {
        await transition.ConfigureAwait(false);
        if (Interlocked.Increment(ref _completedCombats) < _combatLimit) return;
        await Task.Delay(250).ConfigureAwait(false);
        Callable.From(() => NGameTree()?.Quit(0)).CallDeferred();
    }

    private static SceneTree? NGameTree() => Engine.GetMainLoop() as SceneTree;
}
