using System.Linq;
using System.Reflection;
using Godot;
using HarmonyLib;
using MegaCrit.Sts2.Core.AutoSlay;
using MegaCrit.Sts2.Core.AutoSlay.Handlers.Rooms;
using MegaCrit.Sts2.Core.AutoSlay.Handlers.Screens;
using MegaCrit.Sts2.Core.AutoSlay.Helpers;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Context;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Entities.Merchant;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.Entities.RestSite;
using MegaCrit.Sts2.Core.Events;
using MegaCrit.Sts2.Core.GameActions;
using MegaCrit.Sts2.Core.Helpers;
using MegaCrit.Sts2.Core.Map;
using MegaCrit.Sts2.Core.Modding;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Multiplayer.Game.Lobby;
using MegaCrit.Sts2.Core.Nodes;
using MegaCrit.Sts2.Core.Nodes.Cards.Holders;
using MegaCrit.Sts2.Core.Nodes.CommonUi;
using MegaCrit.Sts2.Core.Nodes.Events;
using MegaCrit.Sts2.Core.Nodes.GodotExtensions;
using MegaCrit.Sts2.Core.Nodes.Rewards;
using MegaCrit.Sts2.Core.Nodes.Rooms;
using MegaCrit.Sts2.Core.Nodes.Screens;
using MegaCrit.Sts2.Core.Nodes.Screens.CardSelection;
using MegaCrit.Sts2.Core.Nodes.Screens.GameOverScreen;
using MegaCrit.Sts2.Core.Nodes.Screens.Map;
using MegaCrit.Sts2.Core.Nodes.Screens.Overlays;
using MegaCrit.Sts2.Core.Nodes.Screens.Shops;
using MegaCrit.Sts2.Core.Random;
using MegaCrit.Sts2.Core.Rooms;
using MegaCrit.Sts2.Core.Runs;

namespace Sts2.NativeSim.FullAppBridge;

[ModInitializer(nameof(Initialize))]
public static class FullAppBridgeMod
{
    private static AutoSlayer? _autoSlayer;
    private static bool _initialized;

    public static void Initialize()
    {
        if (_initialized) return;
        _initialized = true;

        string portEnv = System.Environment.GetEnvironmentVariable("STS2_FULL_APP_BRIDGE_PORT") ?? "0";
        int.TryParse(portEnv, out int port);

        string portFile = System.Environment.GetEnvironmentVariable("STS2_FULL_APP_BRIDGE_PORT_FILE") ??
            Path.Combine(OS.GetUserDataDir(), "bridge_port.txt");

        Harmony harmony = new("sts2-native-sim.full-app-bridge");
        PresentationSuppression.Apply(harmony);

        TryPatchPostfix(harmony, typeof(NGame), "LaunchMainMenu", nameof(OnMainMenuLaunched));
        TryPatchPrefix(harmony, typeof(StartRunLobby), "BeginRunForAllPlayersIfAllReady", nameof(OnBeginRunForAllPlayers));

        TryPatchPrefix(harmony, typeof(CombatRoomHandler), nameof(CombatRoomHandler.HandleAsync), nameof(HandleCombatAsync));
        TryPatchPrefix(harmony, typeof(MapScreenHandler), nameof(MapScreenHandler.HandleAsync), nameof(HandleMapAsync));
        TryPatchPrefix(harmony, typeof(RewardsScreenHandler), nameof(RewardsScreenHandler.HandleAsync), nameof(HandleRewardsScreenAsync));
        TryPatchPrefix(harmony, typeof(CardRewardScreenHandler), nameof(CardRewardScreenHandler.HandleAsync), nameof(HandleCardRewardScreenAsync));
        TryPatchPrefix(harmony, typeof(RestSiteRoomHandler), nameof(RestSiteRoomHandler.HandleAsync), nameof(HandleRestSiteAsync));
        TryPatchPrefix(harmony, typeof(DeckUpgradeScreenHandler), nameof(DeckUpgradeScreenHandler.HandleAsync), nameof(HandleDeckUpgradeScreenAsync));
        TryPatchPrefix(harmony, typeof(DeckCardSelectScreenHandler), nameof(DeckCardSelectScreenHandler.HandleAsync), nameof(HandleDeckCardSelectScreenAsync));
        TryPatchPrefix(harmony, typeof(SimpleCardSelectScreenHandler), nameof(SimpleCardSelectScreenHandler.HandleAsync), nameof(HandleSimpleCardSelectScreenAsync));
        TryPatchPrefix(harmony, typeof(ShopRoomHandler), nameof(ShopRoomHandler.HandleAsync), nameof(HandleShopRoomAsync));
        TryPatchPrefix(harmony, typeof(EventRoomHandler), nameof(EventRoomHandler.HandleAsync), nameof(HandleEventRoomAsync));
        TryPatchPrefix(harmony, typeof(TreasureRoomHandler), nameof(TreasureRoomHandler.HandleAsync), nameof(HandleTreasureRoomAsync));
        TryPatchPrefix(harmony, typeof(VictoryRoomHandler), nameof(VictoryRoomHandler.HandleAsync), nameof(HandleVictoryRoomAsync));
        TryPatchPrefix(harmony, typeof(GameOverScreenHandler), nameof(GameOverScreenHandler.HandleAsync), nameof(HandleGameOverAsync));
        TryPatchPrefix(harmony, typeof(AutoSlayer), "WaitForRewardsScreenAsync", nameof(HandleWaitForRewardsScreenAsync));

        FullAppBridgeServer.Start(port, portFile);
    }

    private static void TryPatchPrefix(Harmony harmony, Type type, string methodName, string patchMethodName)
    {
        try
        {
            MethodInfo? method = AccessTools.Method(type, methodName);
            MethodInfo? patch = AccessTools.Method(typeof(FullAppBridgeMod), patchMethodName);
            if (method != null && patch != null)
            {
                harmony.Patch(method, prefix: new HarmonyMethod(patch));
            }
        }
        catch (Exception ex)
        {
            GD.PrintErr($"[FullAppBridge] Failed to patch prefix {type.Name}.{methodName}: {ex.Message}");
        }
    }

    private static void TryPatchPostfix(Harmony harmony, Type type, string methodName, string patchMethodName)
    {
        try
        {
            MethodInfo? method = AccessTools.Method(type, methodName);
            MethodInfo? patch = AccessTools.Method(typeof(FullAppBridgeMod), patchMethodName);
            if (method != null && patch != null)
            {
                harmony.Patch(method, postfix: new HarmonyMethod(patch));
            }
        }
        catch (Exception ex)
        {
            GD.PrintErr($"[FullAppBridge] Failed to patch postfix {type.Name}.{methodName}: {ex.Message}");
        }
    }

    private static void OnMainMenuLaunched(ref Task __result)
    {
        __result = StartAfterMainMenuAsync(__result);
    }

    private static async Task StartAfterMainMenuAsync(Task startup)
    {
        await startup;
        while (!FullAppBridgeServer.IsRunStarted)
        {
            await Task.Delay(10);
        }

        string seed = FullAppBridgeServer.RequestedSeed;
        string logFile = Path.Combine(OS.GetUserDataDir(), $"autoplay_{seed}.log");

        _autoSlayer = new AutoSlayer();
        _autoSlayer.Start(seed, logFile);
    }

    private static void OnBeginRunForAllPlayers(StartRunLobby __instance)
    {
        if (NGame.Instance is not null)
        {
            NGame.Instance.DebugSeedOverride = FullAppBridgeServer.RequestedSeed;
        }

        string requested = FullAppBridgeServer.RequestedCharacter;
        if (!string.IsNullOrWhiteSpace(requested))
        {
            try
            {
                CharacterModel? targetChar = ModelDb.AllCharacters.FirstOrDefault(c =>
                    c.Id.Entry.Equals(requested, StringComparison.OrdinalIgnoreCase) ||
                    c.Id.Entry.Equals($"CHARACTER.{requested}", StringComparison.OrdinalIgnoreCase) ||
                    c.Id.Entry.EndsWith(requested, StringComparison.OrdinalIgnoreCase));

                if (targetChar != null)
                {
                    __instance.SetLocalCharacter(targetChar);
                }
            }
            catch (Exception ex)
            {
                GD.PrintErr($"[FullAppBridge] Failed to set character {requested}: {ex.Message}");
            }
        }
    }

    private static bool HandleCombatAsync(Rng random, CancellationToken ct, ref Task __result)
    {
        __result = RunCombatLoopAsync(random, ct);
        return false;
    }

    private static async Task RunCombatLoopAsync(Rng random, CancellationToken ct)
    {
        CombatManager manager = CombatManager.Instance ?? throw new InvalidOperationException("CombatManager is unavailable.");
        RunManager runManager = RunManager.Instance ?? throw new InvalidOperationException("RunManager is unavailable.");

        await WaitHelper.Until(() => manager.IsInProgress, ct, TimeSpan.FromSeconds(15), "Combat did not start");
        Player player = LocalContext.GetMe(runManager.DebugOnlyGetState()) ?? throw new InvalidOperationException("Local player is unavailable.");

        while (manager.IsInProgress)
        {
            await WaitHelper.Until(
                () => player.PlayerCombatState?.Phase == PlayerTurnPhase.Play || !manager.IsInProgress,
                ct, TimeSpan.FromSeconds(30), "Player play phase did not begin");

            if (!manager.IsInProgress) break;

            while (player.PlayerCombatState?.Phase == PlayerTurnPhase.Play && manager.IsInProgress)
            {
                string actionId = await FullAppBridgeServer.WaitForCoordinatorActionAsync("combat", isTerminal: false, isVictory: false);
                if (actionId == "end_turn")
                {
                    PlayerCombatState state = player.PlayerCombatState!;
                    runManager.ActionQueueSynchronizer.RequestEnqueue(new EndPlayerTurnAction(player, state.TurnNumber));
                    await runManager.ActionExecutor.FinishedExecutingActions();
                    break;
                }
                else if (actionId.StartsWith("play_card:", StringComparison.Ordinal))
                {
                    string[] parts = actionId.Split(':');
                    int cardIndex = int.Parse(parts[1]);
                    var hand = PileType.Hand.GetPile(player).Cards;
                    if (cardIndex < 0 || cardIndex >= hand.Count)
                        throw new InvalidOperationException($"Invalid hand card index: {cardIndex}");

                    CardModel card = hand[cardIndex];
                    Creature? target = null;
                    if (parts.Length >= 4 && parts[2] == "target" && ulong.TryParse(parts[3], out ulong targetId))
                    {
                        target = card.CombatState?.HittableEnemies.FirstOrDefault(e => e.CombatId == targetId)
                            ?? manager.DebugOnlyGetState()?.HittableEnemies.FirstOrDefault(e => e.CombatId == targetId)
                            ?? card.CombatState?.HittableEnemies.FirstOrDefault()
                            ?? manager.DebugOnlyGetState()?.HittableEnemies.FirstOrDefault();
                    }
                    else if (card.TargetType.IsSingleTarget() && card.TargetType != TargetType.Self)
                    {
                        target = card.CombatState?.HittableEnemies.FirstOrDefault()
                            ?? manager.DebugOnlyGetState()?.HittableEnemies.FirstOrDefault();
                    }

                    if (card.TargetType.IsSingleTarget() && card.TargetType != TargetType.Self && target is null)
                    {
                        continue;
                    }

                    card.TryManualPlay(target);
                    await runManager.ActionExecutor.FinishedExecutingActions();
                }
                else if (actionId.StartsWith("use_potion:", StringComparison.Ordinal))
                {
                    string[] parts = actionId.Split(':');
                    int potionIndex = int.Parse(parts[1]);
                    var potions = player.PotionSlots;
                    if (potionIndex < 0 || potionIndex >= potions.Count || potions[potionIndex] is null)
                        throw new InvalidOperationException($"Invalid potion index: {potionIndex}");

                    PotionModel potion = potions[potionIndex]!;
                    Creature? target = null;
                    if (parts.Length >= 4 && parts[2] == "target" && ulong.TryParse(parts[3], out ulong targetId))
                    {
                        target = player.Creature.CombatId == targetId
                            ? player.Creature
                            : player.Creature.CombatState?.HittableEnemies.FirstOrDefault(e => e.CombatId == targetId)
                            ?? manager.DebugOnlyGetState()?.HittableEnemies.FirstOrDefault(e => e.CombatId == targetId)
                            ?? player.Creature.CombatState?.HittableEnemies.FirstOrDefault()
                            ?? manager.DebugOnlyGetState()?.HittableEnemies.FirstOrDefault();
                    }
                    else if (potion.TargetType.IsSingleTarget() && potion.TargetType != TargetType.Self)
                    {
                        target = player.Creature.CombatState?.HittableEnemies.FirstOrDefault()
                            ?? manager.DebugOnlyGetState()?.HittableEnemies.FirstOrDefault();
                    }

                    if (potion.TargetType.IsSingleTarget() && potion.TargetType != TargetType.Self && target is null)
                    {
                        continue;
                    }

                    // EnqueueManualUse depends on the interactive UI action queue
                    // and was a silent no-op in headless runs. Execute the native
                    // game action directly, as the persistent simulator does.
                    var usePotion = new UsePotionAction(potion, target, manager.IsInProgress);
                    runManager.ActionQueueSynchronizer.RequestEnqueue(usePotion);
                    await runManager.ActionExecutor.FinishedExecutingActions();
                }
            }
        }
    }

    private static bool HandleMapAsync(Rng random, CancellationToken ct, ref Task __result)
    {
        __result = RunMapLoopAsync(random, ct);
        return false;
    }

    private static async Task RunMapLoopAsync(Rng random, CancellationToken ct)
    {
        await WaitHelper.Until(() => NMapScreen.Instance != null && NMapScreen.Instance.IsOpen, ct, TimeSpan.FromSeconds(15), "Map screen not active");

        NMapScreen mapScreen = NMapScreen.Instance!;
        RunManager runManager = RunManager.Instance!;
        RunState runState = runManager.DebugOnlyGetState()!;

        var allMapPoints = UiHelper.FindAll<NMapPoint>(mapScreen);
        List<NMapPoint> travelableNodes;

        if (runState.VisitedMapCoords.Count == 0)
        {
            travelableNodes = allMapPoints
                .Where(p => p.Point.coord.row == 0)
                .OrderBy(p => p.Point.coord.col)
                .ToList();
        }
        else
        {
            MapCoord lastCoord = runState.VisitedMapCoords[^1];
            MapPoint? lastPoint = allMapPoints.FirstOrDefault(p => p.Point.coord == lastCoord)?.Point;
            if (lastPoint is not null && lastPoint.Children.Count > 0)
            {
                travelableNodes = allMapPoints
                    .Where(p => lastPoint.Children.Contains(p.Point))
                    .OrderBy(p => p.Point.coord.col)
                    .ToList();
            }
            else
            {
                travelableNodes = allMapPoints
                    .Where(p => p.State == MapPointState.Travelable)
                    .OrderBy(p => p.Point.coord.col)
                    .ToList();
            }
        }

        if (travelableNodes.Count == 0)
        {
            travelableNodes = allMapPoints
                .Where(p => p.State == MapPointState.Travelable)
                .OrderBy(p => p.Point.coord.col)
                .ToList();
        }

        var reachablePoints = travelableNodes.Select(n => n.Point).ToList();

        string actionId = await FullAppBridgeServer.WaitForCoordinatorActionAsync("map", isTerminal: false, isVictory: false, reachablePoints);
        string[] parts = actionId.Split(':');
        int rawIdx = int.Parse(parts[1]);
        int branchIdx = (rawIdx >= 1 && rawIdx <= travelableNodes.Count) ? rawIdx - 1 : rawIdx;
        if (branchIdx < 0 || branchIdx >= travelableNodes.Count)
            branchIdx = 0;

        NMapPoint chosenNode = travelableNodes[branchIdx];

        var roomEnteredTcs = new TaskCompletionSource<bool>();
        Action onRoomEntered = () => roomEnteredTcs.TrySetResult(true);
        runManager.RoomEntered += onRoomEntered;

        try
        {
            await UiHelper.Click(chosenNode, 0);
            await WaitHelper.ForTask(roomEnteredTcs.Task, ct, TimeSpan.FromSeconds(15), "Room was not entered after map selection");
        }
        finally
        {
            runManager.RoomEntered -= onRoomEntered;
        }
    }

    private static T? GetTopScreen<T>() where T : class
    {
        return NOverlayStack.Instance?.Peek() as T;
    }

    private static bool HandleCardRewardScreenAsync(Rng random, CancellationToken ct, ref Task __result)
    {
        __result = RunCardRewardLoopAsync(random, ct);
        return false;
    }

    private static async Task RunCardRewardLoopAsync(Rng random, CancellationToken ct)
    {
        await WaitHelper.Until(() => GetTopScreen<NCardRewardSelectionScreen>() != null, ct, TimeSpan.FromSeconds(15), "Card reward screen not open");
        NCardRewardSelectionScreen screen = GetTopScreen<NCardRewardSelectionScreen>()!;

        var cardHolders = UiHelper.FindAll<NCardHolder>(screen);
        List<CardModel> cardModels = cardHolders.Select(h => h.CardModel).Where(c => c != null).ToList()!;

        string actionId = await FullAppBridgeServer.WaitForCoordinatorActionAsync("card_reward", isTerminal: false, isVictory: false, cardModels);

        if (actionId == "skip_card" || actionId.StartsWith("skip", StringComparison.OrdinalIgnoreCase))
        {
            var skipBtn = UiHelper.FindFirst<NCardRewardAlternativeButton>(screen) ?? UiHelper.FindFirst<NClickableControl>(screen);
            if (skipBtn != null)
            {
                await UiHelper.Click(skipBtn, 0);
            }
        }
        else if (actionId.StartsWith("choose_card:", StringComparison.Ordinal))
        {
            string[] parts = actionId.Split(':');
            int cardIdx = int.Parse(parts[1]);
            if (cardIdx >= 0 && cardIdx < cardHolders.Count)
            {
                NCardHolder chosenHolder = cardHolders[cardIdx];
                chosenHolder.EmitSignal(NCardHolder.SignalName.Pressed, chosenHolder);
            }
            else if (cardHolders.Count > 0)
            {
                cardHolders[0].EmitSignal(NCardHolder.SignalName.Pressed, cardHolders[0]);
            }
        }

        await WaitHelper.Until(() => GetTopScreen<NCardRewardSelectionScreen>() == null, ct, TimeSpan.FromSeconds(15), "Card reward screen did not close");
    }

    private static bool HandleRewardsScreenAsync(Rng random, CancellationToken ct, ref Task __result)
    {
        __result = RunRewardsLoopAsync(random, ct);
        return false;
    }

    private static async Task RunRewardsLoopAsync(Rng random, CancellationToken ct)
    {
        await WaitHelper.Until(() => GetTopScreen<NRewardsScreen>() != null, ct, TimeSpan.FromSeconds(15), "Rewards screen not open");
        NRewardsScreen screen = GetTopScreen<NRewardsScreen>()!;

        while (GetTopScreen<NRewardsScreen>() != null)
        {
            var rewardButtons = UiHelper.FindAll<NRewardButton>(screen).Where(b => b.Reward != null && !b.Reward.SuccessfullySelected).ToList();
            var proceedBtn = UiHelper.FindFirst<NProceedButton>(screen);

            string actionId = await FullAppBridgeServer.WaitForCoordinatorActionAsync("rewards", isTerminal: false, isVictory: false, rewardButtons.Select(b => b.Reward).ToList());

            if (actionId == "proceed" || actionId.StartsWith("skip", StringComparison.OrdinalIgnoreCase))
            {
                if (proceedBtn != null)
                {
                    await UiHelper.Click(proceedBtn, 0);
                }
                break;
            }
            else if (actionId.StartsWith("choose_reward:", StringComparison.Ordinal))
            {
                string[] parts = actionId.Split(':');
                int rewardIdx = int.Parse(parts[1]);
                if (rewardIdx >= 0 && rewardIdx < rewardButtons.Count)
                {
                    NRewardButton chosenBtn = rewardButtons[rewardIdx];
                    await UiHelper.Click(chosenBtn, 0);
                    await Task.Delay(50);
                    if (GetTopScreen<NRewardsScreen>() == null)
                    {
                        break;
                    }
                }
            }
            else
            {
                if (proceedBtn != null)
                {
                    await UiHelper.Click(proceedBtn, 0);
                }
                break;
            }
        }

        await WaitHelper.Until(
            () => !GodotObject.IsInstanceValid(screen) || NOverlayStack.Instance?.Peek() != screen || (NMapScreen.Instance != null && NMapScreen.Instance.IsOpen),
            ct, TimeSpan.FromSeconds(15), "Rewards screen did not close");
    }

    private static bool HandleRestSiteAsync(Rng random, CancellationToken ct, ref Task __result)
    {
        __result = RunRestSiteLoopAsync(random, ct);
        return false;
    }

    private static async Task RunRestSiteLoopAsync(Rng random, CancellationToken ct)
    {
        await WaitHelper.Until(() => NRestSiteRoom.Instance != null, ct, TimeSpan.FromSeconds(15), "Rest site not loaded");
        NRestSiteRoom room = NRestSiteRoom.Instance!;
        var options = room.Options.ToList();

        string actionId = await FullAppBridgeServer.WaitForCoordinatorActionAsync("rest_site", isTerminal: false, isVictory: false, options);
        string optKey = actionId.Replace("choose_rest:", "");
        RestSiteOption? chosen = options.FirstOrDefault(o => o.OptionId.Equals(optKey, StringComparison.OrdinalIgnoreCase))
            ?? options.FirstOrDefault(o => o.OptionId.Contains(optKey, StringComparison.OrdinalIgnoreCase))
            ?? options.FirstOrDefault();

        if (chosen != null)
        {
            // UI buttons run the native option mutation first, then update the
            // room presentation. Calling AfterSelectingOption alone skips Heal,
            // Smith, and other native effects and can fault the headless room.
            bool selected = await chosen.OnSelect();
            if (selected)
            {
                room.AfterSelectingOption(chosen);
            }
            await Task.Delay(50);
        }

        while (NOverlayStack.Instance?.ScreenCount > 0)
        {
            await Task.Delay(20);
        }

        if (room.ProceedButton != null)
        {
            await WaitHelper.Until(() => room.ProceedButton.IsEnabled, ct, TimeSpan.FromSeconds(10), "Rest site proceed button not enabled");
            await UiHelper.Click(room.ProceedButton, 0);
        }
    }

    private static bool HandleDeckUpgradeScreenAsync(Rng random, CancellationToken ct, ref Task __result)
    {
        __result = RunDeckUpgradeLoopAsync(random, ct);
        return false;
    }

    private static async Task RunDeckUpgradeLoopAsync(Rng random, CancellationToken ct)
    {
        await WaitHelper.Until(() => GetTopScreen<NDeckUpgradeSelectScreen>() != null, ct, TimeSpan.FromSeconds(15), "Deck upgrade screen not open");
        NDeckUpgradeSelectScreen screen = GetTopScreen<NDeckUpgradeSelectScreen>()!;

        var cardHolders = UiHelper.FindAll<NGridCardHolder>(screen).Where(h => h.CardModel != null && h.CardModel.CurrentUpgradeLevel < h.CardModel.MaxUpgradeLevel).ToList();
        List<CardModel> upgradableCards = cardHolders.Select(h => h.CardModel).ToList();

        string actionId = await FullAppBridgeServer.WaitForCoordinatorActionAsync("deck_upgrade", isTerminal: false, isVictory: false, upgradableCards);

        int cardIdx = 0;
        if (actionId.StartsWith("choose_upgrade:", StringComparison.Ordinal))
        {
            string[] parts = actionId.Split(':');
            int.TryParse(parts[1], out cardIdx);
        }

        if (cardIdx >= 0 && cardIdx < cardHolders.Count)
        {
            NGridCardHolder chosenHolder = cardHolders[cardIdx];
            chosenHolder.EmitSignal(NCardHolder.SignalName.Pressed, chosenHolder);
        }
        else if (cardHolders.Count > 0)
        {
            NGridCardHolder chosenHolder = cardHolders[0];
            chosenHolder.EmitSignal(NCardHolder.SignalName.Pressed, chosenHolder);
        }

        await Task.Delay(50);
        var confirmBtn = UiHelper.FindFirst<NConfirmButton>(screen);
        if (confirmBtn != null)
        {
            await WaitHelper.Until(() => confirmBtn.IsEnabled, ct, TimeSpan.FromSeconds(10), "Upgrade confirm button not enabled");
            await UiHelper.Click(confirmBtn, 0);
        }

        await WaitHelper.Until(() => GetTopScreen<NDeckUpgradeSelectScreen>() == null, ct, TimeSpan.FromSeconds(15), "Upgrade screen did not close");
    }

    private static bool HandleDeckCardSelectScreenAsync(Rng random, CancellationToken ct, ref Task __result)
    {
        __result = RunDeckCardSelectLoopAsync(random, ct);
        return false;
    }

    private static async Task RunDeckCardSelectLoopAsync(Rng random, CancellationToken ct)
    {
        await WaitHelper.Until(() => GetTopScreen<NDeckCardSelectScreen>() != null, ct, TimeSpan.FromSeconds(15), "Deck card select screen not open");
        NDeckCardSelectScreen screen = GetTopScreen<NDeckCardSelectScreen>()!;

        var cardHolders = UiHelper.FindAll<NGridCardHolder>(screen).Where(h => h.CardModel != null).ToList();
        List<CardModel> selectableCards = cardHolders.Select(h => h.CardModel).ToList();

        string actionId = await FullAppBridgeServer.WaitForCoordinatorActionAsync("deck_card_select", isTerminal: false, isVictory: false, selectableCards);

        int cardIdx = 0;
        if (actionId.StartsWith("choose_card_select:", StringComparison.Ordinal))
        {
            string[] parts = actionId.Split(':');
            int.TryParse(parts[1], out cardIdx);
        }

        if (cardIdx >= 0 && cardIdx < cardHolders.Count)
        {
            NGridCardHolder chosenHolder = cardHolders[cardIdx];
            chosenHolder.EmitSignal(NCardHolder.SignalName.Pressed, chosenHolder);
        }
        else if (cardHolders.Count > 0)
        {
            NGridCardHolder chosenHolder = cardHolders[0];
            chosenHolder.EmitSignal(NCardHolder.SignalName.Pressed, chosenHolder);
        }

        await Task.Delay(50);
        var confirmBtn = UiHelper.FindFirst<NConfirmButton>(screen);
        if (confirmBtn != null)
        {
            await WaitHelper.Until(() => confirmBtn.IsEnabled, ct, TimeSpan.FromSeconds(10), "Card select confirm button not enabled");
            await UiHelper.Click(confirmBtn, 0);
        }

        await WaitHelper.Until(() => GetTopScreen<NDeckCardSelectScreen>() == null, ct, TimeSpan.FromSeconds(15), "Deck card select screen did not close");
    }

    private static bool HandleSimpleCardSelectScreenAsync(Rng random, CancellationToken ct, ref Task __result)
    {
        __result = RunSimpleCardSelectLoopAsync(random, ct);
        return false;
    }

    private static async Task RunSimpleCardSelectLoopAsync(Rng random, CancellationToken ct)
    {
        await WaitHelper.Until(() => GetTopScreen<NSimpleCardSelectScreen>() != null, ct, TimeSpan.FromSeconds(15), "Simple card select screen not open");
        NSimpleCardSelectScreen screen = GetTopScreen<NSimpleCardSelectScreen>()!;

        var cardHolders = UiHelper.FindAll<NGridCardHolder>(screen).Where(h => h.CardModel != null).ToList();
        List<CardModel> selectableCards = cardHolders.Select(h => h.CardModel).ToList();

        string actionId = await FullAppBridgeServer.WaitForCoordinatorActionAsync("simple_card_select", isTerminal: false, isVictory: false, selectableCards);

        int cardIdx = 0;
        if (actionId.StartsWith("choose_card_select:", StringComparison.Ordinal))
        {
            string[] parts = actionId.Split(':');
            int.TryParse(parts[1], out cardIdx);
        }

        if (cardIdx >= 0 && cardIdx < cardHolders.Count)
        {
            NGridCardHolder chosenHolder = cardHolders[cardIdx];
            chosenHolder.EmitSignal(NCardHolder.SignalName.Pressed, chosenHolder);
        }
        else if (cardHolders.Count > 0)
        {
            NGridCardHolder chosenHolder = cardHolders[0];
            chosenHolder.EmitSignal(NCardHolder.SignalName.Pressed, chosenHolder);
        }

        await Task.Delay(50);
        var confirmBtn = UiHelper.FindFirst<NConfirmButton>(screen);
        if (confirmBtn != null)
        {
            await WaitHelper.Until(() => confirmBtn.IsEnabled, ct, TimeSpan.FromSeconds(10), "Confirm button not enabled");
            await UiHelper.Click(confirmBtn, 0);
        }

        await WaitHelper.Until(() => GetTopScreen<NSimpleCardSelectScreen>() == null, ct, TimeSpan.FromSeconds(15), "Simple card select screen did not close");
    }

    private static bool HandleShopRoomAsync(Rng random, CancellationToken ct, ref Task __result)
    {
        __result = RunShopRoomLoopAsync(random, ct);
        return false;
    }

    private static async Task RunShopRoomLoopAsync(Rng random, CancellationToken ct)
    {
        await WaitHelper.ForNode<NMerchantRoom>(((SceneTree)Engine.GetMainLoop()).Root, "/root/Game/RootSceneContainer/Run/RoomContainer/MerchantRoom", ct, TimeSpan.FromSeconds(15));
        NMerchantRoom merchantRoom = (NMerchantRoom)((SceneTree)Engine.GetMainLoop()).Root.GetNode("/root/Game/RootSceneContainer/Run/RoomContainer/MerchantRoom");

        merchantRoom.OpenInventory();
        await Task.Delay(50);

        while (true)
        {
            var inventory = merchantRoom.Inventory;
            var allSlots = inventory.GetAllSlots().Where(s => s.Entry != null && s.Entry.EnoughGold && s.Entry.IsStocked).ToList();

            string actionId = await FullAppBridgeServer.WaitForCoordinatorActionAsync("shop", isTerminal: false, isVictory: false, allSlots.Select(s => s.Entry).ToList());

            if (actionId == "shop_leave" || actionId == "proceed" || actionId.StartsWith("leave", StringComparison.OrdinalIgnoreCase))
            {
                var backBtn = UiHelper.FindFirst<NBackButton>(inventory);
                if (backBtn != null)
                {
                    await UiHelper.Click(backBtn, 0);
                }
                break;
            }
            else if (actionId.StartsWith("shop_buy:", StringComparison.Ordinal))
            {
                string[] parts = actionId.Split(':');
                int slotIdx = int.Parse(parts[1]);
                if (slotIdx >= 0 && slotIdx < allSlots.Count)
                {
                    NMerchantSlot chosenSlot = allSlots[slotIdx];
                    await chosenSlot.Entry.OnTryPurchaseWrapper(inventory.Inventory);
                    await Task.Delay(50);
                }
            }
            else
            {
                var backBtn = UiHelper.FindFirst<NBackButton>(inventory);
                if (backBtn != null)
                {
                    await UiHelper.Click(backBtn, 0);
                }
                break;
            }
        }

        if (merchantRoom.ProceedButton != null)
        {
            await WaitHelper.Until(() => merchantRoom.ProceedButton.IsEnabled, ct, TimeSpan.FromSeconds(10), "Shop proceed button not enabled");
            await UiHelper.Click(merchantRoom.ProceedButton, 0);
        }
    }

    private static bool HandleEventRoomAsync(Rng random, CancellationToken ct, ref Task __result)
    {
        __result = RunEventRoomLoopAsync(random, ct);
        return false;
    }

    private static async Task RunEventRoomLoopAsync(Rng random, CancellationToken ct)
    {
        await WaitHelper.ForNode<Node>(((SceneTree)Engine.GetMainLoop()).Root, "/root/Game/RootSceneContainer/Run/RoomContainer/EventRoom", ct, TimeSpan.FromSeconds(15));
        Node eventRoom = ((SceneTree)Engine.GetMainLoop()).Root.GetNode("/root/Game/RootSceneContainer/Run/RoomContainer/EventRoom");

        while (GodotObject.IsInstanceValid(eventRoom) && eventRoom.IsInsideTree())
        {
            RunState? runState = RunManager.Instance?.DebugOnlyGetState();
            if (runState == null || runState.BaseRoom == null || runState.BaseRoom.RoomType != RoomType.Event) break;

            var optionButtons = UiHelper.FindAll<NEventOptionButton>(eventRoom).Where(b => b.Option != null && !b.Option.IsLocked).ToList();
            if (optionButtons.Count == 0) break;

            string actionId = await FullAppBridgeServer.WaitForCoordinatorActionAsync("event", isTerminal: false, isVictory: false, optionButtons.Select(b => b.Option).ToList());

            int choiceIdx = 0;
            if (actionId.StartsWith("choose_event:", StringComparison.Ordinal))
            {
                string[] parts = actionId.Split(':');
                int.TryParse(parts[1], out choiceIdx);
            }

            if (choiceIdx < 0 || choiceIdx >= optionButtons.Count)
                choiceIdx = 0;

            NEventOptionButton chosenBtn = optionButtons[choiceIdx];
            bool isProceed = chosenBtn.Option.IsProceed;

            await UiHelper.Click(chosenBtn, 0);
            await Task.Delay(50);

            if (isProceed) break;
        }
    }

    private static bool HandleTreasureRoomAsync(Rng random, CancellationToken ct, ref Task __result)
    {
        __result = RunTreasureRoomLoopAsync(random, ct);
        return false;
    }

    private static async Task RunTreasureRoomLoopAsync(Rng random, CancellationToken ct)
    {
        await WaitHelper.ForNode<NTreasureRoom>(((SceneTree)Engine.GetMainLoop()).Root, "/root/Game/RootSceneContainer/Run/RoomContainer/TreasureRoom", ct, TimeSpan.FromSeconds(15));
        NTreasureRoom treasureRoom = (NTreasureRoom)((SceneTree)Engine.GetMainLoop()).Root.GetNode("/root/Game/RootSceneContainer/Run/RoomContainer/TreasureRoom");

        var chest = treasureRoom.GetNodeOrNull<NClickableControl>("Chest");
        if (chest != null && chest.IsEnabled)
        {
            await UiHelper.Click(chest, 0);
            await Task.Delay(50);
        }

        var relics = UiHelper.FindAll<NClickableControl>(treasureRoom).Where(r => r.IsEnabled && r.Visible && r.Name.ToString().Contains("Relic", StringComparison.OrdinalIgnoreCase)).ToList();
        foreach (var relic in relics)
        {
            await UiHelper.Click(relic, 0);
            await Task.Delay(20);
        }

        string actionId = await FullAppBridgeServer.WaitForCoordinatorActionAsync("treasure", isTerminal: false, isVictory: false);

        if (treasureRoom.ProceedButton != null)
        {
            await WaitHelper.Until(() => treasureRoom.ProceedButton.IsEnabled, ct, TimeSpan.FromSeconds(10), "Treasure proceed button not enabled");
            await UiHelper.Click(treasureRoom.ProceedButton, 0);
        }
    }

    private static bool HandleVictoryRoomAsync(Rng random, CancellationToken ct, ref Task __result)
    {
        __result = RunVictoryRoomLoopAsync(random, ct);
        return false;
    }

    private static async Task RunVictoryRoomLoopAsync(Rng random, CancellationToken ct)
    {
        await FullAppBridgeServer.WaitForCoordinatorActionAsync("victory", isTerminal: true, isVictory: true);
    }

    private static bool HandleGameOverAsync(Rng random, CancellationToken ct, ref Task __result)
    {
        __result = RunGameOverLoopAsync(random, ct);
        return false;
    }

    private static async Task RunGameOverLoopAsync(Rng random, CancellationToken ct)
    {
        await FullAppBridgeServer.WaitForCoordinatorActionAsync("game_over", isTerminal: true, isVictory: false);
    }

    private static bool HandleWaitForRewardsScreenAsync(CancellationToken ct, ref Task __result)
    {
        __result = SafeWaitForRewardsOrGameOverScreenAsync(ct);
        return false;
    }

    private static async Task SafeWaitForRewardsOrGameOverScreenAsync(CancellationToken ct)
    {
        await WaitHelper.Until(
            () => GetTopScreen<NRewardsScreen>() != null || GetTopScreen<MegaCrit.Sts2.Core.Nodes.Screens.GameOverScreen.NGameOverScreen>() != null,
            ct, TimeSpan.FromSeconds(15), "Neither rewards nor game over screen appeared after combat");
    }
}
