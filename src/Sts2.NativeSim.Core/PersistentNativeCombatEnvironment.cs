using System.Collections;
using System.Diagnostics;
using System.Linq.Expressions;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Security.Cryptography;
using System.Text.Json;
using Sts2.NativeSim.Protocol;

namespace Sts2.NativeSim.Core;

/// Persistent, non-certifying native combat worker. Reflection is only an ABI adapter;
/// every gameplay transition is dispatched into the shipped game assembly.
public sealed class PersistentNativeCombatEnvironment : IDisposable
{
    private static PersistentNativeCombatEnvironment? _activeEnvironment;
    private static bool _eventPresentationScope;
    private static Task? _scopedScheduledTask;
    private readonly NativeAssemblyContext _context;
    private readonly string _assemblyHash, _pckHash, _productVersion;
    // A complete combat rollout can emit dozens of resident branch handles.
    // Keep enough LRU capacity for the root plus retained winners across a
    // 64-candidate fitness search; eviction must not change which winner can
    // be selected.
    private static readonly int BranchCapacity = int.TryParse(System.Environment.GetEnvironmentVariable("STS2_BRANCH_CAPACITY"), out int cap) && cap > 0 ? cap : 8192;
    private readonly Dictionary<string, Branch> _branches = new(StringComparer.Ordinal);
    private readonly LinkedList<string> _branchOrder = new();
    private readonly Dictionary<object, string> _cardInstanceIds = new(ReferenceEqualityComparer.Instance);
    private readonly Dictionary<uint, object> _combatCreaturesById = new();
    private string _lastSnapshotDebug = "";
    private int _dynamicCardOrdinal;
    private object? _run, _player, _combat, _manager, _pcs;
    private IDisposable? _selectorScope;
    private PendingNativeChoice? _pendingChoice;
    private Task? _continuationTask;
    private TaskCompletionSource? _choiceBegun;
    private int _choiceOrdinal;
    private ResetRequest? _reset;
    private readonly List<string> _history = [];
    private string? _currentBranchHandle;
    private string? _lastActionId;
#pragma warning disable CS0649
    private bool _isPoisoned;
#pragma warning restore CS0649
    private string _hash = "";
    private bool _runServicesInitialized;
    private bool _mapMode;
    private bool _rewardMode;
    private object? _cardReward;
    private string _rewardKind = "card";
    private string? _rewardModelId;
    private int? _rewardSelectionIndex;
    private bool _rewardCompleted;
    private bool _restMode;
    private object[] _restOptions = [];
    private bool _restSelectionStarted;
    private bool _eventMode;
    private string? _eventId;
    private object? _event;
    private bool _runMode;
    private string _runStage = "map";
    private bool _runWon;
    private object? _roomRewardsSet;
    private readonly HashSet<int> _resolvedRoomRewards = [];
    private int? _pendingRoomRewardIndex;
    private object? _treasureRoom, _treasureSynchronizer;
    private bool _treasureOpened, _treasureResolved;
    private object? _merchantRoom, _merchantInventory;
    private readonly Dictionary<object, (string Kind, string? ModelId)> _merchantEntryIdentities = new(ReferenceEqualityComparer.Instance);
    private object? _pendingRewardsSet;
    private PendingRewardSelection? _pendingRewardSelection;
    private bool _customRewardMode, _customRewardsLinked;
    private string[] _customRewardKinds = [];

    public PersistentNativeCombatEnvironment(string assemblyPath, string pckPath)
    {
        if (Interlocked.CompareExchange(ref _activeEnvironment, this, null) is not null)
            throw new InvalidOperationException("PersistentNativeCombatEnvironment is a process singleton; an active instance already exists.");
        assemblyPath = Path.GetFullPath(assemblyPath);
        _assemblyHash = ReflectionTools.HashFile(assemblyPath);
        _pckHash = ReflectionTools.HashFile(pckPath);
        _productVersion = FileVersionInfo.GetVersionInfo(assemblyPath).ProductVersion ?? "unknown";
        _context = new(assemblyPath);
        InitializeOnce();
    }

    public object Hello() => new
    {
        protocol_version = ProtocolConstants.Version, observation_schema_version = ProtocolConstants.ObservationSchemaVersion,
        server = "sts2-native-sim-godot", persistent = true, certifying = false,
        game_build = new { version = _productVersion, assembly_sha256 = _assemblyHash, pck_sha256 = _pckHash },
        methods = new[] { "hello", "catalog", "reset", "run_reset", "map_reset", "reward_reset", "item_reward_reset", "custom_reward_reset", "rest_reset", "event_reset", "observe", "run_observe", "map_observe", "reward_observe", "custom_reward_observe", "rest_observe", "event_observe", "legal_actions", "step", "run_step", "map_step", "reward_step", "custom_reward_step", "rest_step", "event_step", "fork", "restore", "diagnostics", "close" },
        supported_subset = new { characters = "native CharacterModel entries", encounters = "native EncounterModel entries", cards = "base/upgraded cards plus asynchronous native card, bundle, and relic choices", actions = new[] { "play_card", "use_potion", "discard_potion", "end_turn", "choose_cards", "choose_option", "choose_map", "choose_reward", "choose_rest", "choose_event", "open_treasure", "choose_treasure", "buy_shop", "choose_custom_reward", "skip_custom_rewards", "advance_act" }, potions = true, map = "native deterministic routing graph with composed combat, rest, event, treasure, shop, and inter-act transitions", events = "native model initialization, option continuations, nested event-created combats, blocking custom/linked rewards, and the final victory event" }
    };

    public object Catalog()
    {
        Type db = T("MegaCrit.Sts2.Core.Models.ModelDb");
        object[] Models(string collection) => ReflectionTools.Enumerate(ReflectionTools.GetStatic(db, collection))
            .Where(model => model is not null)
            .Select(model => model!)
            .OrderBy(Entry, StringComparer.Ordinal)
            .Select(model => (object)new { model_id = Entry(model), runtime_type = model.GetType().FullName })
            .ToArray();
        object[] Cards() => ReflectionTools.Enumerate(ReflectionTools.GetStatic(db, "AllCards"))
            .Where(model => model is not null)
            .Select(model => model!)
            .OrderBy(Entry, StringComparer.Ordinal)
            .Select(card => (object)new
            {
                model_id = Entry(card), runtime_type = card.GetType().FullName,
                card_type = ReflectionTools.Get(card, "Type")?.ToString(),
                target_type = ReflectionTools.Get(card, "TargetType")?.ToString(),
                rarity = ReflectionTools.Get(card, "Rarity")?.ToString(),
                color = ReflectionTools.Get(card, "Color")?.ToString()
            }).ToArray();
        object[] Encounters()
        {
            Dictionary<string, HashSet<int>> actsByEncounter = new(StringComparer.Ordinal);
            object? actsByIndex = ReflectionTools.GetStatic(db, "ActsByIndex");
            int actIndex = 0;
            foreach (object? acts in ReflectionTools.Enumerate(actsByIndex))
            {
                foreach (object? act in ReflectionTools.Enumerate(acts))
                {
                    if (act is null) continue;
                    foreach (object? encounter in ReflectionTools.Enumerate(ReflectionTools.Get(act, "AllEncounters")))
                    {
                        if (encounter is null) continue;
                        string id = Entry(encounter);
                        if (!actsByEncounter.TryGetValue(id, out HashSet<int>? indices)) actsByEncounter[id] = indices = [];
                        indices.Add(actIndex + 1);
                    }
                }
                actIndex++;
            }
            return ReflectionTools.Enumerate(ReflectionTools.GetStatic(db, "AllEncounters"))
                .Where(model => model is not null)
                .Select(model => model!)
                .OrderBy(Entry, StringComparer.Ordinal)
                .Select(encounter => (object)new
                {
                    model_id = Entry(encounter), runtime_type = encounter.GetType().FullName,
                    room_type = ReflectionTools.Get(encounter, "RoomType")?.ToString(),
                    is_weak = ReflectionTools.Get(encounter, "IsWeak"),
                    act_indices = actsByEncounter.TryGetValue(Entry(encounter), out HashSet<int>? indices) ? indices.Order().ToArray() : []
                }).ToArray();
        }
        return new
        {
            game_build = new { version = _productVersion, assembly_sha256 = _assemblyHash, pck_sha256 = _pckHash },
            cards = Cards(), encounters = Encounters(), relics = Models("AllRelics"),
            potions = Models("AllPotions"), characters = Models("AllCharacters"),
            enchantments = Models("DebugEnchantments"), events = Models("AllEvents")
        };
    }

    public EnvironmentResult Reset(ResetRequest request)
    {
        ThrowIfPoisoned();
        QuiesceOutstandingTransition();
        _branches.Clear();
        _branchOrder.Clear();
        _cardInstanceIds.Clear();
        _combatCreaturesById.Clear();
        GC.Collect(2, GCCollectionMode.Aggressive, blocking: true, compacting: true);
        _runMode = false; _runStage = "map"; _runWon = false; _roomRewardsSet = null; _resolvedRoomRewards.Clear(); _pendingRoomRewardIndex = null; _pendingRewardsSet = null; _pendingRewardSelection = null; _customRewardMode = false; _customRewardsLinked = false; _customRewardKinds = []; _treasureRoom = null; _treasureSynchronizer = null; _treasureOpened = false; _treasureResolved = false; _merchantRoom = null; _merchantInventory = null; _merchantEntryIdentities.Clear(); _mapMode = false; _rewardMode = false; _rewardKind = "card"; _rewardModelId = null; _cardReward = null; _restMode = false; _eventMode = false; _eventId = null; _event = null; Validate(request); _reset = request; _history.Clear(); _currentBranchHandle = null; _lastActionId = null; Construct(request);
        try
        {
            object? runManager = ReflectionTools.GetStatic(T("MegaCrit.Sts2.Core.Runs.RunManager"), "Instance");
            if (runManager is not null)
            {
                object? synchronizer = ReflectionTools.Get(runManager, "RewardsSetSynchronizer");
                if (synchronizer is not null)
                    ReflectionTools.Invoke(synchronizer, "BeforeLeavingRoom");
            }
        }
        catch { }
        return Capture(new { kind = "reset", replayed_actions = 0 });
    }
    public EnvironmentResult RunReset(ResetRequest request)
    {
        ThrowIfPoisoned();
        Reset(request); _runMode = true; _runStage = "map"; InitializeRunMap();
        return Capture(new { kind = "run_reset", replayed_actions = 0 });
    }
    public EnvironmentResult MapReset(ResetRequest request)
    {
        ThrowIfPoisoned();
        Reset(request); _mapMode = true; InitializeMap();
        return Capture(new { kind = "map_reset", replayed_actions = 0 });
    }
    public EnvironmentResult RewardReset(ResetRequest request)
    {
        ThrowIfPoisoned();
        Reset(request); _rewardMode = true; _rewardKind = "card"; _rewardModelId = null; InitializeReward();
        return Capture(new { kind = "reward_reset", replayed_actions = 0 });
    }
    public EnvironmentResult ItemRewardReset(ItemRewardResetRequest request)
    {
        ThrowIfPoisoned();
        if (request.RewardKind is not ("relic" or "potion")) throw new ProtocolException("invalid_reward_kind", request.RewardKind);
        Reset(request.State); _rewardMode = true; _rewardKind = request.RewardKind; _rewardModelId = request.ModelId; InitializeReward();
        return Capture(new { kind = "item_reward_reset", reward_kind = _rewardKind, model_id = _rewardModelId, replayed_actions = 0 });
    }
    public async Task<EnvironmentResult> CustomRewardResetAsync(CustomRewardResetRequest request)
    {
        ThrowIfPoisoned();
        Reset(request.State); _customRewardMode = true; _customRewardsLinked = request.Linked; _customRewardKinds = request.RewardKinds.ToArray();
        await InitializeCustomRewardsAsync();
        return Capture(new { kind = "custom_reward_reset", linked = _customRewardsLinked, replayed_actions = 0 });
    }
    public EnvironmentResult RestReset(ResetRequest request)
    {
        ThrowIfPoisoned();
        Reset(request); _restMode = true; InitializeRestSite();
        return Capture(new { kind = "rest_reset", replayed_actions = 0 });
    }
    public async Task<EnvironmentResult> EventResetAsync(EventResetRequest request)
    {
        ThrowIfPoisoned();
        Reset(request.State); _eventMode = true; _eventId = request.EventId; await InitializeEventAsync();
        return Capture(new { kind = "event_reset", event_id = _eventId, replayed_actions = 0 });
    }
    public EnvironmentResult Observe() { ThrowIfPoisoned(); return Capture(null); }
    public IReadOnlyList<LegalAction> LegalActions() { ThrowIfPoisoned(); return BuildActions(); }

    public async Task<EnvironmentResult> StepAsync(string actionId, bool record = true)
    {
        ThrowIfPoisoned();
        Stopwatch timer = Stopwatch.StartNew();
        // Validate legality before assigning _lastActionId so that a rejected
        // action never contaminates the branch edge recorded by GetOrAddCurrentBranch().
        LegalAction action = BuildActions().SingleOrDefault(x => x.ActionId == actionId)
            ?? throw new ProtocolException("invalid_action", $"Action '{actionId}' is not legal in state {_hash}.");
        _lastActionId = actionId;
        if (action.Kind == "play_card")
            await StartTransitionAsync(() => PlayAsync(Convert.ToUInt32(action.Parameters["card_id"]), action.Parameters["target_id"] is null ? null : Convert.ToUInt32(action.Parameters["target_id"])));
        else if (action.Kind == "end_turn") await StartTransitionAsync(EndTurnAsync);
        else if (action.Kind == "use_potion")
            await StartTransitionAsync(() => UsePotionAsync(Convert.ToInt32(action.Parameters["slot"]), action.Parameters["target_id"] is null ? null : Convert.ToUInt32(action.Parameters["target_id"])));
        else if (action.Kind == "discard_potion") await StartTransitionAsync(() => DiscardPotionAsync(Convert.ToInt32(action.Parameters["slot"])));
        else if (action.Kind is "choose_cards" or "choose_option") await ResumeChoiceAsync((string[])action.Parameters["option_ids"]!);
        else if (action.Kind == "choose_map")
        {
            int col = Convert.ToInt32(action.Parameters["col"]), row = Convert.ToInt32(action.Parameters["row"]);
            if (_runMode) await EnterRunMapCoordAsync(col, row); else ChooseMap(col, row);
        }
        else if (action.Kind == "choose_reward") await ChooseRewardAsync(Convert.ToInt32(action.Parameters["option_index"]));
        else if (action.Kind == "choose_rest") await ChooseRestAsync((string)action.Parameters["option_id"]!);
        else if (action.Kind == "choose_event") await ChooseEventAsync(Convert.ToInt32(action.Parameters["option_index"]));
        else if (action.Kind == "generate_room_rewards") await GenerateRoomRewardsAsync();
        else if (action.Kind == "choose_room_reward") await ChooseRoomRewardAsync(Convert.ToInt32(action.Parameters["reward_index"]), Convert.ToInt32(action.Parameters["option_index"]));
        else if (action.Kind == "leave_room_rewards") await LeaveRoomRewardsAsync();
        else if (action.Kind == "advance_act") await AdvanceActAsync();
        else if (action.Kind == "leave_event") await LeaveCurrentRunRoomAsync("event");
        else if (action.Kind == "leave_rest") await LeaveCurrentRunRoomAsync("rest");
        else if (action.Kind == "open_treasure") await OpenTreasureAsync();
        else if (action.Kind == "choose_treasure") await ChooseTreasureAsync(Convert.ToInt32(action.Parameters["option_index"]));
        else if (action.Kind == "skip_treasure") await ChooseTreasureAsync(null);
        else if (action.Kind == "leave_treasure") await LeaveCurrentRunRoomAsync("treasure");
        else if (action.Kind == "buy_shop") await BuyShopAsync(Convert.ToInt32(action.Parameters["entry_index"]));
        else if (action.Kind == "leave_shop") await LeaveCurrentRunRoomAsync("shop");
        else if (action.Kind == "choose_custom_reward") await ChooseCustomRewardAsync(Convert.ToInt32(action.Parameters["reward_index"]), Convert.ToInt32(action.Parameters["child_index"]), Convert.ToInt32(action.Parameters["option_index"]));
        else if (action.Kind == "skip_custom_rewards") await SkipCustomRewardsAsync();
        else throw new ProtocolException("unsupported_action", action.Kind);
        if (record) _history.Add(actionId);
        timer.Stop();
        // Capture consumes _lastActionId as the transition edge label.  Clear it
        // immediately after so that a subsequent Observe() or Fork() cannot
        // replay the same edge and spawn a redundant child branch node.
        try
        {
            return Capture(new { kind = action.Kind, action_id = actionId, elapsed_ms = timer.Elapsed.TotalMilliseconds, history_length = _history.Count });
        }
        finally
        {
            _lastActionId = null;
        }
    }

    public string Fork() => GetOrAddCurrentBranch();
    public object Diagnostics() => new { branch_count = _branches.Count, branch_capacity = BranchCapacity, history_length = _history.Count, current_state_hash = _hash, last_snapshot_debug = _lastSnapshotDebug };

    public async Task<EnvironmentResult> RestoreAsync(string id)
    {
        ThrowIfPoisoned();
        if (!_branches.TryGetValue(id, out Branch? branch)) throw new ProtocolException("unknown_state_handle", id);
        List<string> branchHistory = ResolveBranchHistory(branch);
        if (StringComparer.Ordinal.Equals(_hash, branch.ExpectedHash) && _history.SequenceEqual(branchHistory, StringComparer.Ordinal))
        {
            _currentBranchHandle = id;
            return Capture(new { kind = "restore", replayed_actions = 0, resident_prefix_hit = true, elapsed_ms = 0.0 });
        }
        QuiesceOutstandingTransition();
        Stopwatch timer = Stopwatch.StartNew();

        if (branch.CombatSnapshot is not null)
        {
            if (RestoreCombatSnapshot(branch.CombatSnapshot))
            {
                _history.Clear();
                _history.AddRange(branchHistory);
                _currentBranchHandle = id;
                EnvironmentResult snapResult = Capture(null);
                if (StringComparer.Ordinal.Equals(snapResult.StateHash, branch.ExpectedHash))
                {
                    timer.Stop();
                    _lastSnapshotDebug = "snapshot_success";
                    return snapResult with { Transition = new { kind = "snapshot_restore", replayed_actions = 0, elapsed_ms = timer.Elapsed.TotalMilliseconds } };
                }
                else
                {
                    _lastSnapshotDebug = $"hash_mismatch: expected={branch.ExpectedHash}, got={snapResult.StateHash}";
                }
            }
            else
            {
                _lastSnapshotDebug = $"restore_failed: {_lastSnapshotDebug}";
            }
        }
        else
        {
            _lastSnapshotDebug = "snapshot_was_null";
        }

        _reset = branch.Reset; _history.Clear(); _cardInstanceIds.Clear(); _combatCreaturesById.Clear(); _dynamicCardOrdinal = 0; _currentBranchHandle = null; _lastActionId = null; Construct(branch.Reset); _runMode = branch.RunMode; _runStage = "map"; _pendingRewardsSet = null; _pendingRewardSelection = null;
        _mapMode = !_runMode && branch.MapMode; _rewardMode = !_runMode && branch.RewardMode; _rewardKind = branch.RewardKind; _rewardModelId = branch.RewardModelId; _restMode = !_runMode && branch.RestMode; _eventMode = !_runMode && branch.EventMode; _eventId = branch.EventId;
        _customRewardMode = branch.CustomRewardMode; _customRewardsLinked = branch.CustomRewardsLinked; _customRewardKinds = branch.CustomRewardKinds;
        if (_runMode) InitializeRunMap();
        else if (_mapMode) InitializeMap();
        if (_rewardMode) InitializeReward();
        if (_restMode) InitializeRestSite();
        if (_eventMode) await InitializeEventAsync();
        if (_customRewardMode) await InitializeCustomRewardsAsync();
        foreach (string action in branchHistory) { await StepAsync(action, false); _history.Add(action); }
        _currentBranchHandle = id;
        EnvironmentResult result = Capture(null);
        if (!StringComparer.Ordinal.Equals(result.StateHash, branch.ExpectedHash))
            throw new ProtocolException("replay_divergence", $"Expected {branch.ExpectedHash}, obtained {result.StateHash}.", new { history_length = branchHistory.Count });
        timer.Stop(); return result with { Transition = new { kind = "restore", replayed_actions = branchHistory.Count, elapsed_ms = timer.Elapsed.TotalMilliseconds } };
    }

    private void InitializeOnce()
    {
        ReflectionTools.SetStatic(T("MegaCrit.Sts2.Core.Modding.ModManager"), "State", Enum.Parse(T("MegaCrit.Sts2.Core.Modding.ModManagerState"), "Skipped"));
        ReflectionTools.InvokeStatic(T("MegaCrit.Sts2.Core.Models.ModelDb"), "Init");
        InstallSaveMock(); ReflectionTools.InvokeStatic(T("MegaCrit.Sts2.Core.Localization.LocManager"), "Initialize");
        ReflectionTools.SetStatic(T("MegaCrit.Sts2.Core.Helpers.NonInteractiveMode"), "AutoSlayerCheck", (Func<bool>)(() => true));
        InstallSeam(); InstallChoiceSelector();
    }

    private void Construct(ResetRequest r)
    {
        Type db = T("MegaCrit.Sts2.Core.Models.ModelDb"), playerType = T("MegaCrit.Sts2.Core.Entities.Players.Player");
        // Cancel and detach every shipped combat continuation before replacing the
        // singleton's state. Without the native reset lifecycle, a completion from
        // the prior reconstructed combat can end the newly installed combat.
        object nativeCombatManager = ReflectionTools.GetStatic(T("MegaCrit.Sts2.Core.Combat.CombatManager"), "Instance")!;
        if (_runServicesInitialized) ReflectionTools.Invoke(nativeCombatManager, "Reset", false);
        object character = Find(ReflectionTools.GetStatic(db, "AllCharacters")!, r.Character);
        object unlock = ReflectionTools.Create(T("MegaCrit.Sts2.Core.Unlocks.UnlockState"), new List<string>(), List(T("MegaCrit.Sts2.Core.Models.ModelId"), []), 0);
        _player = playerType.GetMethods(BindingFlags.Public | BindingFlags.Static).Single(x => x.Name == "CreateForNewRun" && x.GetParameters().Length == 3).Invoke(null, [character, unlock, (ulong)1])!;
        _cardInstanceIds.Clear();
        _choiceOrdinal = 0; _dynamicCardOrdinal = 0; _pendingChoice = null; _continuationTask = null;
        object deck = ReflectionTools.Get(_player, "Deck")!;
        if (!r.UseCharacterStartingLoadout) ReflectionTools.Invoke(deck, "Clear", true);
        Dictionary<string, object> deckVersions = new(StringComparer.Ordinal);
        foreach (CardSpec c in r.UseCharacterStartingLoadout ? [] : r.Deck)
        {
            object nativeCard = Mutable("AllCards", c.ModelId);
            ApplyNativeState(nativeCard, c.NativeState);
            if (c.Enchantment is not null)
            {
                object enchantment = Mutable("DebugEnchantments", c.Enchantment.ModelId);
                ReflectionTools.Invoke(nativeCard, "EnchantInternal", enchantment, Convert.ToDecimal(c.Enchantment.Amount));
                ReflectionTools.Invoke(enchantment, "ModifyCard");
            }
            for (int upgrade = 0; upgrade < c.Upgrades; upgrade++)
            {
                ReflectionTools.Invoke(nativeCard, "UpgradeInternal");
                ReflectionTools.Invoke(nativeCard, "FinalizeUpgradeInternal");
            }
            ReflectionTools.Invoke(deck, "AddInternal", nativeCard, -1, true);
            deckVersions.Add(c.InstanceId, nativeCard);
        }
        object pc = ReflectionTools.Get(_player, "Creature")!;
        if (!r.UseCharacterStartingLoadout)
        {
            ReflectionTools.Set(pc, "CurrentHp", r.CurrentHp); ReflectionTools.Set(pc, "MaxHp", r.MaxHp); ReflectionTools.Set(_player, "Gold", r.Gold);
            foreach (object? relic in ReflectionTools.Enumerate(ReflectionTools.Get(_player, "Relics")).ToArray()) if (relic is not null) ReflectionTools.Invoke(_player, "RemoveRelicInternal", relic, true);
        }
        foreach (RelicSpec relic in r.UseCharacterStartingLoadout ? [] : r.Relics ?? [])
        {
            object nativeRelic = Mutable("AllRelics", relic.ModelId);
            ApplyNativeState(nativeRelic, relic.NativeState);
            if (relic.Counter is int counter) ApplyRelicCounter(nativeRelic, counter);
            ReflectionTools.Invoke(_player, "AddRelicInternal", nativeRelic, -1, true);
        }

        object acts = ReflectionTools.InvokeStatic(T("MegaCrit.Sts2.Core.Models.ActModel"), "GetDefaultList")!;
        object modifiers = List(T("MegaCrit.Sts2.Core.Models.ModifierModel"), []);
        object mode = Enum.GetValues(T("MegaCrit.Sts2.Core.Runs.GameMode")).GetValue(0)!;
        _run = T("MegaCrit.Sts2.Core.Runs.RunState").GetMethods(BindingFlags.Public | BindingFlags.Static).Single(x => x.Name == "CreateForTest")
            .Invoke(null, [List(playerType, [_player]), acts, modifiers, mode, r.Ascension, r.Seed])!;
        object runManager = ReflectionTools.GetStatic(T("MegaCrit.Sts2.Core.Runs.RunManager"), "Instance")!;
        if (_runServicesInitialized)
        {
            object? previousChoiceSynchronizer = ReflectionTools.Get(runManager, "PlayerChoiceSynchronizer");
            if (previousChoiceSynchronizer is not null) ReflectionTools.Invoke(previousChoiceSynchronizer, "Dispose");
            object? previousReplayWriter = ReflectionTools.Get(runManager, "CombatReplayWriter");
            if (previousReplayWriter is not null) ReflectionTools.Invoke(previousReplayWriter, "Dispose");
            ReflectionTools.Set(runManager, "State", null);
        }
        ReflectionTools.Invoke(runManager, "SetUpTest", _run, ReflectionTools.Create(T("MegaCrit.Sts2.Core.Multiplayer.NetSingleplayerGameService")), true, false);
        _runServicesInitialized = true;
        ReflectionTools.Set(ReflectionTools.Get(runManager, "CombatReplayWriter")!, "IsEnabled", false);
        ReflectionTools.SetStatic(T("MegaCrit.Sts2.Core.Context.LocalContext"), "NetId", (ulong?)1);

        // SetUpTest applies shipped Ascension effects. In particular A10 may add
        // ASCENDERS_BANE after the supplied deck was constructed. Assign stable
        // identities only after that native lifecycle has completed so every
        // resulting deck card, including character starters, is represented.
        int loadoutOrdinal = 0;
        foreach (object? nativeCard in ReflectionTools.Enumerate(ReflectionTools.Get(deck, "Cards")))
        {
            if (nativeCard is null || deckVersions.Values.Any(value => ReferenceEquals(value, nativeCard))) continue;
            string prefix = r.UseCharacterStartingLoadout ? "starter" : "native-added";
            deckVersions.Add($"{prefix}-{loadoutOrdinal++}-{Entry(nativeCard)}", nativeCard);
        }

        if (!r.UseCharacterStartingLoadout)
        {
            int maxSlot = (r.Potions ?? []).Count > 0 ? (r.Potions ?? []).Max(p => p.Slot) : -1;
            int currentSlots = ReflectionTools.Enumerate(ReflectionTools.Get(_player, "PotionSlots")).Count;
            if (maxSlot >= currentSlots)
            {
                ReflectionTools.Invoke(_player, "SetMaxPotionCountInternal", maxSlot + 1);
            }
            foreach (object? potion in ReflectionTools.Enumerate(ReflectionTools.Get(_player, "PotionSlots")).ToArray())
                if (potion is not null) ReflectionTools.Invoke(_player, "DiscardPotionInternal", potion, true);
        }
        foreach (PotionSpec potion in r.UseCharacterStartingLoadout ? [] : r.Potions ?? [])
        {
            object result = ReflectionTools.Invoke(_player, "AddPotionInternal", Mutable("AllPotions", potion.ModelId), potion.Slot, true)!;
            if (!(bool)ReflectionTools.Get(result, "success")!) throw new ProtocolException("invalid_reset", $"Could not place potion {potion.ModelId} in slot {potion.Slot}.");
        }

        object encounterModel = r.Encounter.Equals("first", StringComparison.OrdinalIgnoreCase) ? ReflectionTools.Enumerate(ReflectionTools.GetStatic(db, "AllEncounters")).First(x => x is not null)! : Find(ReflectionTools.GetStatic(db, "AllEncounters")!, r.Encounter);
        object encounter = ReflectionTools.Invoke(encounterModel, "ToMutable")!; ReflectionTools.Invoke(encounter, "GenerateMonstersWithSlots", _run);
        _combat = ReflectionTools.Create(T("MegaCrit.Sts2.Core.Combat.CombatState"), encounter, _run, modifiers, List(T("MegaCrit.Sts2.Core.Models.BadgeModel"), []), ReflectionTools.Get(_run, "MultiplayerScalingModel"));
        ReflectionTools.Invoke(_combat, "AddPlayer", _player);
        // CombatManager and RunState normally enter combat together. The isolated constructor
        // supplies CombatState directly, so preserve the other half of that native invariant as
        // well: end-of-combat hooks receive the real CombatRoom wrapping this same state.
        object combatRoom = ReflectionTools.Create(T("MegaCrit.Sts2.Core.Rooms.CombatRoom"), _combat);
        ReflectionTools.Invoke(_run, "PushRoom", combatRoom);
        _manager = nativeCombatManager; ReflectionTools.Set(_manager, "_state", _combat); ReflectionTools.Set(_manager, "IsInProgress", true); InitEvents(_manager);
        ReflectionTools.Invoke(_player, "ResetCombatState"); _pcs = ReflectionTools.Get(_player, "PlayerCombatState")!;
        ReflectionTools.Invoke(_player, "PopulateCombatState", ReflectionTools.Get(ReflectionTools.Get(_run, "Rng")!, "Shuffle"), _combat);
        object drawPile = ReflectionTools.Get(_pcs, "DrawPile")!;
        foreach (object? combatCard in ReflectionTools.Enumerate(ReflectionTools.Get(drawPile, "Cards")))
        {
            if (combatCard is null) continue;
            object? deckVersion = ReflectionTools.Get(combatCard, "DeckVersion");
            string instanceId = deckVersions.Single(pair => ReferenceEquals(pair.Value, deckVersion)).Key;
            _cardInstanceIds[combatCard] = instanceId;
        }
        object handPile = ReflectionTools.Get(_pcs, "Hand")!;
        foreach (string instanceId in r.InitialHand ?? [])
        {
            object combatCard = _cardInstanceIds.Single(pair => pair.Value == instanceId).Key;
            ReflectionTools.Invoke(drawPile, "RemoveInternal", combatCard, true);
            ReflectionTools.Invoke(handPile, "AddInternal", combatCard, -1, true);
        }
        if (r.InitialDrawPile is not null)
        {
            object[] orderedDraw = r.InitialDrawPile.Select(instanceId => _cardInstanceIds.Single(pair => pair.Value == instanceId).Key).ToArray();
            foreach (object combatCard in ReflectionTools.Enumerate(ReflectionTools.Get(drawPile, "Cards")).Where(card => card is not null).Select(card => card!).ToArray())
                ReflectionTools.Invoke(drawPile, "RemoveInternal", combatCard, true);
            foreach (object combatCard in orderedDraw)
                ReflectionTools.Invoke(drawPile, "AddInternal", combatCard, -1, true);
        }
        object enemy = Enum.Parse(T("MegaCrit.Sts2.Core.Combat.CombatSide"), "Enemy", true);
        List<object> constructedEnemies = [];
        int enemyIndex = 0;
        foreach (object? pair in ReflectionTools.Enumerate(ReflectionTools.Get(encounter, "MonstersWithSlots")))
        {
            if (pair is null) continue;
            object monster = r.Enemies is not null && enemyIndex < r.Enemies.Count
                ? Mutable("Monsters", r.Enemies[enemyIndex].ModelId)
                : ReflectionTools.Get(pair, "Item1")!;
            object creature = ReflectionTools.Invoke(_combat, "CreateCreature", monster, enemy, ReflectionTools.Get(pair, "Item2"))!;
            ReflectionTools.Invoke(_combat, "AddCreature", creature); ReflectionTools.Invoke(_manager, "AddCreature", creature);
            if (ReflectionTools.Invoke(_manager, "AfterCreatureAdded", creature) is Task task) task.GetAwaiter().GetResult();
            constructedEnemies.Add(creature);
            enemyIndex++;
        }
        if (r.Enemies is not null)
        {
            if (r.Enemies.Count != constructedEnemies.Count)
                throw new ProtocolException("enemy_composition_mismatch", $"Reset requested {r.Enemies.Count} enemies but encounter constructed {constructedEnemies.Count}.");
            for (int index = 0; index < constructedEnemies.Count; index++)
            {
                EnemySpec expected = r.Enemies[index];
                object creature = constructedEnemies[index];
                if (!StringComparer.Ordinal.Equals(expected.ModelId, Entry(ReflectionTools.Get(creature, "Monster")!)))
                    throw new ProtocolException("enemy_composition_mismatch", $"Enemy {index} requested {expected.ModelId} but encounter constructed {Entry(ReflectionTools.Get(creature, "Monster")!)}.");
                ReflectionTools.Set(creature, "MaxHp", expected.MaxHp);
                ReflectionTools.Set(creature, "CurrentHp", expected.CurrentHp);
                object nativeMonster = ReflectionTools.Get(creature, "Monster")!;
                object machine = ReflectionTools.Get(nativeMonster, "MoveStateMachine")!;
                object states = ReflectionTools.Get(machine, "States")!;
                object State(string id) => ReflectionTools.Enumerate(states)
                    .Where(pair => pair is not null && StringComparer.Ordinal.Equals(Convert.ToString(ReflectionTools.Get(pair!, "Key")), id))
                    .Select(pair => ReflectionTools.Get(pair!, "Value")!)
                    .SingleOrDefault() ?? throw new ProtocolException("unknown_enemy_move", $"Enemy {expected.ModelId} has no native move state '{id}'.");
                if (expected.MoveHistory is not null)
                {
                    IList stateLog = (IList)ReflectionTools.Get(machine, "StateLog")!;
                    stateLog.Clear();
                    foreach (string moveId in expected.MoveHistory) stateLog.Add(State(moveId));
                }
                if (expected.NextMoveId is not null)
                    ReflectionTools.Invoke(nativeMonster, "SetMoveImmediate", State(expected.NextMoveId), true);
            }
        }
        _combatCreaturesById.Clear();
        object pcCreature = ReflectionTools.Get(_player, "Creature")!;
        if (ReflectionTools.Get(pcCreature, "CombatId") is uint pcId) _combatCreaturesById[pcId] = pcCreature;
        foreach (object c in constructedEnemies)
        {
            if (ReflectionTools.Get(c, "CombatId") is uint cId) _combatCreaturesById[cId] = c;
        }
        if (r.InvokeCombatEntryHooks)
        {
            // Reconstruct the assembly-owned room/combat lifecycle rather than synthesizing
            // individual relic, card, character, or pet effects. The exporter opts into this
            // only for traces captured after the field was introduced; legacy traces retain
            // their previous reset contract and remain quarantined if they require the hooks.
            foreach (object? relic in ReflectionTools.Enumerate(ReflectionTools.Get(_player, "Relics")))
            {
                if (relic is null) continue;
                if (ReflectionTools.Invoke(relic, "AfterRoomEntered", combatRoom) is Task roomHook)
                    CompleteResetLifecycle(roomHook, $"relic {Entry(relic)} AfterRoomEntered");
                if (ReflectionTools.Invoke(relic, "BeforeCombatStart") is Task combatHook)
                    CompleteResetLifecycle(combatHook, $"relic {Entry(relic)} BeforeCombatStart");
                if (ReflectionTools.Invoke(relic, "BeforeCombatStartLate") is Task lateCombatHook)
                    CompleteResetLifecycle(lateCombatHook, $"relic {Entry(relic)} BeforeCombatStartLate");
            }
            object currentSide = ReflectionTools.Get(_combat, "CurrentSide")!;
            object participants = ReflectionTools.Get(_combat, "CreaturesOnCurrentSide")!;
            if (ReflectionTools.InvokeStatic(T("MegaCrit.Sts2.Core.Hooks.Hook"), "BeforeSideTurnStart", _combat, currentSide, participants) is Task turnStartHook)
                CompleteResetLifecycle(turnStartHook, "Hook.BeforeSideTurnStart");
        }
        else
        {
            // Backward-compatible narrow lifecycle for already certified Necrobinder traces.
            foreach (object? relic in ReflectionTools.Enumerate(ReflectionTools.Get(_player, "Relics")))
            {
                if (relic is null || ReflectionTools.Get(relic, "SpawnsPets") is not true) continue;
                MethodInfo? beforeCombatStart = relic.GetType().GetMethod("BeforeCombatStart", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                if (beforeCombatStart is null)
                    throw new ProtocolException("unsupported_pet_relic", $"Pet relic {Entry(relic)} has no native BeforeCombatStart lifecycle.");
                if (beforeCombatStart.Invoke(relic, null) is Task petStart) CompleteResetLifecycle(petStart, $"pet relic {Entry(relic)} BeforeCombatStart");
            }
        }
        object cardDb = ReflectionTools.GetStatic(T("MegaCrit.Sts2.Core.GameActions.Multiplayer.NetCombatCardDb"), "Instance")!; ReflectionTools.Invoke(cardDb, "ClearCardsForTesting");
        ReflectionTools.Invoke(cardDb, "StartCombat", List(playerType, [_player]));
        if (r.Enemies is not null)
            for (int index = 0; index < constructedEnemies.Count; index++)
                if (r.Enemies[index].Block is int expectedBlock)
                    ReflectionTools.Set(constructedEnemies[index], "Block", expectedBlock);
        ReflectionTools.Set(_pcs, "Energy", r.Energy ?? Convert.ToInt32(ReflectionTools.Get(_pcs, "MaxEnergy")));
        if (r.Stars is not null) ReflectionTools.Set(_pcs, "Stars", r.Stars.Value);
        ReflectionTools.Set(_pcs, "TurnNumber", r.Turn); ReflectionTools.Set(_pcs, "Phase", Enum.Parse(T("MegaCrit.Sts2.Core.Combat.PlayerTurnPhase"), "Play")); _hash = "";
        ApplyRngCounters(r);
        int observedCards = new[] { "Hand", "DrawPile", "DiscardPile", "ExhaustPile", "PlayPile" }.Sum(name => ReflectionTools.Enumerate(ReflectionTools.Get(ReflectionTools.Get(_pcs, name)!, "Cards")).Count);
        int expectedCards = ReflectionTools.Enumerate(ReflectionTools.Get(deck, "Cards")).Count;
        if (observedCards != expectedCards) throw new ProtocolException("card_conservation", $"Native deck contains {expectedCards} cards but combat constructed {observedCards}.");
    }

    private void CompleteResetLifecycle(Task task, string boundary)
    {
        if (!task.Wait(TimeSpan.FromSeconds(5)))
        {
            string code = _pendingChoice is null ? "reset_lifecycle_timeout" : "unsupported_reset_choice";
            throw new ProtocolException(code, $"Native {boundary} did not complete within the bounded reset window.");
        }
        task.GetAwaiter().GetResult();
    }

    private async Task PlayAsync(uint cardId, uint? targetId)
    {
        object db = ReflectionTools.GetStatic(T("MegaCrit.Sts2.Core.GameActions.Multiplayer.NetCombatCardDb"), "Instance")!;
        object card = ReflectionTools.Enumerate(ReflectionTools.Get(ReflectionTools.Get(_pcs!, "Hand")!, "Cards")).Single(x => x is not null && Convert.ToUInt32(ReflectionTools.Invoke(db, "GetCardId", x)) == cardId)!;
        object? target = targetId is null ? null : ReflectionTools.Enumerate(ReflectionTools.Get(_combat!, "Creatures")).Single(x => x is not null && Convert.ToUInt32(ReflectionTools.Get(x, "CombatId")) == targetId);
        object action = ReflectionTools.Create(T("MegaCrit.Sts2.Core.GameActions.PlayCardAction"), card, target); ReflectionTools.Set(action, "PlayerChoiceContext", ReflectionTools.Create(T("MegaCrit.Sts2.Core.GameActions.Multiplayer.BlockingPlayerChoiceContext")));
        if (ReflectionTools.Invoke(action, "ExecuteAction") is Task task) await task.ConfigureAwait(false);
        // Cards such as Void Form end the turn from inside shipped OnPlay. In the
        // full application the UI coordinator observes the ready flag and drains
        // the turn; headlessly, finish that same shipped combat-manager path here.
        if (PlayerAlive() && Alive("Enemies") && Convert.ToBoolean(ReflectionTools.Invoke(_manager!, "IsPlayerReadyToEndTurn", _player!)))
            await EndTurnAsync().ConfigureAwait(false);
    }
    private async Task EndTurnAsync()
    {
        // CombatManager is a shipped singleton. Some encounter hooks can clear its
        // active flag even though this isolated native CombatState remains live.
        ReflectionTools.Set(_manager!, "_state", _combat);
        ReflectionTools.Set(_manager!, "IsInProgress", true);
        if (ReflectionTools.Invoke(_manager!, "EndPlayerTurnPhaseOneInternal") is Task one) await one.ConfigureAwait(false);
        if (ReflectionTools.Invoke(_manager!, "EndPlayerTurnPhaseTwoInternal") is Task two) await two.ConfigureAwait(false);
        Func<Task> drain = () => Task.CompletedTask; if (ReflectionTools.Invoke(_manager!, "SwitchFromPlayerToEnemySide", drain) is Task sides) await sides.ConfigureAwait(false);
        string phase = ReflectionTools.Get(_pcs!, "Phase")!.ToString()!;
        if (phase != "Play" && PlayerAlive() && Alive("Enemies") && _pendingChoice is null)
        {
            object playerCreature = ReflectionTools.Get(_player!, "Creature")!;
            throw new ProtocolException("incomplete_native_turn", $"Native full-turn coordinator returned in live phase {phase}.", new
            {
                manager_in_progress = ReflectionTools.Get(_manager!, "IsInProgress"), manager_is_ending = ReflectionTools.Get(_manager!, "IsEnding"),
                current_side = ReflectionTools.Get(_combat!, "CurrentSide"), pending_loss = ReflectionTools.Get(_manager!, "_pendingLoss") is not null,
                player_hp = ReflectionTools.Get(playerCreature, "CurrentHp"), player_is_alive = ReflectionTools.Get(playerCreature, "IsAlive"), player_is_dead = ReflectionTools.Get(playerCreature, "IsDead"),
                enemies = ReflectionTools.Enumerate(ReflectionTools.Get(_combat!, "Enemies")).Where(enemy => enemy is not null).Select(enemy => new
                {
                    model_id = Entry(ReflectionTools.Get(enemy!, "Monster")!), hp = ReflectionTools.Get(enemy!, "CurrentHp"), alive = ReflectionTools.Get(enemy!, "IsAlive"),
                    primary = ReflectionTools.Get(enemy!, "IsPrimaryEnemy"), powers = ReflectionTools.Enumerate(ReflectionTools.Get(enemy!, "Powers")).Where(power => power is not null).Select(power => Entry(power!)).ToArray()
                }).ToArray()
            });
        }
    }
    private async Task UsePotionAsync(int slot, uint? targetId)
    {
        object potion = ReflectionTools.Enumerate(ReflectionTools.Get(_player!, "PotionSlots"))[slot]
            ?? throw new ProtocolException("invalid_action", $"Potion slot {slot} is empty.");
        object? target = targetId is null ? null : ReflectionTools.Enumerate(ReflectionTools.Get(_combat!, "Creatures")).Single(x => x is not null && Convert.ToUInt32(ReflectionTools.Get(x, "CombatId")) == targetId);
        object action = ReflectionTools.Create(T("MegaCrit.Sts2.Core.GameActions.UsePotionAction"), potion, target, true);
        if (ReflectionTools.Invoke(action, "ExecuteAction") is Task task) await task.ConfigureAwait(false);
    }
    private async Task DiscardPotionAsync(int slot)
    {
        object action = ReflectionTools.Create(T("MegaCrit.Sts2.Core.GameActions.DiscardPotionGameAction"), _player!, Convert.ToUInt32(slot), true);
        if (ReflectionTools.Invoke(action, "ExecuteAction") is Task task) await task.ConfigureAwait(false);
    }

    private object TransitionKernelSnapshot() => new
    {
        run_mode = _runMode,
        run_stage = _runStage,
        map_mode = _mapMode,
        reward_mode = _rewardMode,
        reward_kind = _rewardKind,
        reward_model_id = _rewardModelId,
        rest_mode = _restMode,
        event_mode = _eventMode,
        event_id = _eventId,
        custom_reward_mode = _customRewardMode,
        custom_rewards_linked = _customRewardsLinked,
        custom_reward_kinds = _customRewardKinds
    };

    private string ComputeStateHash(object observation)
    {
        object hashPayload = new
        {
            hash_schema_version = 3,
            observation,
            kernel = TransitionKernelSnapshot()
        };
        return Convert.ToHexString(SHA256.HashData(JsonSerializer.SerializeToUtf8Bytes(hashPayload)));
    }

    private EnvironmentResult Capture(object? transition)
    {
        if (_runMode && _runStage != "combat" && _runStage != "run_terminal" && !PlayerAlive())
        {
            _runWon = false;
            _runStage = "run_terminal";
        }
        if (_customRewardMode) return CaptureCustomRewards(transition);
        if (_runMode && _runStage == "run_terminal") return CaptureRunTerminal(transition);
        if (_runMode && _runStage == "act_transition") return CaptureActTransition(transition);
        if (_runMode && _runStage == "map") return CaptureMap(transition);
        if (_runMode && _runStage == "rewards") return CaptureRoomRewards(transition);
        if (_runMode && _runStage == "treasure") return CaptureTreasure(transition);
        if (_runMode && _runStage == "shop") return CaptureShop(transition);
        if (_mapMode) return CaptureMap(transition);
        if (_rewardMode) return CaptureReward(transition);
        if (_restMode) return CaptureRest(transition);
        if (_eventMode) return CaptureEvent(transition);
        EnsureReset(); object rng = ReflectionTools.Get(_run!, "Rng")!, serial = ReflectionTools.Invoke(rng, "ToSerializable")!;
        SortedDictionary<string, int> counters = new(StringComparer.Ordinal); foreach (object? p in ReflectionTools.Enumerate(ReflectionTools.Get(serial, "Counters"))) if (p is not null) counters[ReflectionTools.Get(p, "Key")!.ToString()!] = Convert.ToInt32(ReflectionTools.Get(p, "Value"));
        object[] creatures = ReflectionTools.Enumerate(ReflectionTools.Get(_combat!, "Creatures")).Where(x => x is not null).Select(x => Creature(x!)).ToArray();
        object[] piles = new[] { "Hand", "DrawPile", "DiscardPile", "ExhaustPile", "PlayPile" }.Select(Pile).ToArray(); LegalAction[] actions = BuildActions().ToArray();
        bool playerAlive = PlayerAlive(), enemyAlive = Alive("Enemies"), terminal = !playerAlive || (!_runMode && !enemyAlive);
        object? choiceState = _pendingChoice?.Snapshot();
        Dictionary<string, object?> combatObservation = new()
        {
            ["turn"] = ReflectionTools.Get(_pcs!, "TurnNumber"),
            ["phase"] = ReflectionTools.Get(_pcs!, "Phase")!.ToString(),
            ["energy"] = ReflectionTools.Get(_pcs!, "Energy"),
            ["max_energy"] = ReflectionTools.Get(_pcs!, "MaxEnergy"),
            ["stars"] = ReflectionTools.Get(_pcs!, "Stars"),
            ["creatures"] = creatures,
            ["piles"] = piles
        };
        if (_reset!.CaptureOrbs || ReflectionTools.Get(_pcs!, "OrbQueue") is not null)
        {
            if (ReflectionTools.Get(_pcs!, "OrbQueue") is { } queue)
            {
                combatObservation["orbs"] = new
                {
                    capacity = ReflectionTools.Get(queue, "Capacity"),
                    entries = ReflectionTools.Enumerate(ReflectionTools.Get(queue, "Orbs")).Where(orb => orb is not null).Select(orb => new
                    {
                        model_id = Entry(orb!), passive = ReflectionTools.Get(orb!, "PassiveVal"), evoke = ReflectionTools.Get(orb!, "EvokeVal"), native_state = SavedNativeState(orb!)
                    }).ToArray()
                };
            }
        }
        object observation = new
        {
            schema_version = ProtocolConstants.ObservationSchemaVersion, game_build = new { version = _productVersion, assembly_sha256 = _assemblyHash, pck_sha256 = _pckHash },
            run = new { seed = ReflectionTools.Get(rng, "StringSeed"), ascension = _reset!.Ascension, gold = ReflectionTools.Get(_player!, "Gold"), rng_counters = counters },
            combat = combatObservation,
            inventory = new { relics = ReflectionTools.Enumerate(ReflectionTools.Get(_player!, "Relics")).Where(x => x is not null).Select(x => new { model_id = Entry(x!), counter = (bool)ReflectionTools.Get(x!, "ShowCounter")! ? ReflectionTools.Get(x!, "DisplayAmount") : null, native_state = SavedNativeState(x!) }).ToArray(), potions = ReflectionTools.Enumerate(ReflectionTools.Get(_player!, "PotionSlots")).Select((x, i) => x is null ? null : new { slot = i, model_id = Entry(x) }).ToArray() },
            outstanding_choice = choiceState,
            decision = new { kind = terminal ? "terminal" : _pendingChoice is null ? "combat_action" : _pendingChoice.DecisionKind, legal_actions = actions }, terminal, victory = terminal && playerAlive
        };
        _hash = ComputeStateHash(observation); string handle = GetOrAddCurrentBranch();
        return new(observation, _hash, actions, terminal, terminal && playerAlive, handle, transition, ScoringFeatures());
    }

    private IReadOnlyList<LegalAction> BuildActions()
    {
        IReadOnlyList<LegalAction> raw = BuildActionsRaw();
        List<LegalAction> unique = [];
        foreach (IGrouping<string, LegalAction> group in raw.GroupBy(action => action.ActionId, StringComparer.Ordinal))
        {
            LegalAction first = group.First();
            string expected = JsonSerializer.Serialize(new { first.Kind, first.Parameters });
            if (group.Skip(1).Any(action => !StringComparer.Ordinal.Equals(expected, JsonSerializer.Serialize(new { action.Kind, action.Parameters }))))
            {
                LegalAction[] collisions = group.ToArray();
                if (collisions.All(action => action.Kind == "play_card" && action.Parameters.ContainsKey("card_id"))
                    && collisions.Select(action => Convert.ToUInt32(action.Parameters["card_id"])).Distinct().Count() == collisions.Length)
                {
                    // Some shipped generated-card effects place multiple network
                    // registrations of one logical CardModel instance in hand.
                    // NetCombatCardDb's ID is the native execution identity; add
                    // it only when the stable logical ID collides.
                    unique.AddRange(collisions.Select(action => action with
                    {
                        ActionId = $"{action.ActionId}:net:{Convert.ToUInt32(action.Parameters["card_id"])}"
                    }));
                    continue;
                }
                throw new ProtocolException("legal_action_id_collision", $"Native legal action ID '{group.Key}' maps to conflicting parameters.", new { actions = collisions });
            }
            unique.Add(first);
        }
        return unique;
    }

    private IReadOnlyList<LegalAction> BuildActionsRaw()
    {
        if (_pendingRewardsSet is not null) return BuildCustomRewardActions();
        if (_runMode && _runStage == "run_terminal") return [];
        if (_runMode && _runStage == "act_transition") return [new("advance_act", "advance_act", new Dictionary<string, object?>())];
        if (_runMode && _runStage == "map") return BuildMapActions();
        if (_runMode && _runStage == "rewards") return BuildRoomRewardActions();
        if (_runMode && _runStage == "treasure") return BuildTreasureActions();
        if (_runMode && _runStage == "shop") return BuildShopActions();
        if (_runMode && _runStage == "combat" && PlayerAlive() && !Alive("Enemies"))
            return [new("generate_room_rewards", "generate_room_rewards", new Dictionary<string, object?>())];
        if (_mapMode) return BuildMapActions();
        if (_rewardMode) return BuildRewardActions();
        if (_restMode) return BuildRestActions();
        if (_eventMode) return BuildEventActions();
        if (_pendingChoice is not null) return BuildChoiceActions(_pendingChoice);
        if (_pcs is null || ReflectionTools.Get(_pcs, "Phase")!.ToString() != "Play" || !PlayerAlive() || !Alive("Enemies")) return [];
        object db = ReflectionTools.GetStatic(T("MegaCrit.Sts2.Core.GameActions.Multiplayer.NetCombatCardDb"), "Instance")!; List<LegalAction> result = []; HashSet<string> targeted = new(StringComparer.Ordinal) { "AnyEnemy", "AnyAlly" };
        foreach (object? card in ReflectionTools.Enumerate(ReflectionTools.Get(ReflectionTools.Get(_pcs, "Hand")!, "Cards")))
        {
            if (card is null || !(bool)ReflectionTools.Invoke(card, "CanPlay")!) continue; uint cid = Convert.ToUInt32(ReflectionTools.Invoke(db, "GetCardId", card)); string tt = ReflectionTools.Get(card, "TargetType")!.ToString()!;
            string instanceId = GetCardInstanceId(card); string stableId = Uri.EscapeDataString(instanceId);
            if (!targeted.Contains(tt)) result.Add(new($"play:{stableId}:none", "play_card", new Dictionary<string, object?> { ["instance_id"] = instanceId, ["card_id"] = cid, ["target_id"] = null }));
            else foreach (object? target in ReflectionTools.Enumerate(ReflectionTools.Get(_combat!, "Creatures"))) if (target is not null && (bool)ReflectionTools.Invoke(card, "IsValidTarget", target)!) { uint id = Convert.ToUInt32(ReflectionTools.Get(target, "CombatId")); result.Add(new($"play:{stableId}:target:{id}", "play_card", new Dictionary<string, object?> { ["instance_id"] = instanceId, ["card_id"] = cid, ["target_id"] = id })); }
        }
        IReadOnlyList<object?> potionSlots = ReflectionTools.Enumerate(ReflectionTools.Get(_player!, "PotionSlots"));
        for (int slot = 0; slot < potionSlots.Count; slot++)
        {
            object? potion = potionSlots[slot];
            if (potion is null) continue;
            string potionId = Entry(potion);
            foreach (object? target in ReflectionTools.Enumerate(ReflectionTools.Get(_combat!, "Creatures")))
            {
                if (target is null || !(bool)ReflectionTools.Invoke(potion, "IsValidTarget", target)!) continue;
                uint targetId = Convert.ToUInt32(ReflectionTools.Get(target, "CombatId"));
                result.Add(new($"use_potion:{slot}:target:{targetId}", "use_potion", new Dictionary<string, object?> { ["slot"] = slot, ["model_id"] = potionId, ["target_id"] = targetId }));
            }
            if ((bool)ReflectionTools.Invoke(potion, "IsValidTarget", (object?)null)!)
                result.Add(new($"use_potion:{slot}:none", "use_potion", new Dictionary<string, object?> { ["slot"] = slot, ["model_id"] = potionId, ["target_id"] = null }));
            if ((bool)ReflectionTools.Get(_player!, "CanRemovePotions")!)
                result.Add(new($"discard_potion:{slot}", "discard_potion", new Dictionary<string, object?> { ["slot"] = slot, ["model_id"] = potionId }));
        }
        result.Add(new("end_turn", "end_turn", new Dictionary<string, object?>())); return result;
    }

    private void InitializeMap()
    {
        object act = ReflectionTools.Get(_run!, "Act")!;
        object map = ReflectionTools.Invoke(act, "CreateMap", _run!, false)!;
        ReflectionTools.Set(_run!, "Map", map);
    }

    private void InitializeRunMap()
    {
        ReflectionTools.Invoke(_manager!, "Reset", true);
        object runManager = ReflectionTools.GetStatic(T("MegaCrit.Sts2.Core.Runs.RunManager"), "Instance")!;
        ReflectionTools.Invoke(runManager, "GenerateRooms");
        object generated = ReflectionTools.Invoke(runManager, "GenerateMap")!;
        if (generated is not Task task) throw new ProtocolException("invalid_state", "Native map generation did not return a task.");
        task.GetAwaiter().GetResult();
    }

    private IReadOnlyList<LegalAction> BuildMapActions()
    {
        object map = ReflectionTools.Get(_run!, "Map")!;
        object? current = ReflectionTools.Get(_run!, "CurrentMapPoint");
        IEnumerable<object?> candidates = current is null
            ? ReflectionTools.Enumerate(ReflectionTools.Get(map, "startMapPoints"))
            : ReflectionTools.Enumerate(ReflectionTools.Get(current, "Children"));
        return candidates.Where(point => point is not null).Select(point => point!).Select(point =>
        {
            object coord = ReflectionTools.Get(point, "coord")!;
            int col = Convert.ToInt32(ReflectionTools.Get(coord, "col")), row = Convert.ToInt32(ReflectionTools.Get(coord, "row"));
            return new LegalAction($"choose_map:{col}:{row}", "choose_map", new Dictionary<string, object?>
            {
                ["col"] = col, ["row"] = row, ["point_type"] = ReflectionTools.Get(point, "PointType")!.ToString()
            });
        }).OrderBy(action => Convert.ToInt32(action.Parameters["col"])).ThenBy(action => Convert.ToInt32(action.Parameters["row"])).ToArray();
    }

    private void ChooseMap(int col, int row)
    {
        object coord = ReflectionTools.Create(T("MegaCrit.Sts2.Core.Map.MapCoord"), col, row);
        bool added = (bool)ReflectionTools.Invoke(_run!, "AddVisitedMapCoord", coord)!;
        if (!added) throw new ProtocolException("invalid_action", $"Map coordinate {col},{row} was already visited.");
    }

    private async Task EnterRunMapCoordAsync(int col, int row)
    {
        if (_runStage != "map") throw new ProtocolException("invalid_action", $"Cannot enter a map coordinate during run stage '{_runStage}'.");
        object map = ReflectionTools.Get(_run!, "Map")!, coord = ReflectionTools.Create(T("MegaCrit.Sts2.Core.Map.MapCoord"), col, row);
        object point = ReflectionTools.Invoke(map, "GetPoint", coord)!;
        if (!(bool)ReflectionTools.Invoke(_run!, "AddVisitedMapCoord", coord)!)
            throw new ProtocolException("invalid_action", $"Map coordinate {col},{row} was already visited.");
        object pointType = ReflectionTools.Get(point, "PointType")!;
        object runManager = ReflectionTools.GetStatic(T("MegaCrit.Sts2.Core.Runs.RunManager"), "Instance")!;
        if (ReflectionTools.Invoke(runManager, "EnterMapPointInternal", row + 1, pointType, null, false) is Task task) await task.ConfigureAwait(false);
        object room = ReflectionTools.Get(_run!, "CurrentRoom") ?? throw new ProtocolException("invalid_state", "Native map entry produced no current room.");
        string roomType = ReflectionTools.Get(room, "RoomType")!.ToString()!;
        _mapMode = false; _rewardMode = false; _restMode = false; _eventMode = false;
        if (roomType is "Monster" or "Elite" or "Boss")
        {
            if (ReflectionTools.Invoke(_manager!, "StartCombatInternal") is Task startCombat) await startCombat.ConfigureAwait(false);
            _runStage = "combat";
            RebindEnteredCombat(room);
        }
        else if (roomType == "RestSite")
        {
            _runStage = "rest"; _restMode = true;
            _restOptions = ReflectionTools.Enumerate(ReflectionTools.Get(room, "Options")).Where(option => option is not null).Select(option => option!).ToArray();
            _restSelectionStarted = false;
        }
        else if (roomType == "Event")
        {
            _runStage = "event"; _eventMode = true;
            _event = ReflectionTools.Get(room, "LocalMutableEvent")!; _eventId = Entry(_event);
        }
        else if (roomType == "Treasure")
        {
            _runStage = "treasure"; _treasureRoom = room;
            _treasureSynchronizer = ReflectionTools.Get(runManager, "TreasureRoomRelicSynchronizer")!;
            _treasureOpened = false; _treasureResolved = false;
        }
        else if (roomType == "Shop")
        {
            _runStage = "shop"; _merchantRoom = room;
            _merchantInventory = ReflectionTools.Invoke(room, "GetLocalInventory")!;
            foreach (object entry in MerchantEntries()) MerchantEntryIdentity(entry);
        }
        else
            throw new ProtocolException("unsupported_room", $"Native room type '{roomType}' entered successfully but its decision coordinator is not connected yet.");
    }

    private void RebindEnteredCombat(object room)
    {
        Dictionary<object, string> deckIds = new(ReferenceEqualityComparer.Instance);
        foreach ((object card, string id) in _cardInstanceIds)
        {
            object? deckVersion = ReflectionTools.Get(card, "DeckVersion");
            if (deckVersion is not null) deckIds.TryAdd(deckVersion, id);
        }
        _combat = ReflectionTools.Get(room, "CombatState")!;
        _manager = ReflectionTools.GetStatic(T("MegaCrit.Sts2.Core.Combat.CombatManager"), "Instance")!;
        _pcs = ReflectionTools.Get(_player!, "PlayerCombatState")!;
        _cardInstanceIds.Clear(); _dynamicCardOrdinal = 0;
        foreach (string pileName in new[] { "Hand", "DrawPile", "DiscardPile", "ExhaustPile", "PlayPile" })
        {
            object pile = ReflectionTools.Get(_pcs, pileName)!;
            foreach (object? card in ReflectionTools.Enumerate(ReflectionTools.Get(pile, "Cards")))
            {
                if (card is null) continue;
                object? deckVersion = ReflectionTools.Get(card, "DeckVersion");
                if (deckVersion is not null && deckIds.TryGetValue(deckVersion, out string? id)) _cardInstanceIds[card] = id;
                else GetCardInstanceId(card);
            }
        }
    }

    private EnvironmentResult CaptureMap(object? transition)
    {
        EnsureReset(); object map = ReflectionTools.Get(_run!, "Map")!, rng = ReflectionTools.Get(_run!, "Rng")!;
        object Coord(object point)
        {
            object coord = ReflectionTools.Get(point, "coord")!;
            return new { col = ReflectionTools.Get(coord, "col"), row = ReflectionTools.Get(coord, "row") };
        }
        object[] points = ReflectionTools.Enumerate(ReflectionTools.Invoke(map, "GetAllMapPoints"))
            .Where(point => point is not null).Select(point => point!).Select(point => new
            {
                coord = Coord(point), point_type = ReflectionTools.Get(point, "PointType")!.ToString(),
                children = ReflectionTools.Enumerate(ReflectionTools.Get(point, "Children")).Where(child => child is not null).Select(child => Coord(child!)).ToArray()
            }).OrderBy(point => Convert.ToInt32(ReflectionTools.Get(point.coord, "row"))).ThenBy(point => Convert.ToInt32(ReflectionTools.Get(point.coord, "col"))).ToArray();
        object[] visited = ReflectionTools.Enumerate(ReflectionTools.Get(_run!, "_visitedMapCoords")).Where(coord => coord is not null).Select(coord => new { col = ReflectionTools.Get(coord!, "col"), row = ReflectionTools.Get(coord!, "row") }).ToArray();
        LegalAction[] actions = BuildMapActions().ToArray();
        object observation = new
        {
            schema_version = ProtocolConstants.ObservationSchemaVersion,
            game_build = new { version = _productVersion, assembly_sha256 = _assemblyHash, pck_sha256 = _pckHash },
            run = new { seed = ReflectionTools.Get(rng, "StringSeed"), ascension = _reset!.Ascension, act_index = ReflectionTools.Get(_run!, "CurrentActIndex"), act_floor = ReflectionTools.Get(_run!, "ActFloor"), rng_counters = RunRngCounters() },
            map = new { points, visited, current = ReflectionTools.Get(_run!, "CurrentMapPoint") is { } current ? Coord(current) : null },
            decision = new { kind = actions.Length == 0 ? "map_terminal" : "map_choice", legal_actions = actions },
            terminal = actions.Length == 0, victory = false
        };
        _hash = ComputeStateHash(observation); string handle = GetOrAddCurrentBranch();
        return new(observation, _hash, actions, actions.Length == 0, false, handle, transition, ScoringFeatures());
    }

    private EnvironmentResult CaptureActTransition(object? transition)
    {
        EnsureReset();
        LegalAction[] actions = BuildActions().ToArray();
        object deck = ReflectionTools.Get(_player!, "Deck")!;
        object observation = new
        {
            schema_version = ProtocolConstants.ObservationSchemaVersion,
            game_build = new { version = _productVersion, assembly_sha256 = _assemblyHash, pck_sha256 = _pckHash },
            run = RunInventorySnapshot(deck),
            decision = new { kind = "act_transition", legal_actions = actions },
            terminal = false,
            victory = false
        };
        _hash = ComputeStateHash(observation);
        string handle = GetOrAddCurrentBranch();
        return new(observation, _hash, actions, false, false, handle, transition, ScoringFeatures());
    }

    private EnvironmentResult CaptureRunTerminal(object? transition)
    {
        EnsureReset();
        object deck = ReflectionTools.Get(_player!, "Deck")!;
        object observation = new
        {
            schema_version = ProtocolConstants.ObservationSchemaVersion,
            game_build = new { version = _productVersion, assembly_sha256 = _assemblyHash, pck_sha256 = _pckHash },
            run = RunInventorySnapshot(deck),
            decision = new { kind = "run_terminal", legal_actions = Array.Empty<LegalAction>() },
            terminal = true,
            victory = _runWon
        };
        _hash = ComputeStateHash(observation);
        string handle = GetOrAddCurrentBranch();
        return new(observation, _hash, [], true, _runWon, handle, transition, ScoringFeatures());
    }

    private void InitializeReward()
    {
        if (ReflectionTools.Get(_run!, "CurrentMapPointHistoryEntry") is null)
        {
            object pointType = Enum.Parse(T("MegaCrit.Sts2.Core.Map.MapPointType"), "Monster");
            object roomType = Enum.Parse(T("MegaCrit.Sts2.Core.Rooms.RoomType"), "Monster");
            ReflectionTools.Invoke(_run!, "AppendToMapPointHistory", pointType, roomType, null);
        }
        if (_rewardKind == "card")
        {
            object character = ReflectionTools.Get(_player!, "Character")!;
            object cardPool = ReflectionTools.Get(character, "CardPool")!;
            object pools = List(T("MegaCrit.Sts2.Core.Models.CardPoolModel"), [cardPool]);
            object source = Enum.Parse(T("MegaCrit.Sts2.Core.Runs.CardCreationSource"), "Encounter");
            object rarity = Enum.Parse(T("MegaCrit.Sts2.Core.Runs.CardRarityOddsType"), "RegularEncounter");
            Type optionsType = T("MegaCrit.Sts2.Core.Runs.CardCreationOptions");
            object options = optionsType.GetConstructors().Single(ctor => ctor.GetParameters().Length == 4).Invoke([pools, source, rarity, null]);
            Type rewardType = T("MegaCrit.Sts2.Core.Rewards.CardReward");
            _cardReward = rewardType.GetConstructors().Single(ctor => ctor.GetParameters().Length == 4 && ctor.GetParameters()[1].ParameterType == typeof(int)).Invoke([options, 3, _player!, null]);
        }
        else
        {
            bool relic = _rewardKind == "relic";
            Type rewardType = T(relic ? "MegaCrit.Sts2.Core.Rewards.RelicReward" : "MegaCrit.Sts2.Core.Rewards.PotionReward");
            _cardReward = _rewardModelId is null
                ? ReflectionTools.Create(rewardType, _player!)
                : ReflectionTools.Create(rewardType, Mutable(relic ? "AllRelics" : "AllPotions", _rewardModelId), _player!);
        }
        ReflectionTools.Invoke(_cardReward, "Populate");
        _rewardSelectionIndex = null;
        _rewardCompleted = false;
    }

    private IReadOnlyList<LegalAction> BuildRewardActions()
    {
        if (_pendingChoice is not null) return BuildChoiceActions(_pendingChoice);
        if (_cardReward is null || _rewardCompleted) return [];
        if (_rewardKind != "card")
        {
            object model = ReflectionTools.Get(_cardReward, _rewardKind == "relic" ? "Relic" : "Potion")!;
            string modelId = Entry(model);
            return
            [
                new($"choose_reward:0:{modelId}", "choose_reward", new Dictionary<string, object?> { ["option_index"] = 0, ["model_id"] = modelId, ["skip"] = false }),
                new("choose_reward:skip", "choose_reward", new Dictionary<string, object?> { ["option_index"] = -1, ["model_id"] = null, ["skip"] = true })
            ];
        }
        object[] cards = ReflectionTools.Enumerate(ReflectionTools.Get(_cardReward, "Cards")).Where(card => card is not null).Select(card => card!).ToArray();
        List<LegalAction> result = cards.Select((card, index) => new LegalAction($"choose_reward:{index}:{Entry(card)}", "choose_reward", new Dictionary<string, object?>
        {
            ["option_index"] = index, ["model_id"] = Entry(card), ["skip"] = false
        })).ToList();
        if ((bool)ReflectionTools.Get(_cardReward, "CanSkip")!)
            result.Add(new("choose_reward:skip", "choose_reward", new Dictionary<string, object?> { ["option_index"] = -1, ["model_id"] = null, ["skip"] = true }));
        return result;
    }

    private async Task ChooseRewardAsync(int optionIndex)
    {
        if (_cardReward is null) throw new ProtocolException("invalid_action", "No native card reward is active.");
        _rewardSelectionIndex = optionIndex;
        try
        {
            if (optionIndex < 0)
            {
                ReflectionTools.Invoke(_cardReward, "OnSkipped");
                _rewardCompleted = true;
            }
            else
            {
                await StartTransitionAsync(() => (Task)ReflectionTools.Invoke(_cardReward, "SelectUnsynchronized")!);
                if (_pendingChoice is null)
                    _rewardCompleted = (bool)ReflectionTools.Get(_cardReward, "SuccessfullySelected")!;
            }
        }
        finally { if (_pendingChoice is null) _rewardSelectionIndex = null; }
    }

    private EnvironmentResult CaptureReward(object? transition)
    {
        EnsureReset(); LegalAction[] actions = BuildRewardActions().ToArray(); object deck = ReflectionTools.Get(_player!, "Deck")!;
        object[] options = _cardReward is null ? [] : _rewardKind == "card"
            ? ReflectionTools.Enumerate(ReflectionTools.Get(_cardReward, "Cards")).Where(card => card is not null).Select((card, index) => new { option_id = $"reward-{index}-{Entry(card!)}", model_id = Entry(card!) }).Cast<object>().ToArray()
            : [new { option_id = $"reward-0-{Entry(ReflectionTools.Get(_cardReward, _rewardKind == "relic" ? "Relic" : "Potion")!)}", model_id = Entry(ReflectionTools.Get(_cardReward, _rewardKind == "relic" ? "Relic" : "Potion")!) }];
        object observation = new
        {
            schema_version = ProtocolConstants.ObservationSchemaVersion,
            game_build = new { version = _productVersion, assembly_sha256 = _assemblyHash, pck_sha256 = _pckHash },
            run = new { seed = _reset!.Seed, ascension = _reset.Ascension, gold = ReflectionTools.Get(_player!, "Gold"), rng_counters = RunRngCounters(), deck = ReflectionTools.Enumerate(ReflectionTools.Get(deck, "Cards")).Where(card => card is not null).Select(card => Entry(card!)).ToArray(), relics = ReflectionTools.Enumerate(ReflectionTools.Get(_player!, "Relics")).Where(relic => relic is not null).Select(relic => Entry(relic!)).ToArray(), potions = ReflectionTools.Enumerate(ReflectionTools.Get(_player!, "PotionSlots")).Select(potion => potion is null ? null : Entry(potion)).ToArray() },
            reward = new { kind = _rewardKind, options, can_skip = true, selected = _rewardCompleted },
            outstanding_choice = _pendingChoice?.Snapshot(),
            decision = new { kind = _pendingChoice is not null ? _pendingChoice.DecisionKind : actions.Length == 0 ? "reward_complete" : "reward_choice", legal_actions = actions },
            terminal = false, victory = false
        };
        _hash = ComputeStateHash(observation); string handle = GetOrAddCurrentBranch();
        return new(observation, _hash, actions, false, false, handle, transition, ScoringFeatures());
    }

    private void InitializeRestSite()
    {
        if (ReflectionTools.Get(_run!, "CurrentMapPointHistoryEntry") is null)
        {
            object pointType = Enum.Parse(T("MegaCrit.Sts2.Core.Map.MapPointType"), "RestSite");
            object roomType = Enum.Parse(T("MegaCrit.Sts2.Core.Rooms.RoomType"), "RestSite");
            ReflectionTools.Invoke(_run!, "AppendToMapPointHistory", pointType, roomType, null);
        }
        _restOptions = ReflectionTools.Enumerate(ReflectionTools.InvokeStatic(T("MegaCrit.Sts2.Core.Entities.RestSite.RestSiteOption"), "Generate", _player!))
            .Where(option => option is not null).Select(option => option!).ToArray();
        _restSelectionStarted = false;
    }

    private IReadOnlyList<LegalAction> BuildRestActions()
    {
        if (_pendingRewardsSet is not null) return BuildCustomRewardActions();
        if (_pendingChoice is not null) return BuildChoiceActions(_pendingChoice);
        if (_restSelectionStarted) return _runMode ? [new("leave_rest", "leave_rest", new Dictionary<string, object?>())] : [];
        return _restOptions.Where(option => (bool)ReflectionTools.Get(option, "IsEnabled")!).Select(option =>
        {
            string id = (string)ReflectionTools.Get(option, "OptionId")!;
            return new LegalAction($"choose_rest:{id}", "choose_rest", new Dictionary<string, object?> { ["option_id"] = id });
        }).OrderBy(action => action.ActionId, StringComparer.Ordinal).ToArray();
    }

    private async Task ChooseRestAsync(string optionId)
    {
        object option = _restOptions.Single(option => StringComparer.Ordinal.Equals(ReflectionTools.Get(option, "OptionId"), optionId));
        _restSelectionStarted = true;
        await StartTransitionAsync(() => (Task)ReflectionTools.Invoke(option, "OnSelect")!);
    }

    private EnvironmentResult CaptureRest(object? transition)
    {
        EnsureReset(); LegalAction[] actions = BuildRestActions().ToArray(); object deck = ReflectionTools.Get(_player!, "Deck")!;
        object[] options = _restOptions.Select(option => new
        {
            option_id = ReflectionTools.Get(option, "OptionId"), enabled = ReflectionTools.Get(option, "IsEnabled"), implementation = option.GetType().Name
        }).ToArray();
        object? choice = _pendingChoice?.Snapshot();
        object observation = new
        {
            schema_version = ProtocolConstants.ObservationSchemaVersion,
            game_build = new { version = _productVersion, assembly_sha256 = _assemblyHash, pck_sha256 = _pckHash },
            run = new
            {
                seed = _reset!.Seed, ascension = _reset.Ascension, rng_counters = RunRngCounters(),
                current_hp = ReflectionTools.Get(ReflectionTools.Get(_player!, "Creature")!, "CurrentHp"),
                max_hp = ReflectionTools.Get(ReflectionTools.Get(_player!, "Creature")!, "MaxHp"),
                deck = ReflectionTools.Enumerate(ReflectionTools.Get(deck, "Cards")).Where(card => card is not null).Select(card => new { model_id = Entry(card!), upgrades = ReflectionTools.Get(card!, "CurrentUpgradeLevel") }).ToArray()
            },
            rest_site = new { options, selected = _restSelectionStarted && _pendingChoice is null },
            outstanding_choice = choice, outstanding_rewards = CustomRewardsSnapshot(),
            decision = new { kind = _pendingRewardsSet is not null ? "custom_reward_choice" : _pendingChoice is not null ? _pendingChoice.DecisionKind : _restSelectionStarted ? "rest_complete" : "rest_choice", legal_actions = actions },
            terminal = false, victory = false
        };
        _hash = ComputeStateHash(observation); string handle = GetOrAddCurrentBranch();
        return new(observation, _hash, actions, false, false, handle, transition, ScoringFeatures());
    }

    private async Task InitializeEventAsync()
    {
        if (string.IsNullOrWhiteSpace(_eventId)) throw new ProtocolException("invalid_event", "An event_id is required.");
        object canonical = Find(ReflectionTools.GetStatic(T("MegaCrit.Sts2.Core.Models.ModelDb"), "AllEvents")!, _eventId);
        if (!(bool)ReflectionTools.Invoke(canonical, "IsAllowed", _run!)!)
            throw new ProtocolException("event_not_allowed", $"Native event '{_eventId}' is not allowed in this run state.");
        _event = ReflectionTools.Invoke(canonical, "ToMutable")!;
        if (ReflectionTools.Get(_run!, "CurrentMapPointHistoryEntry") is null)
        {
            object pointType = Enum.Parse(T("MegaCrit.Sts2.Core.Map.MapPointType"), "Unknown");
            object roomType = Enum.Parse(T("MegaCrit.Sts2.Core.Rooms.RoomType"), "Event");
            ReflectionTools.Invoke(_run!, "AppendToMapPointHistory", pointType, roomType, ReflectionTools.Get(_event, "Id"));
        }
        if (ReflectionTools.Invoke(_event, "BeginEvent", _player!, false) is Task task) await task.ConfigureAwait(false);
    }

    private IReadOnlyList<LegalAction> BuildEventActions()
    {
        if (_pendingRewardsSet is not null) return BuildCustomRewardActions();
        if (_pendingChoice is not null) return BuildChoiceActions(_pendingChoice);
        if (_event is null) return [];
        EnsureHeadlessArchitectOption();
        if ((bool)ReflectionTools.Get(_event, "IsFinished")!) return _runMode ? [new("leave_event", "leave_event", new Dictionary<string, object?>())] : [];
        return ReflectionTools.Enumerate(ReflectionTools.Get(_event, "CurrentOptions"))
            .Select((option, index) => (option, index))
            .Where(pair => pair.option is not null && !(bool)ReflectionTools.Get(pair.option, "IsLocked")! && !(bool)ReflectionTools.Get(pair.option, "WasChosen")!)
            .Select(pair =>
            {
                string textKey = (string)ReflectionTools.Get(pair.option!, "TextKey")!;
                return new LegalAction($"choose_event:{pair.index}:{Uri.EscapeDataString(textKey)}", "choose_event", new Dictionary<string, object?>
                {
                    ["option_index"] = pair.index, ["text_key"] = textKey, ["is_proceed"] = ReflectionTools.Get(pair.option!, "IsProceed")
                });
            }).ToArray();
    }

    private void EnsureHeadlessArchitectOption()
    {
        if (_event?.GetType().Name != "TheArchitect"
            || ReflectionTools.Enumerate(ReflectionTools.Get(_event, "CurrentOptions")).Any()) return;
        object option = ReflectionTools.Invoke(_event, "CreateOptionForCurrentLine")!;
        object description = ReflectionTools.GetStatic(_event.GetType(), "_emptyLocString")!;
        object options = List(T("MegaCrit.Sts2.Core.Events.EventOption"), [option]);
        ReflectionTools.Invoke(_event, "SetEventState", description, options);
    }

    private async Task ChooseEventAsync(int optionIndex)
    {
        if (_event is null) throw new ProtocolException("invalid_action", "No native event is active.");
        object? option = ReflectionTools.Enumerate(ReflectionTools.Get(_event, "CurrentOptions")).ElementAtOrDefault(optionIndex);
        if (option is null || (bool)ReflectionTools.Get(option, "IsLocked")! || (bool)ReflectionTools.Get(option, "WasChosen")!)
            throw new ProtocolException("invalid_action", $"Event option {optionIndex} is not selectable.");
        object? roomBefore = _runMode ? ReflectionTools.Get(_run!, "CurrentRoom") : null;
        bool completesVictoryRoom = roomBefore is not null
            && ReflectionTools.Get(roomBefore, "IsVictoryRoom") is true
            && (StringComparer.Ordinal.Equals(Convert.ToString(ReflectionTools.Get(option, "TextKey")), "PROCEED")
                || ReflectionTools.Get(_event, "IsOnLastLine") is true);
        _eventPresentationScope = true; _scopedScheduledTask = null;
        try
        {
            await StartTransitionAsync(() => (Task)ReflectionTools.Invoke(option, "Chosen")!);
            if (_pendingChoice is null && _scopedScheduledTask is { } scheduled) await scheduled.ConfigureAwait(false);
        }
        finally { _eventPresentationScope = false; _scopedScheduledTask = null; }
        if (completesVictoryRoom)
        {
            object runManager = ReflectionTools.GetStatic(T("MegaCrit.Sts2.Core.Runs.RunManager"), "Instance")!;
            if (ReflectionTools.Get(_run!, "IsGameOver") is not true
                && ReflectionTools.Invoke(runManager, "EnterNextAct") is Task win) await win.ConfigureAwait(false);

            int currentAct = Convert.ToInt32(ReflectionTools.Get(_run!, "CurrentActIndex"));
            int actCount = ReflectionTools.Enumerate(ReflectionTools.Get(_run!, "Acts")).Count;
            if (currentAct < actCount && ReflectionTools.Get(_run!, "IsGameOver") is not true)
            {
                _eventMode = false; _event = null; _eventId = null;
                _runStage = "map";
                return;
            }
            _runWon = true; _eventMode = false; _event = null; _eventId = null; _runStage = "run_terminal";
            return;
        }
        if (_pendingChoice is null) await BindEventCombatIfEnteredAsync();
    }

    private async Task BindEventCombatIfEnteredAsync()
    {
        if (!_runMode || _runStage != "event") return;
        object? room = ReflectionTools.Get(_run!, "CurrentRoom");
        bool hasEventDecision = _event is not null && ((bool)ReflectionTools.Get(_event, "IsFinished")! || ReflectionTools.Enumerate(ReflectionTools.Get(_event, "CurrentOptions")).Any(option => option is not null && !(bool)ReflectionTools.Get(option, "IsLocked")! && !(bool)ReflectionTools.Get(option, "WasChosen")!));
        for (int attempt = 0; attempt < 1024 && !hasEventDecision && room?.GetType().Name == "EventRoom"; attempt++)
        {
            await Task.Yield();
            room = ReflectionTools.Get(_run!, "CurrentRoom");
        }
        if (room is null || ReflectionTools.Get(room, "RoomType")?.ToString() is not ("Monster" or "Elite" or "Boss")) return;
        if (ReflectionTools.Invoke(_manager!, "StartCombatInternal") is Task startCombat) await startCombat.ConfigureAwait(false);
        _eventMode = false; _runStage = "combat";
        RebindEnteredCombat(room);
    }

    private EnvironmentResult CaptureEvent(object? transition)
    {
        EnsureReset();
        if (_event is null) throw new ProtocolException("invalid_state", "Event mode has no native event instance.");
        LegalAction[] actions = BuildEventActions().ToArray();
        object creature = ReflectionTools.Get(_player!, "Creature")!, deck = ReflectionTools.Get(_player!, "Deck")!;
        object[] options = ReflectionTools.Enumerate(ReflectionTools.Get(_event, "CurrentOptions")).Select((option, index) => option is null ? null : new
        {
            option_index = index, text_key = ReflectionTools.Get(option, "TextKey"), locked = ReflectionTools.Get(option, "IsLocked"),
            chosen = ReflectionTools.Get(option, "WasChosen"), is_proceed = ReflectionTools.Get(option, "IsProceed")
        }).Where(option => option is not null).ToArray()!;
        bool finished = (bool)ReflectionTools.Get(_event, "IsFinished")!;
        object observation = new
        {
            schema_version = ProtocolConstants.ObservationSchemaVersion,
            game_build = new { version = _productVersion, assembly_sha256 = _assemblyHash, pck_sha256 = _pckHash },
            run = new
            {
                seed = _reset!.Seed, ascension = _reset.Ascension, gold = ReflectionTools.Get(_player!, "Gold"), rng_counters = RunRngCounters(),
                current_hp = ReflectionTools.Get(creature, "CurrentHp"), max_hp = ReflectionTools.Get(creature, "MaxHp"),
                deck = ReflectionTools.Enumerate(ReflectionTools.Get(deck, "Cards")).Where(card => card is not null).Select(card => new { model_id = Entry(card!), upgrades = ReflectionTools.Get(card!, "CurrentUpgradeLevel") }).ToArray(),
                relics = ReflectionTools.Enumerate(ReflectionTools.Get(_player!, "Relics")).Where(relic => relic is not null).Select(relic => Entry(relic!)).ToArray(),
                potions = ReflectionTools.Enumerate(ReflectionTools.Get(_player!, "PotionSlots")).Select(potion => potion is null ? null : Entry(potion)).ToArray()
            },
            @event = new { model_id = _eventId, options, finished },
            outstanding_choice = _pendingChoice?.Snapshot(), outstanding_rewards = CustomRewardsSnapshot(),
            decision = new { kind = _pendingRewardsSet is not null ? "custom_reward_choice" : _pendingChoice is not null ? _pendingChoice.DecisionKind : finished ? "event_complete" : "event_choice", legal_actions = actions },
            terminal = false, victory = false
        };
        _hash = ComputeStateHash(observation); string handle = GetOrAddCurrentBranch();
        return new(observation, _hash, actions, false, false, handle, transition, ScoringFeatures());
    }

    private void EnsureRewardsSetViewing(object synchronizer, object rewardsSet)
    {
        try
        {
            int setId = Convert.ToInt32(ReflectionTools.Get(rewardsSet, "Id") ?? -1);
            if (setId < 0)
            {
                ReflectionTools.Invoke(synchronizer, "BeginRewardsSet", rewardsSet);
                return;
            }
            object? rewardStates = ReflectionTools.Get(synchronizer, "_rewardStates");
            if (rewardStates is IEnumerable enumerable)
            {
                foreach (object? state in enumerable)
                {
                    if (state is null) continue;
                    object? stack = ReflectionTools.Get(state, "rewardsStack");
                    if (stack is IEnumerable stackEnum)
                    {
                        foreach (object? entry in stackEnum)
                        {
                            if (entry is not null && ReferenceEquals(ReflectionTools.Get(entry, "set"), rewardsSet))
                                return;
                        }
                    }
                }
                ReflectionTools.Invoke(synchronizer, "BeginRewardsSet", rewardsSet);
            }
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[WARN] EnsureRewardsSetViewing: {ex.Message}");
            try
            {
                if (Convert.ToInt32(ReflectionTools.Get(rewardsSet, "Id") ?? -1) < 0)
                    ReflectionTools.Invoke(synchronizer, "BeginRewardsSet", rewardsSet);
            }
            catch { }
        }
    }

    private async Task GenerateRoomRewardsAsync()
    {
        object room = ReflectionTools.Get(_run!, "CurrentRoom") ?? throw new ProtocolException("invalid_state", "No current room can generate rewards.");
        object generated = ReflectionTools.InvokeStatic(T("MegaCrit.Sts2.Core.Commands.RewardsCmd"), "GenerateForRoomEnd", _player!, room)!;
        if (generated is not Task task) throw new ProtocolException("invalid_state", "Native reward generation did not return a task.");
        await task.ConfigureAwait(false);
        _roomRewardsSet = ReflectionTools.Get(task, "Result")!;
        _resolvedRoomRewards.Clear();
        object synchronizer = ReflectionTools.Get(ReflectionTools.GetStatic(T("MegaCrit.Sts2.Core.Runs.RunManager"), "Instance")!, "RewardsSetSynchronizer")!;
        EnsureRewardsSetViewing(synchronizer, _roomRewardsSet);
        _runStage = "rewards";
    }

    private IReadOnlyList<LegalAction> BuildRoomRewardActions()
    {
        if (_roomRewardsSet is null) return [];
        List<LegalAction> actions = [];
        object[] rewards = ReflectionTools.Enumerate(ReflectionTools.Get(_roomRewardsSet, "Rewards")).Where(reward => reward is not null).Select(reward => reward!).ToArray();
        for (int rewardIndex = 0; rewardIndex < rewards.Length; rewardIndex++)
        {
            object reward = rewards[rewardIndex];
            if (_resolvedRoomRewards.Contains(rewardIndex) || (bool)ReflectionTools.Get(reward, "SuccessfullySelected")!) continue;
            if (reward.GetType().Name == "CardReward")
            {
                object[] cards = ReflectionTools.Enumerate(ReflectionTools.Get(reward, "Cards")).Where(card => card is not null).Select(card => card!).ToArray();
                for (int optionIndex = 0; optionIndex < cards.Length; optionIndex++)
                {
                    string modelId = Entry(cards[optionIndex]);
                    actions.Add(new($"choose_room_reward:{rewardIndex}:card:{optionIndex}:{modelId}", "choose_room_reward", new Dictionary<string, object?> { ["reward_index"] = rewardIndex, ["option_index"] = optionIndex, ["reward_kind"] = "card", ["model_id"] = modelId }));
                }
            }
            else
            {
                object? model = ReflectionTools.Get(reward, "Relic") ?? ReflectionTools.Get(reward, "Potion");
                string kind = reward.GetType().Name.Replace("Reward", "", StringComparison.Ordinal).ToLowerInvariant();
                actions.Add(new($"choose_room_reward:{rewardIndex}:take", "choose_room_reward", new Dictionary<string, object?> { ["reward_index"] = rewardIndex, ["option_index"] = 0, ["reward_kind"] = kind, ["model_id"] = model is null ? null : Entry(model) }));
            }
            actions.Add(new($"choose_room_reward:{rewardIndex}:skip", "choose_room_reward", new Dictionary<string, object?> { ["reward_index"] = rewardIndex, ["option_index"] = -1, ["reward_kind"] = reward.GetType().Name, ["model_id"] = null }));
        }
        actions.Add(new("leave_room_rewards", "leave_room_rewards", new Dictionary<string, object?>()));
        return actions;
    }

    private async Task ChooseRoomRewardAsync(int rewardIndex, int optionIndex)
    {
        if (_roomRewardsSet is null) throw new ProtocolException("invalid_action", "No native room rewards are active.");
        object[] rewards = ReflectionTools.Enumerate(ReflectionTools.Get(_roomRewardsSet, "Rewards")).Where(reward => reward is not null).Select(reward => reward!).ToArray();
        if (rewardIndex < 0 || rewardIndex >= rewards.Length || _resolvedRoomRewards.Contains(rewardIndex)) throw new ProtocolException("invalid_action", $"Room reward {rewardIndex} is not selectable.");
        object reward = rewards[rewardIndex];
        if (optionIndex < 0)
        {
            _resolvedRoomRewards.Add(rewardIndex);
            ReflectionTools.Invoke(reward, "OnSkipped");
            return;
        }
        object synchronizer = ReflectionTools.Get(ReflectionTools.GetStatic(T("MegaCrit.Sts2.Core.Runs.RunManager"), "Instance")!, "RewardsSetSynchronizer")!;
        EnsureRewardsSetViewing(synchronizer, _roomRewardsSet);
        _pendingRoomRewardIndex = rewardIndex;
        try
        {
            if (reward.GetType().Name == "CardReward")
            {
                _rewardSelectionIndex = optionIndex;
                MethodInfo? selectOption = reward.GetType().GetMethod("SelectLocalOption", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                if (selectOption is not null)
                {
                    await StartTransitionAsync(() => (Task)selectOption.Invoke(reward, [optionIndex, _player!])!);
                }
                else
                {
                    await StartTransitionAsync(async () =>
                    {
                        try
                        {
                            await ((Task)ReflectionTools.Invoke(synchronizer, "SelectLocalReward", reward)!).ConfigureAwait(false);
                        }
                        catch (Exception ex)
                        {
                            Console.Error.WriteLine($"[WARN] SelectLocalReward failed for CardReward ({ex.Message}), falling back to SelectUnsynchronized");
                            await ((Task)ReflectionTools.Invoke(reward, "SelectUnsynchronized")!).ConfigureAwait(false);
                        }
                    });
                }
            }
            else
            {
                await StartTransitionAsync(async () =>
                {
                    try
                    {
                        await ((Task)ReflectionTools.Invoke(synchronizer, "SelectLocalReward", reward)!).ConfigureAwait(false);
                    }
                    catch (Exception ex)
                    {
                        Console.Error.WriteLine($"[WARN] SelectLocalReward failed for {reward.GetType().Name} ({ex.Message}), falling back to SelectUnsynchronized");
                        await ((Task)ReflectionTools.Invoke(reward, "SelectUnsynchronized")!).ConfigureAwait(false);
                    }
                });
            }
        }
        finally
        {
            if (_pendingChoice is null)
            {
                if (ReflectionTools.Get(reward, "SuccessfullySelected") is true)
                    _resolvedRoomRewards.Add(rewardIndex);
                _rewardSelectionIndex = null;
                _pendingRoomRewardIndex = null;
            }
        }
    }

    private async Task LeaveRoomRewardsAsync()
    {
        if (_roomRewardsSet is null) throw new ProtocolException("invalid_action", "No native room rewards are active.");
        object runManager = ReflectionTools.GetStatic(T("MegaCrit.Sts2.Core.Runs.RunManager"), "Instance")!;
        try
        {
            object synchronizer = ReflectionTools.Get(runManager, "RewardsSetSynchronizer")!;
            ReflectionTools.Invoke(synchronizer, "BeforeLeavingRoom");
        }
        catch { }
        bool resumesEvent = ReflectionTools.Enumerate(ReflectionTools.Get(_run!, "Rooms")).Count > 1;
        int roomCount = ReflectionTools.Enumerate(ReflectionTools.Get(_run!, "Rooms")).Count;
        object? currentRoom = ReflectionTools.Get(_run!, "CurrentRoom");
        if (resumesEvent)
        {
            if (ReflectionTools.Invoke(runManager, "ProceedFromTerminalRewardsScreen") is Task proceed) await proceed.ConfigureAwait(false);
            object parent = ReflectionTools.Get(_run!, "CurrentRoom") ?? throw new ProtocolException("invalid_state", "Native event combat did not resume a parent room.");
            if (ReflectionTools.Get(parent, "RoomType")?.ToString() != "Event") throw new ProtocolException("unsupported_room", $"Native nested combat resumed unsupported room '{parent.GetType().Name}'.");
            _event = ReflectionTools.Get(parent, "LocalMutableEvent")!; _eventId = Entry(_event); _eventMode = true; _runStage = "event";
        }
        else if (currentRoom is not null && IsActEndingBoss(currentRoom))
        {
            _runStage = "act_transition";
        }
        else
        {
            if (roomCount == 1 && ReflectionTools.Invoke(runManager, "ExitCurrentRoom") is Task exit) await exit.ConfigureAwait(false);
            _runStage = "map";
        }
        _roomRewardsSet = null; _resolvedRoomRewards.Clear();
    }

    private bool IsActEndingBoss(object room)
    {
        if (ReflectionTools.Get(room, "RoomType")?.ToString() != "Boss") return false;
        object map = ReflectionTools.Get(_run!, "Map")!;
        object? secondBoss = ReflectionTools.Get(map, "SecondBossMapPoint");
        if (secondBoss is null) return true;
        object? currentCoord = ReflectionTools.Get(_run!, "CurrentMapCoord");
        object? firstBoss = ReflectionTools.Get(map, "BossMapPoint");
        object? firstBossCoord = firstBoss is null ? null : ReflectionTools.Get(firstBoss, "coord");
        return currentCoord is null || firstBossCoord is null || !currentCoord.Equals(firstBossCoord);
    }

    private async Task AdvanceActAsync()
    {
        if (!_runMode || _runStage != "act_transition")
            throw new ProtocolException("invalid_action", $"Cannot advance an act during run stage '{_runStage}'.");
        object runManager = ReflectionTools.GetStatic(T("MegaCrit.Sts2.Core.Runs.RunManager"), "Instance")!;
        int priorAct = Convert.ToInt32(ReflectionTools.Get(_run!, "CurrentActIndex"));
        int actCount = ReflectionTools.Enumerate(ReflectionTools.Get(_run!, "Acts")).Count;
        ReflectionTools.Set(_run!, "ActFloor", Convert.ToInt32(ReflectionTools.Get(_run!, "ActFloor")) + 1);
        _restMode = false; _eventMode = false; _event = null; _eventId = null;
        if (priorAct < actCount - 1)
        {
            if (ReflectionTools.Invoke(runManager, "ExitCurrentRooms") is Task exit) await exit.ConfigureAwait(false);
            if (ReflectionTools.Invoke(runManager, "SetActInternal", priorAct + 1) is Task setAct) await setAct.ConfigureAwait(false);
            if (ReflectionTools.InvokeStatic(T("MegaCrit.Sts2.Core.Hooks.Hook"), "AfterActEntered", _run!) is Task hook) await hook.ConfigureAwait(false);
            _runStage = "map";
            return;
        }

        Type db = T("MegaCrit.Sts2.Core.Models.ModelDb");
        MethodInfo eventLookup = db.GetMethods(BindingFlags.Public | BindingFlags.Static)
            .Single(method => method.Name == "Event" && method.IsGenericMethodDefinition);
        object architect = eventLookup.MakeGenericMethod(T("MegaCrit.Sts2.Core.Models.Events.TheArchitect")).Invoke(null, null)!;
        object victoryRoom = ReflectionTools.Create(T("MegaCrit.Sts2.Core.Rooms.EventRoom"), architect);
        _eventPresentationScope = true; _scopedScheduledTask = null;
        try
        {
            if (ReflectionTools.Invoke(runManager, "EnterRoom", victoryRoom) is Task transition) await transition.ConfigureAwait(false);
            if (_scopedScheduledTask is { } scheduled) await scheduled.ConfigureAwait(false);
            object enteredEvent = ReflectionTools.Get(victoryRoom, "LocalMutableEvent")!;
            if (!ReflectionTools.Enumerate(ReflectionTools.Get(enteredEvent, "CurrentOptions")).Any())
            {
                object option = ReflectionTools.Invoke(enteredEvent, "CreateOptionForCurrentLine")!;
                object description = ReflectionTools.GetStatic(enteredEvent.GetType(), "_emptyLocString")!;
                object options = List(T("MegaCrit.Sts2.Core.Events.EventOption"), [option]);
                ReflectionTools.Invoke(enteredEvent, "SetEventState", description, options);
            }
        }
        finally { _eventPresentationScope = false; _scopedScheduledTask = null; }

        object? room = ReflectionTools.Get(_run!, "CurrentRoom");
        if (room is not null && ReflectionTools.Get(room, "RoomType")?.ToString() == "Event")
        {
            _event = ReflectionTools.Get(room, "LocalMutableEvent")!;
            _eventId = Entry(_event);
            _eventMode = true;
            _runStage = "event";
            return;
        }
        throw new ProtocolException("invalid_state", "Native final-act transition produced neither the victory event nor a terminal run.");
    }

    private async Task LeaveCurrentRunRoomAsync(string expectedStage)
    {
        if (!_runMode || _runStage != expectedStage) throw new ProtocolException("invalid_action", $"Cannot leave '{expectedStage}' during run stage '{_runStage}'.");
        object runManager = ReflectionTools.GetStatic(T("MegaCrit.Sts2.Core.Runs.RunManager"), "Instance")!;
        if (ReflectionTools.Invoke(runManager, "ExitCurrentRoom") is Task exit) await exit.ConfigureAwait(false);
        _restMode = false; _eventMode = false; _event = null; _eventId = null;
        _treasureRoom = null; _treasureSynchronizer = null; _treasureOpened = false; _treasureResolved = false;
        _merchantRoom = null; _merchantInventory = null; _merchantEntryIdentities.Clear(); _runStage = "map";
    }

    private IReadOnlyList<LegalAction> BuildTreasureActions()
    {
        if (!_treasureOpened) return [new("open_treasure", "open_treasure", new Dictionary<string, object?>())];
        if (_treasureResolved) return [new("leave_treasure", "leave_treasure", new Dictionary<string, object?>())];
        object[] relics = ReflectionTools.Enumerate(ReflectionTools.Get(_treasureSynchronizer!, "CurrentRelics")).Where(relic => relic is not null).Select(relic => relic!).ToArray();
        List<LegalAction> actions = relics.Select((relic, index) => new LegalAction($"choose_treasure:{index}:{Entry(relic)}", "choose_treasure", new Dictionary<string, object?> { ["option_index"] = index, ["model_id"] = Entry(relic) })).ToList();
        actions.Add(new("skip_treasure", "skip_treasure", new Dictionary<string, object?>()));
        return actions;
    }

    private async Task OpenTreasureAsync()
    {
        if (_treasureRoom is null || _treasureOpened) throw new ProtocolException("invalid_action", "No unopened native treasure room is active.");
        object result = ReflectionTools.Invoke(_treasureRoom, "DoNormalRewards")!;
        if (result is not Task task) throw new ProtocolException("invalid_state", "Native treasure rewards did not return a task.");
        await task.ConfigureAwait(false);
        _treasureOpened = true;
        if (ReflectionTools.Get(_treasureSynchronizer!, "CurrentRelics") is null) _treasureResolved = true;
    }

    private async Task ChooseTreasureAsync(int? optionIndex)
    {
        if (!_treasureOpened || _treasureResolved || _treasureSynchronizer is null) throw new ProtocolException("invalid_action", "No native treasure relic choice is active.");
        object[] relics = ReflectionTools.Enumerate(ReflectionTools.Get(_treasureSynchronizer, "CurrentRelics")).Where(relic => relic is not null).Select(relic => relic!).ToArray();
        if (optionIndex is { } index && (index < 0 || index >= relics.Length)) throw new ProtocolException("invalid_action", $"Treasure relic {index} is not selectable.");
        object? selected = optionIndex is { } selectedIndex ? relics[selectedIndex] : null;
        ReflectionTools.Invoke(_treasureSynchronizer, "OnPicked", _player!, optionIndex);
        if (selected is not null)
        {
            object mutable = ReflectionTools.Invoke(selected, "ToMutable")!;
            object obtain = ReflectionTools.InvokeStatic(T("MegaCrit.Sts2.Core.Commands.RelicCmd"), "Obtain", mutable, _player!, -1)!;
            if (obtain is Task task) await task.ConfigureAwait(false);
        }
        _treasureResolved = true;
    }

    private EnvironmentResult CaptureTreasure(object? transition)
    {
        EnsureReset(); object deck = ReflectionTools.Get(_player!, "Deck")!;
        object[] relicOptions = !_treasureOpened || _treasureResolved || _treasureSynchronizer is null || ReflectionTools.Get(_treasureSynchronizer, "CurrentRelics") is null
            ? [] : ReflectionTools.Enumerate(ReflectionTools.Get(_treasureSynchronizer, "CurrentRelics")).Where(relic => relic is not null).Select((relic, index) => new { option_index = index, model_id = Entry(relic!) }).Cast<object>().ToArray();
        LegalAction[] actions = BuildTreasureActions().ToArray();
        object observation = new
        {
            schema_version = ProtocolConstants.ObservationSchemaVersion,
            game_build = new { version = _productVersion, assembly_sha256 = _assemblyHash, pck_sha256 = _pckHash },
            run = RunInventorySnapshot(deck),
            treasure = new { opened = _treasureOpened, resolved = _treasureResolved, relic_options = relicOptions },
            decision = new { kind = !_treasureOpened ? "treasure_open" : _treasureResolved ? "treasure_complete" : "treasure_relic_choice", legal_actions = actions },
            terminal = false, victory = false
        };
        _hash = ComputeStateHash(observation); string handle = GetOrAddCurrentBranch();
        return new(observation, _hash, actions, false, false, handle, transition, ScoringFeatures());
    }

    private object[] MerchantEntries()
        => _merchantInventory is null ? [] : ReflectionTools.Enumerate(ReflectionTools.Get(_merchantInventory, "AllEntries")).Where(entry => entry is not null).Select(entry => entry!).ToArray();

    private IReadOnlyList<LegalAction> BuildShopActions()
    {
        if (_pendingRewardsSet is not null) return BuildCustomRewardActions();
        if (_pendingChoice is not null) return BuildChoiceActions(_pendingChoice);
        List<LegalAction> actions = [];
        object[] entries = MerchantEntries();
        for (int index = 0; index < entries.Length; index++)
        {
            object entry = entries[index];
            if (!(bool)ReflectionTools.Get(entry, "IsStocked")! || !(bool)ReflectionTools.Get(entry, "EnoughGold")!) continue;
            (string kind, string? modelId) = MerchantEntryIdentity(entry);
            actions.Add(new($"buy_shop:{index}:{kind}:{modelId ?? "none"}", "buy_shop", new Dictionary<string, object?> { ["entry_index"] = index, ["entry_kind"] = kind, ["model_id"] = modelId, ["cost"] = ReflectionTools.Get(entry, "Cost") }));
        }
        actions.Add(new("leave_shop", "leave_shop", new Dictionary<string, object?>()));
        return actions;
    }

    private async Task BuyShopAsync(int entryIndex)
    {
        object[] entries = MerchantEntries();
        if (entryIndex < 0 || entryIndex >= entries.Length) throw new ProtocolException("invalid_action", $"Merchant entry {entryIndex} is not selectable.");
        object entry = entries[entryIndex];
        if (!(bool)ReflectionTools.Get(entry, "IsStocked")! || !(bool)ReflectionTools.Get(entry, "EnoughGold")!) throw new ProtocolException("invalid_action", $"Merchant entry {entryIndex} cannot be purchased.");
        if (entry.GetType().Name == "MerchantCardRemovalEntry")
            await StartTransitionAsync(() => PurchaseHeadlessCardRemovalAsync(entry));
        else
            await StartTransitionAsync(() => (Task)ReflectionTools.Invoke(entry, "OnTryPurchaseWrapper", _merchantInventory, false)!);
    }

    private async Task PurchaseHeadlessCardRemovalAsync(object entry)
    {
        Task purchase = (Task)ReflectionTools.Invoke(entry, "OnTryPurchaseWrapper", _merchantInventory, false, true)!;
        await purchase;
        bool success = Convert.ToBoolean(ReflectionTools.Get(purchase, "Result"));
        if (success) ReflectionTools.Invoke(entry, "SetUsed");
    }

    private EnvironmentResult CaptureShop(object? transition)
    {
        EnsureReset(); object deck = ReflectionTools.Get(_player!, "Deck")!;
        object[] entries = MerchantEntries().Select((entry, index) =>
        {
            (string kind, string? modelId) = MerchantEntryIdentity(entry);
            return (object)new { entry_index = index, kind, model_id = modelId, cost = ReflectionTools.Get(entry, "Cost"), stocked = ReflectionTools.Get(entry, "IsStocked"), enough_gold = ReflectionTools.Get(entry, "EnoughGold") };
        }).ToArray();
        LegalAction[] actions = BuildShopActions().ToArray();
        object observation = new
        {
            schema_version = ProtocolConstants.ObservationSchemaVersion,
            game_build = new { version = _productVersion, assembly_sha256 = _assemblyHash, pck_sha256 = _pckHash },
            run = RunInventorySnapshot(deck), shop = new { entries }, outstanding_choice = _pendingChoice?.Snapshot(), outstanding_rewards = CustomRewardsSnapshot(),
            decision = new { kind = _pendingRewardsSet is not null ? "custom_reward_choice" : _pendingChoice is null ? "shop_choice" : _pendingChoice.DecisionKind, legal_actions = actions }, terminal = false, victory = false
        };
        _hash = ComputeStateHash(observation); string handle = GetOrAddCurrentBranch();
        return new(observation, _hash, actions, false, false, handle, transition, ScoringFeatures());
    }

    private (string Kind, string? ModelId) MerchantEntryIdentity(object entry)
    {
        string type = entry.GetType().Name;
        (string Kind, string? ModelId)? current = type switch
        {
            "MerchantCardEntry" when ReflectionTools.Get(entry, "CreationResult") is { } creation => ("card", Entry(ReflectionTools.Get(creation, "Card")!)),
            "MerchantRelicEntry" when ReflectionTools.Get(entry, "Model") is { } relic => ("relic", Entry(relic)),
            "MerchantPotionEntry" when ReflectionTools.Get(entry, "Model") is { } potion => ("potion", Entry(potion)),
            "MerchantCardRemovalEntry" => ("card_removal", null),
            _ => null
        };
        if (current is { } identity) { _merchantEntryIdentities[entry] = identity; return identity; }
        if (_merchantEntryIdentities.TryGetValue(entry, out (string Kind, string? ModelId) prior)) return prior;
        throw new ProtocolException("unsupported_shop_entry", $"Native merchant entry '{type}' is not connected.");
    }

    private object RunInventorySnapshot(object deck) => new
    {
        seed = _reset!.Seed, ascension = _reset.Ascension, gold = ReflectionTools.Get(_player!, "Gold"), rng_counters = RunRngCounters(),
        deck = ReflectionTools.Enumerate(ReflectionTools.Get(deck, "Cards")).Where(card => card is not null).Select(card => new { model_id = Entry(card!), upgrades = ReflectionTools.Get(card!, "CurrentUpgradeLevel") }).ToArray(),
        relics = ReflectionTools.Enumerate(ReflectionTools.Get(_player!, "Relics")).Where(relic => relic is not null).Select(relic => Entry(relic!)).ToArray(),
        potions = ReflectionTools.Enumerate(ReflectionTools.Get(_player!, "PotionSlots")).Select(potion => potion is null ? null : Entry(potion)).ToArray()
    };

    private object ScoringFeatures()
    {
        object creature = ReflectionTools.Get(_player!, "Creature")!, deck = ReflectionTools.Get(_player!, "Deck")!;
        bool inCombat = _combat is not null && ((_runMode && _runStage == "combat") ||
            (!_runMode && !_mapMode && !_rewardMode && !_restMode && !_eventMode && !_customRewardMode));
        object[] combatCreatures = inCombat
            ? ReflectionTools.Enumerate(ReflectionTools.Get(_combat!, "Creatures")).Where(value => value is not null).Select(value => value!).ToArray()
            : [];
        object? currentPoint = ReflectionTools.Get(_run!, "CurrentMapPoint"), currentCoord = currentPoint is null ? null : ReflectionTools.Get(currentPoint, "coord");
        return new
        {
            schema_version = 3,
            character = Entry(ReflectionTools.Get(_player!, "Character")!),
            ascension = _reset!.Ascension,
            current_hp = ReflectionTools.Get(creature, "CurrentHp"), max_hp = ReflectionTools.Get(creature, "MaxHp"), block = ReflectionTools.Get(creature, "Block"),
            gold = ReflectionTools.Get(_player!, "Gold"),
            act_index = ReflectionTools.Get(_run!, "CurrentActIndex"), act_floor = ReflectionTools.Get(_run!, "ActFloor"),
            map_col = currentCoord is null ? null : ReflectionTools.Get(currentCoord, "col"), map_row = currentCoord is null ? null : ReflectionTools.Get(currentCoord, "row"),
            deck = ReflectionTools.Enumerate(ReflectionTools.Get(deck, "Cards")).Where(card => card is not null).Select(card => new
            {
                model_id = Entry(card!), upgrades = ReflectionTools.Get(card!, "CurrentUpgradeLevel"), enchantment = ReflectionTools.Get(card!, "Enchantment") is { } enchantment ? Entry(enchantment) : null
            }).ToArray(),
            relics = ReflectionTools.Enumerate(ReflectionTools.Get(_player!, "Relics")).Where(relic => relic is not null).Select(relic => Entry(relic!)).ToArray(),
            potion_count = ReflectionTools.Enumerate(ReflectionTools.Get(_player!, "PotionSlots")).Count(potion => potion is not null),
            potion_capacity = ReflectionTools.Enumerate(ReflectionTools.Get(_player!, "PotionSlots")).Count,
            combat = !inCombat ? null : new
            {
                turn = ReflectionTools.Get(_pcs!, "TurnNumber"), energy = ReflectionTools.Get(_pcs!, "Energy"), max_energy = ReflectionTools.Get(_pcs!, "MaxEnergy"), stars = ReflectionTools.Get(_pcs!, "Stars"),
                piles = new
                {
                    hand = ScoringPile("Hand"), draw = ScoringPile("DrawPile"), discard = ScoringPile("DiscardPile"),
                    exhaust = ScoringPile("ExhaustPile"), play = ScoringPile("PlayPile")
                },
                creatures = combatCreatures.Select(ScoringCreature).ToArray()
            }
        };
    }

    private object ScoringCreature(object creature)
    {
        object? monster = ReflectionTools.Get(creature, "Monster"), move = monster is null ? null : ReflectionTools.Get(monster, "NextMove");
        object[] intents = move is null ? [] : ReflectionTools.Enumerate(ReflectionTools.Get(move, "Intents")).Where(value => value is not null).Select(value => Intent(value!, creature)).ToArray();
        return new
        {
            model_id = ReflectionTools.Get(ReflectionTools.Get(creature, "ModelId")!, "Entry"), side = ReflectionTools.Get(creature, "Side")!.ToString(),
            hp = ReflectionTools.Get(creature, "CurrentHp"), max_hp = ReflectionTools.Get(creature, "MaxHp"), block = ReflectionTools.Get(creature, "Block"), alive = ReflectionTools.Get(creature, "IsAlive"),
            powers = ReflectionTools.Enumerate(ReflectionTools.Get(creature, "Powers")).Where(power => power is not null).Select(power => new { model_id = Entry(power!), amount = ReflectionTools.Get(power!, "Amount") }).ToArray(),
            next_move = move is null ? null : new { id = ReflectionTools.Get(move, "Id"), intents }
        };
    }

    private object[] ScoringPile(string name) => ReflectionTools.Enumerate(ReflectionTools.Get(ReflectionTools.Get(_pcs!, name)!, "Cards"))
        .Where(card => card is not null).Select(card => (object)new { model_id = Entry(card!), upgrades = ReflectionTools.Get(card!, "CurrentUpgradeLevel") }).ToArray();

    private IReadOnlyList<LegalAction> BuildCustomRewardActions()
    {
        if (_pendingChoice is not null) return BuildChoiceActions(_pendingChoice);
        if (_pendingRewardsSet is null) return [];
        List<LegalAction> actions = [];
        object[] rewards = ReflectionTools.Enumerate(ReflectionTools.Get(_pendingRewardsSet, "Rewards")).Where(reward => reward is not null).Select(reward => reward!).ToArray();
        for (int rewardIndex = 0; rewardIndex < rewards.Length; rewardIndex++)
        {
            object top = rewards[rewardIndex];
            if ((bool)ReflectionTools.Get(top, "SuccessfullySelected")!) continue;
            if (top.GetType().Name == "LinkedRewardSet")
            {
                object[] children = ReflectionTools.Enumerate(ReflectionTools.Get(top, "Rewards")).Where(reward => reward is not null).Select(reward => reward!).ToArray();
                for (int childIndex = 0; childIndex < children.Length; childIndex++) AddCustomRewardActions(actions, rewardIndex, childIndex, children[childIndex]);
            }
            else AddCustomRewardActions(actions, rewardIndex, -1, top);
        }
        if (!(bool)ReflectionTools.Get(_pendingRewardsSet, "DisallowSkipping")!) actions.Add(new("skip_custom_rewards", "skip_custom_rewards", new Dictionary<string, object?>()));
        return actions;
    }

    private async Task InitializeCustomRewardsAsync()
    {
        if (_customRewardKinds.Length == 0) throw new ProtocolException("invalid_reward_kind", "At least one native custom reward kind is required.");
        if (ReflectionTools.Get(_run!, "CurrentMapPointHistoryEntry") is null)
        {
            object pointType = Enum.Parse(T("MegaCrit.Sts2.Core.Map.MapPointType"), "Unknown"), roomType = Enum.Parse(T("MegaCrit.Sts2.Core.Rooms.RoomType"), "Event");
            ReflectionTools.Invoke(_run!, "AppendToMapPointHistory", pointType, roomType, null);
        }
        Type rewardType = T("MegaCrit.Sts2.Core.Rewards.Reward");
        List<object?> rewards = _customRewardKinds.Select(CreateCustomReward).Cast<object?>().ToList();
        if (_customRewardsLinked)
        {
            object children = List(rewardType, rewards);
            rewards = [ReflectionTools.Create(T("MegaCrit.Sts2.Core.Rewards.LinkedRewardSet"), children, _player!)];
        }
        object typedRewards = List(rewardType, rewards);
        await StartTransitionAsync(() => (Task)ReflectionTools.InvokeStatic(T("MegaCrit.Sts2.Core.Commands.RewardsCmd"), "OfferCustom", _player!, typedRewards)!);
        if (_pendingRewardsSet is null) throw new ProtocolException("invalid_state", "Native custom rewards completed without exposing a reward decision.");
    }

    private object CreateCustomReward(string kind) => kind switch
    {
        "card_removal" => ReflectionTools.Create(T("MegaCrit.Sts2.Core.Rewards.CardRemovalReward"), _player!),
        "potion" => ReflectionTools.Create(T("MegaCrit.Sts2.Core.Rewards.PotionReward"), _player!),
        "relic" => ReflectionTools.Create(T("MegaCrit.Sts2.Core.Rewards.RelicReward"), _player!),
        "gold" => ReflectionTools.Create(T("MegaCrit.Sts2.Core.Rewards.GoldReward"), 10, 20, _player!, false),
        _ => throw new ProtocolException("invalid_reward_kind", $"Unsupported native custom reward kind '{kind}'.")
    };

    private EnvironmentResult CaptureCustomRewards(object? transition)
    {
        EnsureReset(); object deck = ReflectionTools.Get(_player!, "Deck")!; LegalAction[] actions = BuildCustomRewardActions().ToArray();
        object observation = new
        {
            schema_version = ProtocolConstants.ObservationSchemaVersion,
            game_build = new { version = _productVersion, assembly_sha256 = _assemblyHash, pck_sha256 = _pckHash },
            run = RunInventorySnapshot(deck), custom_rewards = CustomRewardsSnapshot(), outstanding_choice = _pendingChoice?.Snapshot(),
            decision = new { kind = _pendingChoice is not null ? _pendingChoice.DecisionKind : _pendingRewardsSet is not null ? "custom_reward_choice" : "custom_reward_complete", legal_actions = actions },
            terminal = false, victory = false
        };
        _hash = ComputeStateHash(observation); string handle = GetOrAddCurrentBranch();
        return new(observation, _hash, actions, false, false, handle, transition, ScoringFeatures());
    }

    private void AddCustomRewardActions(List<LegalAction> actions, int rewardIndex, int childIndex, object reward)
    {
        string kind = RewardKind(reward);
        if (reward.GetType().Name == "CardReward")
        {
            object[] cards = ReflectionTools.Enumerate(ReflectionTools.Get(reward, "Cards")).Where(card => card is not null).Select(card => card!).ToArray();
            for (int optionIndex = 0; optionIndex < cards.Length; optionIndex++)
            {
                string modelId = Entry(cards[optionIndex]);
                actions.Add(new($"choose_custom_reward:{rewardIndex}:{childIndex}:{optionIndex}:{kind}:{modelId}", "choose_custom_reward", new Dictionary<string, object?> { ["reward_index"] = rewardIndex, ["child_index"] = childIndex, ["option_index"] = optionIndex, ["reward_kind"] = kind, ["model_id"] = modelId }));
            }
            return;
        }
        string? id = RewardModelId(reward);
        actions.Add(new($"choose_custom_reward:{rewardIndex}:{childIndex}:0:{kind}:{id ?? "none"}", "choose_custom_reward", new Dictionary<string, object?> { ["reward_index"] = rewardIndex, ["child_index"] = childIndex, ["option_index"] = 0, ["reward_kind"] = kind, ["model_id"] = id }));
    }

    private async Task ChooseCustomRewardAsync(int rewardIndex, int childIndex, int optionIndex)
    {
        if (_pendingRewardsSet is null) throw new ProtocolException("invalid_action", "No native custom reward set is active.");
        Task offer = _continuationTask ?? throw new ProtocolException("invalid_state", "Custom rewards have no suspended native offer task.");
        object[] rewards = ReflectionTools.Enumerate(ReflectionTools.Get(_pendingRewardsSet, "Rewards")).Where(reward => reward is not null).Select(reward => reward!).ToArray();
        if (rewardIndex < 0 || rewardIndex >= rewards.Length) throw new ProtocolException("invalid_action", $"Custom reward {rewardIndex} is not selectable.");
        object top = rewards[rewardIndex], reward = top;
        if (childIndex >= 0)
        {
            if (top.GetType().Name != "LinkedRewardSet") throw new ProtocolException("invalid_action", $"Custom reward {rewardIndex} is not linked.");
            object[] children = ReflectionTools.Enumerate(ReflectionTools.Get(top, "Rewards")).Where(child => child is not null).Select(child => child!).ToArray();
            if (childIndex >= children.Length) throw new ProtocolException("invalid_action", $"Linked reward child {childIndex} is not selectable.");
            reward = children[childIndex];
        }
        _rewardSelectionIndex = reward.GetType().Name == "CardReward" ? optionIndex : null;
        object synchronizer = ReflectionTools.Get(ReflectionTools.GetStatic(T("MegaCrit.Sts2.Core.Runs.RunManager"), "Instance")!, "RewardsSetSynchronizer")!;
        _choiceBegun = new(TaskCreationOptions.RunContinuationsAsynchronously);
        Task selection = (Task)ReflectionTools.Invoke(synchronizer, "SelectLocalReward", reward)!;
        _pendingRewardSelection = new(top, reward, offer, childIndex >= 0);
        _continuationTask = selection;
        Task completed = await Task.WhenAny(selection, _choiceBegun.Task).ConfigureAwait(false);
        if (completed != selection) { await _choiceBegun.Task.ConfigureAwait(false); return; }
        await selection.ConfigureAwait(false);
        _continuationTask = null; _choiceBegun = null; _rewardSelectionIndex = null;
        await FinalizeCustomRewardSelectionAsync();
    }

    private async Task FinalizeCustomRewardSelectionAsync()
    {
        PendingRewardSelection pending = _pendingRewardSelection ?? throw new ProtocolException("invalid_state", "No custom reward selection is pending.");
        _pendingRewardSelection = null;
        object synchronizer = ReflectionTools.Get(ReflectionTools.GetStatic(T("MegaCrit.Sts2.Core.Runs.RunManager"), "Instance")!, "RewardsSetSynchronizer")!;
        if (pending.IsLinked)
        {
            ReflectionTools.Invoke(pending.TopReward, "OnSkipped");
            if (ReflectionTools.Invoke(synchronizer, "SelectLocalReward", pending.TopReward) is Task parentSelection) await parentSelection.ConfigureAwait(false);
        }
        bool complete = (bool)ReflectionTools.Get(_pendingRewardsSet!, "AllRewardsSuccessfullySelected")!;
        if (complete)
        {
            await pending.OfferTask.ConfigureAwait(false);
            _pendingRewardsSet = null; _continuationTask = null; _choiceBegun = null;
        }
        else
        {
            _continuationTask = pending.OfferTask;
            _choiceBegun = new(TaskCreationOptions.RunContinuationsAsynchronously);
        }
    }

    private async Task SkipCustomRewardsAsync()
    {
        if (_pendingRewardsSet is null || (bool)ReflectionTools.Get(_pendingRewardsSet, "DisallowSkipping")!) throw new ProtocolException("invalid_action", "Native custom rewards cannot be skipped.");
        Task offer = _continuationTask ?? throw new ProtocolException("invalid_state", "Custom rewards have no suspended native offer task.");
        object synchronizer = ReflectionTools.Get(ReflectionTools.GetStatic(T("MegaCrit.Sts2.Core.Runs.RunManager"), "Instance")!, "RewardsSetSynchronizer")!;
        ReflectionTools.Invoke(synchronizer, "SkipLocalRewardsSet");
        await offer.ConfigureAwait(false);
        _pendingRewardsSet = null; _pendingRewardSelection = null; _continuationTask = null; _choiceBegun = null;
    }

    private object? CustomRewardsSnapshot()
    {
        if (_pendingRewardsSet is null) return null;
        object SnapshotReward(object reward) => new { kind = RewardKind(reward), model_id = RewardModelId(reward), implementation = reward.GetType().Name, selected = ReflectionTools.Get(reward, "SuccessfullySelected") };
        object[] rewards = ReflectionTools.Enumerate(ReflectionTools.Get(_pendingRewardsSet, "Rewards")).Where(reward => reward is not null).Select((reward, index) =>
        {
            object model = reward!;
            object[] children = model.GetType().Name == "LinkedRewardSet" ? ReflectionTools.Enumerate(ReflectionTools.Get(model, "Rewards")).Where(child => child is not null).Select(child => SnapshotReward(child!)).ToArray() : [];
            return (object)new { reward_index = index, reward = SnapshotReward(model), children };
        }).ToArray();
        return new { rewards, can_skip = !(bool)ReflectionTools.Get(_pendingRewardsSet, "DisallowSkipping")! };
    }

    private static string RewardKind(object reward) => reward.GetType().Name.Replace("Reward", "", StringComparison.Ordinal).ToLowerInvariant();
    private static string? RewardModelId(object reward)
    {
        object? model = ReflectionTools.Get(reward, "Relic") ?? ReflectionTools.Get(reward, "Potion") ?? ReflectionTools.Get(reward, "Card") ?? ReflectionTools.Get(reward, "_card");
        return model is null ? null : Entry(model);
    }

    private EnvironmentResult CaptureRoomRewards(object? transition)
    {
        EnsureReset();
        if (_roomRewardsSet is null) throw new ProtocolException("invalid_state", "Reward stage has no native RewardsSet.");
        object[] rewards = ReflectionTools.Enumerate(ReflectionTools.Get(_roomRewardsSet, "Rewards")).Where(reward => reward is not null).Select((reward, index) =>
        {
            object model = reward!;
            object[] options = model.GetType().Name == "CardReward"
                ? ReflectionTools.Enumerate(ReflectionTools.Get(model, "Cards")).Where(card => card is not null).Select(card => (object)new { model_id = Entry(card!) }).ToArray()
                : ReflectionTools.Get(model, "Relic") is { } relic ? [new { model_id = Entry(relic) }]
                : ReflectionTools.Get(model, "Potion") is { } potion ? [new { model_id = Entry(potion) }]
                : [];
            return (object)new { reward_index = index, kind = model.GetType().Name.Replace("Reward", "", StringComparison.Ordinal).ToLowerInvariant(), options, resolved = _resolvedRoomRewards.Contains(index) || (bool)ReflectionTools.Get(model, "SuccessfullySelected")! };
        }).ToArray();
        object deck = ReflectionTools.Get(_player!, "Deck")!; LegalAction[] actions = BuildRoomRewardActions().ToArray();
        object observation = new
        {
            schema_version = ProtocolConstants.ObservationSchemaVersion,
            game_build = new { version = _productVersion, assembly_sha256 = _assemblyHash, pck_sha256 = _pckHash },
            run = new { seed = _reset!.Seed, ascension = _reset.Ascension, gold = ReflectionTools.Get(_player!, "Gold"), rng_counters = RunRngCounters(), deck = ReflectionTools.Enumerate(ReflectionTools.Get(deck, "Cards")).Where(card => card is not null).Select(card => Entry(card!)).ToArray(), relics = ReflectionTools.Enumerate(ReflectionTools.Get(_player!, "Relics")).Where(relic => relic is not null).Select(relic => Entry(relic!)).ToArray(), potions = ReflectionTools.Enumerate(ReflectionTools.Get(_player!, "PotionSlots")).Select(potion => potion is null ? null : Entry(potion)).ToArray() },
            room_rewards = new { rewards },
            decision = new { kind = "room_reward_choice", legal_actions = actions }, terminal = false, victory = false
        };
        _hash = ComputeStateHash(observation); string handle = GetOrAddCurrentBranch();
        return new(observation, _hash, actions, false, false, handle, transition, ScoringFeatures());
    }

    private object Creature(object c)
    {
        object? monster = ReflectionTools.Get(c, "Monster"), move = monster is null ? null : ReflectionTools.Get(monster, "NextMove");
        object[] intents = move is null ? [] : ReflectionTools.Enumerate(ReflectionTools.Get(move, "Intents")).Where(x => x is not null).Select(x => Intent(x!, c)).ToArray();
        return new { combat_id = ReflectionTools.Get(c, "CombatId"), model_id = ReflectionTools.Get(ReflectionTools.Get(c, "ModelId")!, "Entry"), side = ReflectionTools.Get(c, "Side")!.ToString(), hp = ReflectionTools.Get(c, "CurrentHp"), max_hp = ReflectionTools.Get(c, "MaxHp"), block = ReflectionTools.Get(c, "Block"), alive = ReflectionTools.Get(c, "IsAlive"), next_move = move is null ? null : new { id = ReflectionTools.Get(move, "Id"), intents }, powers = ReflectionTools.Enumerate(ReflectionTools.Get(c, "Powers")).Where(x => x is not null).Select(x => new { model_id = Entry(x!), amount = ReflectionTools.Get(x!, "Amount") }).ToArray() };
    }
    private object Intent(object i, object owner)
    {
        object? damage = null, repeats = null; if (i.GetType().IsSubclassOf(T("MegaCrit.Sts2.Core.MonsterMoves.Intents.AttackIntent"))) { damage = ReflectionTools.Invoke(i, "GetSingleDamage", ReflectionTools.Get(ReflectionTools.Get(owner, "CombatState")!, "Allies"), owner); repeats = ReflectionTools.Get(i, "Repeats"); }
        return new { intent_type = ReflectionTools.Get(i, "IntentType")!.ToString(), implementation = i.GetType().Name, damage, repeats };
    }
    private object Pile(string name)
    {
        object pile = ReflectionTools.Get(_pcs!, name)!, db = ReflectionTools.GetStatic(T("MegaCrit.Sts2.Core.GameActions.Multiplayer.NetCombatCardDb"), "Instance")!;
        return new { name, type = ReflectionTools.Get(pile, "Type")!.ToString(), cards = ReflectionTools.Enumerate(ReflectionTools.Get(pile, "Cards")).Where(x => x is not null).Select(x => { object cost = ReflectionTools.Get(x!, "EnergyCost")!; return new { instance_id = GetCardInstanceId(x!), net_id = ReflectionTools.Invoke(db, "GetCardId", x!), model_id = Entry(x!), card_type = ReflectionTools.Get(x!, "Type")!.ToString(), target_type = ReflectionTools.Get(x!, "TargetType")!.ToString(), energy_cost = ReflectionTools.Invoke(cost, "GetResolved"), costs_x = ReflectionTools.Get(cost, "CostsX"), upgrades = ReflectionTools.Get(x!, "CurrentUpgradeLevel"), enchantment = ReflectionTools.Get(x!, "Enchantment") is { } enchantment ? new { model_id = Entry(enchantment), amount = ReflectionTools.Get(enchantment, "Amount") } : null, native_state = SavedNativeState(x!) }; }).ToArray() };
    }

    private string GetCardInstanceId(object card)
    {
        if (_cardInstanceIds.TryGetValue(card, out string? id)) return id;
        id = $"dynamic-{_dynamicCardOrdinal++}-{Entry(card)}";
        _cardInstanceIds.Add(card, id);
        return id;
    }

    private void ApplyRngCounters(ResetRequest request)
    {
        if (request.RngCounters is not { Count: > 0 }) return;
        Type rngType = T("MegaCrit.Sts2.Core.Entities.Rngs.RunRngType");
        object save = ReflectionTools.Create(T("MegaCrit.Sts2.Core.Saves.Runs.SerializableRunRngSet"));
        ReflectionTools.Set(save, "Seed", request.Seed);
        IDictionary counters = (IDictionary)ReflectionTools.Get(save, "Counters")!;
        foreach ((string name, int count) in request.RngCounters)
        {
            if (count < 0) throw new ProtocolException("invalid_reset", $"RNG counter {name} cannot be negative.");
            string normalized = name.Replace("_", "", StringComparison.Ordinal);
            object key = Enum.GetValues(rngType).Cast<object>().SingleOrDefault(x => x.ToString()!.Replace("_", "", StringComparison.Ordinal).Equals(normalized, StringComparison.OrdinalIgnoreCase))
                ?? throw new ProtocolException("invalid_reset", $"Unknown RNG stream '{name}'.");
            counters.Add(key, count);
        }
        ReflectionTools.Invoke(ReflectionTools.Get(_run!, "Rng")!, "LoadFromSerializable", save);
    }

    private static void ApplyNativeState(object model, IReadOnlyDictionary<string, JsonElement>? state)
    {
        if (state is not { Count: > 0 }) return;
        foreach ((string name, JsonElement value) in state)
        {
            PropertyInfo property = model.GetType().GetProperty(name, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance)
                ?? throw new ProtocolException("unsupported_reset_field", $"{model.GetType().Name} has no native saved property '{name}'.");
            if (!property.GetCustomAttributes(true).Any(x => x.GetType().Name == "SavedPropertyAttribute"))
                throw new ProtocolException("unsupported_reset_field", $"{model.GetType().Name}.{name} is not a native [SavedProperty].");
            object converted = property.PropertyType == typeof(int) ? value.GetInt32()
                : property.PropertyType == typeof(bool) ? value.GetBoolean()
                : property.PropertyType == typeof(string) ? value.GetString()!
                : property.PropertyType.IsEnum ? Enum.ToObject(property.PropertyType, value.GetInt32())
                : property.PropertyType == typeof(int[]) ? value.EnumerateArray().Select(x => x.GetInt32()).ToArray()
                : throw new ProtocolException("unsupported_reset_field", $"Saved property type {property.PropertyType.Name} is not supported yet.");
            property.SetValue(model, converted);
        }
    }

    private static void ApplyRelicCounter(object relic, int counter)
    {
        PropertyInfo[] candidates = relic.GetType().GetProperties(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance)
            .Where(x => x.PropertyType == typeof(int) && x.GetCustomAttributes(true).Any(a => a.GetType().Name == "SavedPropertyAttribute")).ToArray();
        if (candidates.Length != 1) throw new ProtocolException("unsupported_reset_field", $"Relic {Entry(relic)} does not have one unambiguous native integer counter; supply native_state by saved-property name.");
        candidates[0].SetValue(relic, counter);
    }

    private static IReadOnlyDictionary<string, object?> SavedNativeState(object model)
    {
        if (model.GetType().GetMethod("ToSerializable", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance, Type.EmptyTypes) is null)
            return new SortedDictionary<string, object?>(StringComparer.Ordinal);
        object serial = ReflectionTools.Invoke(model, "ToSerializable")!;
        object? props = ReflectionTools.Get(serial, "Props");
        SortedDictionary<string, object?> result = new(StringComparer.Ordinal);
        if (props is null) return result;
        foreach (string fieldName in new[] { "ints", "bools", "strings", "intArrays" })
        {
            object? list = ReflectionTools.Get(props, fieldName);
            if (list is null) continue;
            foreach (object? item in ReflectionTools.Enumerate(list)) if (item is not null)
                result[Convert.ToString(ReflectionTools.Get(item, "name"))!] = ReflectionTools.Get(item, "value");
        }
        return result;
    }

    private SortedDictionary<string, int> RunRngCounters()
    {
        object serial = ReflectionTools.Invoke(ReflectionTools.Get(_run!, "Rng")!, "ToSerializable")!;
        SortedDictionary<string, int> counters = new(StringComparer.Ordinal);
        foreach (object? pair in ReflectionTools.Enumerate(ReflectionTools.Get(serial, "Counters")))
            if (pair is not null) counters[Convert.ToString(ReflectionTools.Get(pair, "Key"))!] = Convert.ToInt32(ReflectionTools.Get(pair, "Value"));
        return counters;
    }

    private bool Alive(string side) => _combat is not null && ReflectionTools.Enumerate(ReflectionTools.Get(_combat, side)).Any(x => x is not null && (bool)ReflectionTools.Get(x, "IsAlive")!);
    private bool PlayerAlive() => _player is not null && ReflectionTools.Get(_player, "Creature") is { } creature && (bool)ReflectionTools.Get(creature, "IsAlive")!;
    private object Mutable(string collection, string id)
    {
        object canonical = Find(ReflectionTools.GetStatic(T("MegaCrit.Sts2.Core.Models.ModelDb"), collection)!, id);
        MethodInfo? toMutable = canonical.GetType().GetMethods(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance)
            .FirstOrDefault(m => m.Name == "ToMutable" && m.GetParameters().Length == 0);
        if (toMutable is not null)
            return toMutable.Invoke(canonical, null)!;
        MethodInfo? mutableClone = canonical.GetType().GetMethods(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance)
            .FirstOrDefault(m => m.Name == "MutableClone" && m.GetParameters().Length == 0);
        if (mutableClone is not null)
            return mutableClone.Invoke(canonical, null)!;
        return ReflectionTools.Invoke(canonical, "MutableClone")!;
    }
    private static object Find(object models, string id) => ReflectionTools.Enumerate(models).Where(x => x is not null).SingleOrDefault(x => Entry(x!).Equals(id, StringComparison.OrdinalIgnoreCase) || x!.GetType().Name.Equals(id, StringComparison.OrdinalIgnoreCase)) ?? throw new ProtocolException("unknown_model", id);
    private static string Entry(object model) => Convert.ToString(ReflectionTools.Get(ReflectionTools.Get(model, "Id") ?? ReflectionTools.Get(model, "ModelId")!, "Entry"))!;
    private Type T(string name) => _context.RequireType(name);
    private static object List(Type type, IReadOnlyList<object?> items) { IList list = (IList)Activator.CreateInstance(typeof(List<>).MakeGenericType(type))!; foreach (object? item in items) list.Add(item); return list; }
    private void EnsureReset() { if (_reset is null) throw new ProtocolException("not_reset", "Call reset first."); }
    private void Validate(ResetRequest r)
    {
        if (r.GameBuild.AssemblySha256 is { Length: > 0 } h && !h.Equals(_assemblyHash, StringComparison.OrdinalIgnoreCase)) throw new ProtocolException("build_mismatch", h);
        if (r.GameBuild.PckSha256 is { Length: > 0 } p && !p.Equals(_pckHash, StringComparison.OrdinalIgnoreCase)) throw new ProtocolException("build_mismatch", p);
        if (r.GameBuild.Version is { Length: > 0 } v && !v.Equals(_productVersion, StringComparison.OrdinalIgnoreCase)) throw new ProtocolException("build_mismatch", v);
        if (r.Ascension < 0 || r.Ascension > 10) throw new ProtocolException("invalid_reset", "Ascension must be between 0 and 10.");
        if (r.CurrentHp < 1 || r.CurrentHp > r.MaxHp || (!r.UseCharacterStartingLoadout && r.Deck.Count == 0) || r.Stars < 0) throw new ProtocolException("invalid_reset", "Invalid HP, stars, or empty custom deck.");
        if (r.UseCharacterStartingLoadout && (r.Deck.Count > 0 || (r.Relics?.Count ?? 0) > 0 || (r.Potions?.Count ?? 0) > 0 || (r.InitialHand?.Count ?? 0) > 0 || r.InitialDrawPile is not null))
            throw new ProtocolException("invalid_reset", "A character starting loadout cannot be combined with supplied deck, relic, potion, hand, or draw-pile state.");
        if (r.Deck.Any(c => c is null) || (r.Relics ?? []).Any(x => x is null) || (r.Potions ?? []).Any(x => x is null)) throw new ProtocolException("invalid_reset", "Deck, relic, and potion arrays cannot contain null entries.");
        if (r.Deck.Any(c => string.IsNullOrWhiteSpace(c.InstanceId)) || r.Deck.Select(c => c.InstanceId).Distinct(StringComparer.Ordinal).Count() != r.Deck.Count) throw new ProtocolException("invalid_reset", "Every deck card requires a unique non-empty instance_id.");
        if ((r.InitialHand ?? []).Distinct(StringComparer.Ordinal).Count() != (r.InitialHand?.Count ?? 0) || (r.InitialHand ?? []).Any(id => !r.Deck.Any(c => c.InstanceId == id))) throw new ProtocolException("invalid_reset", "initial_hand must contain unique instance_id values from deck.");
        if (r.InitialDrawPile is not null)
        {
            string[] realized = (r.InitialHand ?? []).Concat(r.InitialDrawPile).ToArray();
            if (realized.Distinct(StringComparer.Ordinal).Count() != realized.Length || realized.Length != r.Deck.Count || realized.Any(id => !r.Deck.Any(card => card.InstanceId == id)))
                throw new ProtocolException("invalid_reset", "initial_hand and initial_draw_pile must partition the supplied deck by stable instance_id.");
        }
        foreach (CardSpec c in r.Deck) if (c.Upgrades < 0 || c.Enchantment?.Amount < 0) throw new ProtocolException("invalid_reset", "Card upgrades and enchantment amounts cannot be negative.");
        if ((r.Potions ?? []).Any(x => x.Slot < 0 || x.Slot >= 5) || (r.Potions ?? []).Select(x => x.Slot).Distinct().Count() != (r.Potions?.Count ?? 0)) throw new ProtocolException("invalid_reset", "Potion slots must be unique non-negative values.");
        if ((r.Enemies ?? []).Any(x => string.IsNullOrWhiteSpace(x.ModelId) || x.MaxHp < 1 || x.CurrentHp < 1 || x.CurrentHp > x.MaxHp)) throw new ProtocolException("invalid_reset", "Enemy identities and HP must describe live native creatures.");
        foreach (PotionSpec potion in r.Potions ?? []) if (potion.NativeState?.Count > 0) throw new ProtocolException("unsupported_reset_field", "Potion native state is not supported.");
    }

    private void InstallSaveMock()
    {
        object save = RuntimeHelpers.GetUninitializedObject(T("MegaCrit.Sts2.Core.Saves.SaveManager"));
        object sm = RuntimeHelpers.GetUninitializedObject(T("MegaCrit.Sts2.Core.Saves.Managers.SettingsSaveManager")), settings = ReflectionTools.Create(T("MegaCrit.Sts2.Core.Saves.SettingsSave")); ReflectionTools.Set(settings, "Language", "eng"); ReflectionTools.Set(sm, "Settings", settings); ReflectionTools.Set(save, "_settingsSaveManager", sm);
        object pm = RuntimeHelpers.GetUninitializedObject(T("MegaCrit.Sts2.Core.Saves.Managers.PrefsSaveManager")), prefs = ReflectionTools.Create(T("MegaCrit.Sts2.Core.Saves.PrefsSave")); ReflectionTools.Set(prefs, "FastMode", Enum.Parse(T("MegaCrit.Sts2.Core.Settings.FastModeType"), "Instant")); ReflectionTools.Set(pm, "Prefs", prefs); ReflectionTools.Set(save, "_prefsSaveManager", pm);
        object gm = RuntimeHelpers.GetUninitializedObject(T("MegaCrit.Sts2.Core.Saves.Managers.ProgressSaveManager")); ReflectionTools.Set(gm, "Progress", ReflectionTools.InvokeStatic(T("MegaCrit.Sts2.Core.Saves.ProgressState"), "CreateDefault")); ReflectionTools.Set(save, "_progressSaveManager", gm);
        object migrations = Activator.CreateInstance(T("MegaCrit.Sts2.Core.Saves.Migrations.MigrationManager"), [null])!; ReflectionTools.Set(save, "_migrationManager", migrations);
        ReflectionTools.InvokeStatic(T("MegaCrit.Sts2.Core.Saves.SaveManager"), "MockInstanceForTesting", save);
    }
    private void RebindRunChoiceSynchronizer(object runManager)
    {
        object? previous = ReflectionTools.Get(runManager, "PlayerChoiceSynchronizer");
        if (previous is not null) ReflectionTools.Invoke(previous, "Dispose");
        object replacement = ReflectionTools.Create(T("MegaCrit.Sts2.Core.GameActions.Multiplayer.PlayerChoiceSynchronizer"), ReflectionTools.Get(runManager, "NetService"), _run!);
        ReflectionTools.Set(runManager, "PlayerChoiceSynchronizer", replacement);
    }
    private void InstallSeam()
    {
        Assembly ha = _context.LoadDependency("0Harmony.dll"); Type ht = ha.GetType("HarmonyLib.Harmony", true)!, hmt = ha.GetType("HarmonyLib.HarmonyMethod", true)!; object harmony = Activator.CreateInstance(ht, "sts2.native-sim.persistent")!; MethodInfo patch = ht.GetMethods().Single(x => x.Name == "Patch" && x.GetParameters().Length == 5);
        void P(MethodInfo m, string n) => patch.Invoke(harmony, [m, Activator.CreateInstance(hmt, typeof(PersistentNativeCombatEnvironment).GetMethod(n, BindingFlags.NonPublic | BindingFlags.Static)!), null, null, null]);
        void Po(MethodInfo m, string n) => patch.Invoke(harmony, [m, null, Activator.CreateInstance(hmt, typeof(PersistentNativeCombatEnvironment).GetMethod(n, BindingFlags.NonPublic | BindingFlags.Static)!), null, null]);
        P(T("MegaCrit.Sts2.Core.Commands.CreatureCmd").GetMethod("TriggerAnim", BindingFlags.Public | BindingFlags.Static)!, nameof(SkipTask));
        foreach (MethodInfo m in T("MegaCrit.Sts2.Core.Commands.SfxCmd").GetMethods(BindingFlags.Public | BindingFlags.Static | BindingFlags.DeclaredOnly)) { if (m.ReturnType != typeof(void)) throw new InvalidOperationException($"State-bearing SfxCmd: {m}"); P(m, nameof(SkipVoid)); }
        foreach (MethodInfo m in T("MegaCrit.Sts2.Core.Commands.ThinkCmd").GetMethods(BindingFlags.Public | BindingFlags.Static | BindingFlags.DeclaredOnly)) P(m, nameof(SkipVoid));
        foreach (MethodInfo m in T("MegaCrit.Sts2.Core.Logging.Log").GetMethods(BindingFlags.Public | BindingFlags.Static | BindingFlags.DeclaredOnly).Where(x => x.Name == "Info")) P(m, nameof(SkipVoid));
        P(T("MegaCrit.Sts2.Core.Models.CardModel").GetMethod("PlayPowerCardFlyVfx", BindingFlags.NonPublic | BindingFlags.Instance)!, nameof(SkipTask));
        Type q = T("MegaCrit.Sts2.Core.Nodes.Combat.NCardPlayQueue"); foreach (MethodInfo m in q.GetMethods(BindingFlags.Public | BindingFlags.Instance | BindingFlags.DeclaredOnly).Where(x => x.Name is "UpdateCardBeforeExecution" or "RemoveCardFromQueueForCancellation" or "RemoveCardFromQueueForExecution")) P(m, nameof(SkipVoid));
        Type pile = T("MegaCrit.Sts2.Core.Commands.CardPileCmd"); foreach (MethodInfo m in pile.GetMethods(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static | BindingFlags.DeclaredOnly).Where(x => x.Name is "Add" or "RemoveFromCombat").Where(x => x.GetParameters().Any(p => p.Name == "skipVisuals"))) P(m, nameof(ForceSkipVisuals));
        foreach (MethodInfo m in pile.GetMethods(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static | BindingFlags.DeclaredOnly).Where(x => x.Name == "RemoveFromDeck" && x.GetParameters().Any(p => p.Name == "showPreview"))) P(m, nameof(ForceHidePreview));
        P(pile.GetMethod("AddDuringManualCardPlay", BindingFlags.Public | BindingFlags.Static)!, nameof(HeadlessManualCardPlay));
        P(T("MegaCrit.Sts2.Core.Commands.CardCmd").GetMethod("PreviewInternal", BindingFlags.NonPublic | BindingFlags.Static)!, nameof(SkipObject));
        P(T("MegaCrit.Sts2.Core.Commands.ForgeCmd").GetMethod("PreviewSovereignBlade", BindingFlags.NonPublic | BindingFlags.Static)!, nameof(SkipVoid));
        P(T("MegaCrit.Sts2.Core.Commands.TalkCmd").GetMethod("Play", BindingFlags.Public | BindingFlags.Static)!, nameof(SkipObject));
        P(T("MegaCrit.Sts2.Core.Nodes.Screens.CardSelection.NCardRewardSelectionScreen").GetMethod("ShowScreen", BindingFlags.Public | BindingFlags.Static)!, nameof(SkipObject));
        P(T("MegaCrit.Sts2.Core.Commands.CardSelectCmd").GetMethod("FromChooseABundleScreen", BindingFlags.Public | BindingFlags.Static)!, nameof(CaptureBundleChoice));
        P(T("MegaCrit.Sts2.Core.Commands.RelicSelectCmd").GetMethod("FromChooseARelicScreen", BindingFlags.Public | BindingFlags.Static)!, nameof(CaptureRelicChoice));
        P(T("MegaCrit.Sts2.Core.Nodes.Screens.NRewardsScreen").GetMethod("ShowScreen", BindingFlags.Public | BindingFlags.Static)!, nameof(CaptureRewardsScreen));
        Type runManagerType = T("MegaCrit.Sts2.Core.Runs.RunManager");
        P(runManagerType.GetMethod("FadeIn", BindingFlags.NonPublic | BindingFlags.Instance)!, nameof(SkipTask));
        P(runManagerType.GetMethod("ClearScreens", BindingFlags.NonPublic | BindingFlags.Instance)!, nameof(SkipVoid));
        P(runManagerType.GetMethod("WriteReplay", BindingFlags.Public | BindingFlags.Instance)!, nameof(SkipVoid));
        P(runManagerType.GetMethod("EnterRoomWithoutExitingCurrentRoom", BindingFlags.Public | BindingFlags.Instance)!, nameof(ForceNoFade));
        foreach (MethodInfo method in T("MegaCrit.Sts2.Core.Assets.PreloadManager").GetMethods(BindingFlags.Public | BindingFlags.Static | BindingFlags.DeclaredOnly).Where(method => method.Name.StartsWith("LoadRoom", StringComparison.Ordinal) || method.Name == "LoadActAssets"))
        {
            if (method.ReturnType != typeof(Task)) throw new InvalidOperationException($"State-bearing room preload method: {method}");
            P(method, nameof(SkipTask));
        }
        P(T("MegaCrit.Sts2.Core.Combat.CombatManager").GetMethod("AfterCombatRoomLoaded", BindingFlags.Public | BindingFlags.Instance)!, nameof(SkipVoid));
        P(T("MegaCrit.Sts2.Core.Nodes.Ftue.NCombatRulesFtue").GetMethod("Create", BindingFlags.Public | BindingFlags.Static)!, nameof(SkipObject));
        Type nEventRoom = T("MegaCrit.Sts2.Core.Nodes.Rooms.NEventRoom");
        P(nEventRoom.GetMethod("Create", BindingFlags.Public | BindingFlags.Static)!, nameof(SkipObject));
        P(nEventRoom.GetProperty("Instance", BindingFlags.Public | BindingFlags.Static)!.GetMethod!, nameof(SkipObject));
        P(T("MegaCrit.Sts2.Core.Nodes.Rooms.NRestSiteRoom").GetMethod("Create", BindingFlags.Public | BindingFlags.Static)!, nameof(SkipObject));
        P(T("MegaCrit.Sts2.Core.Nodes.Rooms.NTreasureRoom").GetMethod("Create", BindingFlags.Public | BindingFlags.Static)!, nameof(SkipObject));
        P(T("MegaCrit.Sts2.Core.Nodes.Rooms.NMerchantRoom").GetMethod("Create", BindingFlags.Public | BindingFlags.Static)!, nameof(SkipObject));
        Type nGame = T("MegaCrit.Sts2.Core.Nodes.NGame");
        P(nGame.GetProperty("Instance", BindingFlags.Public | BindingFlags.Static)!.GetMethod!, nameof(ReturnScopedPresentationObject));
        foreach (MethodInfo method in nGame.GetMethods(BindingFlags.Public | BindingFlags.Instance | BindingFlags.DeclaredOnly).Where(method => method.Name is "ScreenRumble" or "ScreenShake" or "ScreenShakeTrauma"))
        {
            if (method.ReturnType != typeof(void)) throw new InvalidOperationException($"State-bearing screen-shake method: {method}");
            P(method, nameof(SkipVoid));
        }
        Type nRun = T("MegaCrit.Sts2.Core.Nodes.NRun");
        P(nRun.GetProperty("Instance", BindingFlags.Public | BindingFlags.Static)!.GetMethod!, nameof(ReturnGameOverNRunOrNull));
        P(nRun.GetProperty("RunMusicController", BindingFlags.Public | BindingFlags.Instance)!.GetMethod!, nameof(ReturnUninitializedPresentationObject));
        P(nRun.GetMethod("ShowGameOverScreen", BindingFlags.Public | BindingFlags.Instance)!, nameof(SkipVoid));
        P(T("MegaCrit.Sts2.Core.Nodes.Audio.NRunMusicController").GetMethod("StopMusic", BindingFlags.Public | BindingFlags.Instance)!, nameof(SkipVoid));
        Type audioManager = T("MegaCrit.Sts2.Core.Nodes.Audio.NAudioManager");
        P(audioManager.GetProperty("Instance", BindingFlags.Public | BindingFlags.Static)!.GetMethod!, nameof(ReturnGameOverPresentationObject));
        P(audioManager.GetMethod("PlayMusic", BindingFlags.Public | BindingFlags.Instance)!, nameof(SkipVoid));
        Type debugAudio = T("MegaCrit.Sts2.Core.Audio.Debug.NDebugAudioManager");
        P(debugAudio.GetProperty("Instance", BindingFlags.Public | BindingFlags.Static)!.GetMethod!, nameof(ReturnScopedPresentationObject));
        P(debugAudio.GetMethod("Play", BindingFlags.Public | BindingFlags.Instance)!, nameof(SkipInt));
        P(debugAudio.GetMethod("Stop", BindingFlags.Public | BindingFlags.Instance)!, nameof(SkipVoid));
        Type activeScreen = T("MegaCrit.Sts2.Core.Nodes.Screens.ScreenContext.ActiveScreenContext");
        P(activeScreen.GetProperty("Instance", BindingFlags.Public | BindingFlags.Static)!.GetMethod!, nameof(ReturnScopedPresentationObject));
        P(activeScreen.GetMethod("Update", BindingFlags.Public | BindingFlags.Instance)!, nameof(SkipVoid));
        foreach (MethodInfo method in T("MegaCrit.Sts2.Core.Context.LocalContext").GetMethods(BindingFlags.Public | BindingFlags.Static | BindingFlags.DeclaredOnly).Where(method => method.Name == "IsMe")) P(method, nameof(SuppressTrialPresentationContext));
        Po(T("MegaCrit.Sts2.Core.Helpers.TaskHelper").GetMethod("RunSafely", BindingFlags.Public | BindingFlags.Static)!, nameof(CaptureScopedScheduledTask));
        foreach (MethodInfo m in T("MegaCrit.Sts2.Core.Multiplayer.Replay.CombatReplayWriter").GetMethods(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.DeclaredOnly).Where(x => x.Name == "RecordPlayerChoice"))
        {
            if (m.ReturnType != typeof(void)) throw new InvalidOperationException($"State-bearing replay recording method: {m}");
            P(m, nameof(SkipVoid));
        }
        P(T("MegaCrit.Sts2.Core.TestSupport.TestMode").GetProperty("IsOff", BindingFlags.Public | BindingFlags.Static)!.GetMethod!, nameof(SuppressScopedPresentationGuard));
        P(T("MegaCrit.Sts2.Core.TestSupport.TestMode").GetProperty("IsOn", BindingFlags.Public | BindingFlags.Static)!.GetMethod!, nameof(EnableTransformPresentationGuard));
        // Native combat completion normally persists the run and updates profile progress. A
        // headless isolated combat has neither backing store, and must never touch the user's
        // installed-game saves. These are persistence side effects; all combat state transitions,
        // hooks, rewards, and room completion continue through the shipped native implementation.
        Type saveManager = T("MegaCrit.Sts2.Core.Saves.SaveManager");
        P(saveManager.GetMethods(BindingFlags.Public | BindingFlags.Instance | BindingFlags.DeclaredOnly).Single(x => x.Name == "SaveRun" && x.GetParameters().Length == 2), nameof(SkipTask));
        P(saveManager.GetMethod("UpdateProgressAfterCombatWon", BindingFlags.Public | BindingFlags.Instance)!, nameof(SkipVoid));
        P(saveManager.GetMethod("SaveProgressFile", BindingFlags.Public | BindingFlags.Instance)!, nameof(SkipVoid));
        P(T("MegaCrit.Sts2.Core.Models.Monsters.DecimillipedeSegment").GetMethod("AnimSegmentsAttack", BindingFlags.NonPublic | BindingFlags.Instance)!, nameof(SkipTask));
        // SandpitPower.AfterApplied, AfterPowerAmountChanged, and UpdateCreaturePositions are
        // gameplay state-bearing lifecycle hooks (they reposition creatures and trigger native
        // power effects). They must not be suppressed in the persistent environment.
        // Crusher.BeforeDeath and Rocket.BeforeDeath / AfterCurrentHpChanged are shipped
        // combat-outcome hooks; removing them would change the native death/reward path.
        // SoulNexus.AfterDeath is a state transition hook.
        // The Background property accessors on Crusher and Rocket return a presentation-only
        // scene node that is never written to game state; those are safe to stub.
        Type crusher = T("MegaCrit.Sts2.Core.Models.Monsters.Crusher");
        P(crusher.GetProperty("Background", BindingFlags.NonPublic | BindingFlags.Instance)!.GetMethod!, nameof(ReturnUninitializedPresentationObject));
        Type rocket = T("MegaCrit.Sts2.Core.Models.Monsters.Rocket");
        P(rocket.GetProperty("Background", BindingFlags.NonPublic | BindingFlags.Instance)!.GetMethod!, nameof(ReturnUninitializedPresentationObject));
        Type crabBackground = T("MegaCrit.Sts2.Core.Nodes.Vfx.Backgrounds.NKaiserCrabBossBackground");
        foreach (MethodInfo m in crabBackground.GetMethods(BindingFlags.Public | BindingFlags.Instance | BindingFlags.DeclaredOnly).Where(x => x.Name.StartsWith("Play", StringComparison.Ordinal)))
        {
            if (m.ReturnType == typeof(Task)) P(m, nameof(SkipTask));
            else if (m.ReturnType == typeof(void)) P(m, nameof(SkipVoid));
            else throw new InvalidOperationException($"State-bearing Kaiser Crab presentation method: {m}");
        }
        Type? canvasItem = crabBackground.BaseType;
        while (canvasItem?.FullName != "Godot.CanvasItem") canvasItem = canvasItem?.BaseType;
        P((canvasItem ?? throw new MissingMemberException("Godot.CanvasItem base type")).GetMethod("SetVisible", BindingFlags.Public | BindingFlags.Instance)!, nameof(SkipVoid));
        foreach (MethodInfo m in T("MegaCrit.Sts2.Core.Nodes.Vfx.NDamageNumVfx").GetMethods(BindingFlags.Public | BindingFlags.Static | BindingFlags.DeclaredOnly).Where(x => x.Name == "Create")) P(m, nameof(SkipObject));
        foreach (MethodInfo m in T("MegaCrit.Sts2.Core.Nodes.Vfx.NMonsterDeathVfx").GetMethods(BindingFlags.Public | BindingFlags.Static | BindingFlags.DeclaredOnly).Where(x => x.Name == "Create")) P(m, nameof(SkipObject));
        P(T("MegaCrit.Sts2.Core.Nodes.Vfx.PlayerHurtVignetteHelper").GetMethod("Play", BindingFlags.Public | BindingFlags.Static)!, nameof(SkipVoid));
        P(T("MegaCrit.Sts2.Core.Nodes.Vfx.NItemThrowVfx").GetMethods(BindingFlags.Public | BindingFlags.Static | BindingFlags.DeclaredOnly).Single(x => x.Name == "Create"), nameof(SkipObject));
        foreach (MethodInfo m in T("MegaCrit.Sts2.Core.Nodes.Vfx.NCardEnchantVfx").GetMethods(BindingFlags.Public | BindingFlags.Static | BindingFlags.DeclaredOnly).Where(x => x.Name == "Create")) P(m, nameof(SkipObject));
        foreach (MethodInfo m in T("MegaCrit.Sts2.Core.Nodes.Vfx.NStunnedVfx").GetMethods(BindingFlags.Public | BindingFlags.Static | BindingFlags.DeclaredOnly).Where(x => x.Name == "Create")) P(m, nameof(SkipObject));
        foreach (string typeName in new[] {
            "MegaCrit.Sts2.Core.Nodes.Vfx.NGroundFireVfx",
            "MegaCrit.Sts2.Core.Nodes.Vfx.NLargeMagicMissileVfx",
            "MegaCrit.Sts2.Core.Nodes.Vfx.NRollingBoulderVfx",
            "MegaCrit.Sts2.Core.Nodes.Vfx.NSmokyVignetteVfx",
        })
            foreach (MethodInfo m in T(typeName).GetMethods(BindingFlags.Public | BindingFlags.Static | BindingFlags.DeclaredOnly).Where(x => x.Name == "Create")) P(m, nameof(SkipObject));
    }
    private static void InitEvents(object manager)
    {
        for (Type? t = manager.GetType(); t is not null; t = t.BaseType) foreach (FieldInfo f in t.GetFields(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.DeclaredOnly)) { if (f.GetValue(manager) is not null || !f.FieldType.IsGenericType) continue; Type d = f.FieldType.GetGenericTypeDefinition(); if (d != typeof(Action<>) && d != typeof(Action<,>)) continue; MethodInfo invoke = f.FieldType.GetMethod("Invoke")!; ParameterExpression[] p = invoke.GetParameters().Select(x => Expression.Parameter(x.ParameterType, x.Name)).ToArray(); f.SetValue(manager, Expression.Lambda(f.FieldType, Expression.Empty(), p).Compile()); }
    }
    private static bool SkipTask(ref Task __result) { __result = Task.CompletedTask; return false; }
    private static bool SkipVoid() => false;
    private static bool SkipObject(ref object? __result) { __result = null; return false; }
    private static bool SkipInt(ref int __result) { __result = 0; return false; }
    private static bool CaptureRewardsScreen(object __0, ref object? __result)
    {
        PersistentNativeCombatEnvironment environment = _activeEnvironment ?? throw new InvalidOperationException("No active native environment can coordinate rewards.");
        if (environment._pendingRewardsSet is not null) throw new ProtocolException("nested_reward_collision", "A second native reward set began before the first was resolved.");
        environment._pendingRewardsSet = __0;
        environment._choiceBegun?.TrySetResult();
        __result = null;
        return false;
    }
    private static bool CaptureBundleChoice(object __1, MethodBase __originalMethod, ref object? __result)
    {
        PersistentNativeCombatEnvironment environment = _activeEnvironment ?? throw new InvalidOperationException("No active native environment can coordinate bundle choices.");
        if (environment._pendingChoice is not null) throw new ProtocolException("nested_choice_collision", "A second native choice began before the first was resolved.");
        object[] bundles = ReflectionTools.Enumerate(__1).Where(bundle => bundle is not null).Select(bundle => bundle!).ToArray();
        if (bundles.Length == 0) throw new ProtocolException("invalid_native_choice", "Native bundle choice exposed zero options.");
        Type resultType = ((MethodInfo)__originalMethod).ReturnType.GetGenericArguments().Single();
        object completion = Activator.CreateInstance(typeof(TaskCompletionSource<>).MakeGenericType(resultType), TaskCreationOptions.RunContinuationsAsynchronously)!;
        string choiceId = $"option-choice-{environment._choiceOrdinal++}";
        string[] optionIds = bundles.Select((_, index) => $"{choiceId}-option-{index}").ToArray();
        object[] snapshots = bundles.Select((bundle, index) => (object)new
        {
            option_id = optionIds[index],
            cards = ReflectionTools.Enumerate(bundle).Where(card => card is not null).Select(card => new { model_id = Entry(card!) }).ToArray()
        }).ToArray();
        environment._pendingChoice = new(
            choiceId, "option_choice", "choose_option", optionIds, 1, 1, snapshots,
            selectedIds =>
            {
                ValidateSelection(optionIds, selectedIds, 1, 1);
                object selectedBundle = bundles[Array.IndexOf(optionIds, selectedIds[0])];
                Type cardModel = resultType.IsGenericType
                    ? resultType.GetGenericArguments().Single()
                    : throw new ProtocolException("unsupported_native_choice", $"Native bundle choice returned unsupported result type {resultType}.");
                object typed = List(cardModel, ReflectionTools.Enumerate(selectedBundle).ToArray());
                completion.GetType().GetMethod("SetResult")!.Invoke(completion, [typed]);
            },
            () => TryCancelCompletion(completion));
        environment._choiceBegun?.TrySetResult();
        __result = ReflectionTools.Get(completion, "Task");
        return false;
    }
    private static bool CaptureRelicChoice(object __1, MethodBase __originalMethod, ref object? __result)
    {
        PersistentNativeCombatEnvironment environment = _activeEnvironment ?? throw new InvalidOperationException("No active native environment can coordinate relic choices.");
        if (environment._pendingChoice is not null) throw new ProtocolException("nested_choice_collision", "A second native choice began before the first was resolved.");
        object[] relics = ReflectionTools.Enumerate(__1).Where(relic => relic is not null).Select(relic => relic!).ToArray();
        if (relics.Length == 0) throw new ProtocolException("invalid_native_choice", "Native relic choice exposed zero options.");
        Type resultType = ((MethodInfo)__originalMethod).ReturnType.GetGenericArguments().Single();
        object completion = Activator.CreateInstance(typeof(TaskCompletionSource<>).MakeGenericType(resultType), TaskCreationOptions.RunContinuationsAsynchronously)!;
        string choiceId = $"option-choice-{environment._choiceOrdinal++}";
        string[] optionIds = relics.Select((_, index) => $"{choiceId}-option-{index}").ToArray();
        object[] snapshots = relics.Select((relic, index) => (object)new { option_id = optionIds[index], model_id = Entry(relic) }).ToArray();
        environment._pendingChoice = new(
            choiceId, "option_choice", "choose_option", optionIds, 0, 1, snapshots,
            selectedIds =>
            {
                ValidateSelection(optionIds, selectedIds, 0, 1);
                object? selected = selectedIds.Length == 0 ? null : relics[Array.IndexOf(optionIds, selectedIds[0])];
                completion.GetType().GetMethod("SetResult")!.Invoke(completion, [selected]);
            },
            () => TryCancelCompletion(completion));
        environment._choiceBegun?.TrySetResult();
        __result = ReflectionTools.Get(completion, "Task");
        return false;
    }
    private static bool SuppressTrialPresentationContext(ref bool __result)
    {
        if (!_eventPresentationScope) return true;
        bool isTrialPresentation = new StackTrace().GetFrames().Select(frame => frame.GetMethod())
            .Any(method => method?.DeclaringType?.FullName == "MegaCrit.Sts2.Core.Models.Events.Trial" && method.Name is "Accept" or "AddVfxAnchoredToPortrait");
        if (!isTrialPresentation) return true;
        __result = false;
        return false;
    }
    private static void CaptureScopedScheduledTask(Task task) { if (_eventPresentationScope) _scopedScheduledTask = task; }
    private static bool ReturnUninitializedPresentationObject(MethodBase __originalMethod, ref object? __result)
    {
        __result = RuntimeHelpers.GetUninitializedObject(((MethodInfo)__originalMethod).ReturnType);
        return false;
    }
    private static bool IsGameOverPresentationCall()
    {
        string?[] frames = new StackTrace().GetFrames().Select(frame => frame.GetMethod()?.DeclaringType?.FullName).ToArray();
        if (frames.Any(typeName => typeName?.StartsWith("MegaCrit.Sts2.Core.Commands.CreatureCmd+<KillWithoutCheckingWinCondition>", StringComparison.Ordinal) == true)) return false;
        if (new StackTrace().GetFrames().Any(frame => frame.GetMethod()?.DeclaringType?.FullName == "MegaCrit.Sts2.Core.Runs.RunManager" && frame.GetMethod()?.Name == "ToSave")) return false;
        if (!frames.Any(typeName => typeName?.StartsWith("MegaCrit.Sts2.Core.Commands.CreatureCmd+<Kill>", StringComparison.Ordinal) == true)) return false;
        object? creature = _activeEnvironment?._player is { } player ? ReflectionTools.Get(player, "Creature") : null;
        return creature is not null && Convert.ToBoolean(ReflectionTools.Get(creature, "IsDead"));
    }
    private static bool ReturnGameOverNRunOrNull(MethodBase __originalMethod, ref object? __result)
    {
        __result = IsGameOverPresentationCall() ? RuntimeHelpers.GetUninitializedObject(((MethodInfo)__originalMethod).ReturnType) : null;
        return false;
    }
    private static bool ReturnGameOverPresentationObject(MethodBase __originalMethod, ref object? __result)
    {
        if (!IsGameOverPresentationCall()) return true;
        __result = RuntimeHelpers.GetUninitializedObject(((MethodInfo)__originalMethod).ReturnType);
        return false;
    }
    private static bool ReturnScopedPresentationObject(MethodBase __originalMethod, ref object? __result)
    {
        if (!_eventPresentationScope) return true;
        __result = RuntimeHelpers.GetUninitializedObject(((MethodInfo)__originalMethod).ReturnType);
        return false;
    }
    private static bool SuppressScopedPresentationGuard(ref bool __result)
    {
        // CreatureCmd.Kill has already applied native death hooks and LoseCombat before this
        // guard. Its guarded body is exclusively music, run-history persistence, and the game-over
        // screen, none of which may run in an isolated worker.
        if (IsGameOverPresentationCall())
        {
            __result = false;
            return false;
        }
        string[] guardedCallers =
        {
            "MegaCrit.Sts2.Core.Commands.OstyCmd+<Summon>",
            "MegaCrit.Sts2.Core.Models.Cards.BouncingFlask+<OnPlay>",
            "MegaCrit.Sts2.Core.Models.Cards.Nightmare+<OnPlay>",
            "MegaCrit.Sts2.Core.Models.Cards.HandOfGreed+<OnPlay>",
            "MegaCrit.Sts2.Core.Commands.CardCmd+<Transform>",
            "MegaCrit.Sts2.Core.Models.Monsters.BowlbugSilk+<SpitMove>",
            "MegaCrit.Sts2.Core.Models.Monsters.MagiKnight+<MagicBombMove>",
            "MegaCrit.Sts2.Core.Models.Monsters.Guardbot",
            "MegaCrit.Sts2.Core.Models.Monsters.Noisebot",
            "MegaCrit.Sts2.Core.Models.Monsters.Stabbot",
            "MegaCrit.Sts2.Core.Models.Monsters.Zapbot",
            "MegaCrit.Sts2.Core.Models.Monsters.KinFollower+<BoomerangMove>",
            "MegaCrit.Sts2.Core.Models.Powers.SandpitPower+<AfterRemoved>",
            "MegaCrit.Sts2.Core.Models.Powers.InfestedPower+<AfterDeath>"
        };
        bool isPresentationGuard = new StackTrace().GetFrames()
            .Select(frame => frame.GetMethod()?.DeclaringType?.FullName)
            .Any(typeName => typeName is not null && guardedCallers.Any(typeName.StartsWith));
        if (!isPresentationGuard) return true;
        __result = false;
        return false;
    }
    private static bool EnableTransformPresentationGuard(ref bool __result)
    {
        string[] testModeCallers =
        {
            "MegaCrit.Sts2.Core.Commands.CardCmd+<Transform>",
            "MegaCrit.Sts2.Core.Models.Powers.RollingBoulderPower+<AfterPlayerTurnStart>",
            "MegaCrit.Sts2.Core.Multiplayer.Game.OneOffSynchronizer+<DoMerchantCardRemoval>",
        };
        bool isTransform = new StackTrace().GetFrames()
            .Select(frame => frame.GetMethod()?.DeclaringType?.FullName)
            .Any(typeName => typeName is not null && testModeCallers.Any(typeName.StartsWith));
        if (!isTransform) return true;
        __result = true;
        return false;
    }
    private static void ForceSkipVisuals(ref bool skipVisuals) => skipVisuals = true;
    private static void ForceNoFade(ref bool fadeToBlack) => fadeToBlack = false;
    private static void ForceHidePreview(ref bool showPreview) => showPreview = false;
    private static bool HeadlessManualCardPlay(object card, ref Task __result) { Assembly a = card.GetType().Assembly; Type cmd = a.GetType("MegaCrit.Sts2.Core.Commands.CardPileCmd", true)!; __result = (Task)ReflectionTools.InvokeStatic(cmd, "Add", card, Enum.Parse(a.GetType("MegaCrit.Sts2.Core.Entities.Cards.PileType", true)!, "Play"), Enum.Parse(a.GetType("MegaCrit.Sts2.Core.Entities.Cards.CardPilePosition", true)!, "Top"), null, true)!; return false; }
    private void InstallChoiceSelector()
    {
        Type selectorInterface = T("MegaCrit.Sts2.Core.TestSupport.ICardSelector");
        object proxyObject = DispatchProxy.Create(selectorInterface, typeof(NativeChoiceDispatchProxy));
        ((NativeChoiceDispatchProxy)proxyObject).Handler = HandleSelectorCall;
        _selectorScope = (IDisposable)ReflectionTools.InvokeStatic(T("MegaCrit.Sts2.Core.Commands.CardSelectCmd"), "UseSelector", proxyObject)!;
    }

    private object? HandleSelectorCall(MethodInfo method, object?[]? args)
    {
        if (method.Name == "GetSelectedCardReward")
        {
            if (_rewardSelectionIndex is null) throw new ProtocolException("unsupported_choice", "Native reward selection was requested without a coordinated reward action.");
            if (_rewardSelectionIndex < 0) return Activator.CreateInstance(method.ReturnType);
            object[] results = ReflectionTools.Enumerate(args![0]).Where(result => result is not null).Select(result => result!).ToArray();
            object selection = Activator.CreateInstance(method.ReturnType)!;
            if (_rewardSelectionIndex.Value < results.Length)
            {
                ReflectionTools.Set(selection, "card", ReflectionTools.Get(results[_rewardSelectionIndex.Value], "Card"));
                ReflectionTools.Set(selection, "alternative", null);
                return selection;
            }
            if (args.Length > 1 && args[1] is not null)
            {
                object[] alternatives = ReflectionTools.Enumerate(args[1]).Where(alt => alt is not null).Select(alt => alt!).ToArray();
                int altIndex = _rewardSelectionIndex.Value - results.Length;
                if (altIndex >= 0 && altIndex < alternatives.Length)
                {
                    ReflectionTools.Set(selection, "card", null);
                    ReflectionTools.Set(selection, "alternative", alternatives[altIndex]);
                    return selection;
                }
            }
            throw new ProtocolException("invalid_choice", $"Reward option {_rewardSelectionIndex} is outside 0..{results.Length - 1}.");
        }
        if (method.Name != "GetSelectedCards" || args is null)
            throw new ProtocolException("unsupported_choice", $"Unknown native selector method {method}.");
        if (_pendingChoice is not null) throw new ProtocolException("nested_choice_collision", "A second native card choice began before the first was resolved.");
        object[] cards = ReflectionTools.Enumerate(args[0]).Where(x => x is not null).Select(x => x!).ToArray();
        int min = Convert.ToInt32(args[1]), max = Math.Min(Convert.ToInt32(args[2]), cards.Length);
        if (min < 0 || max < min) throw new ProtocolException("invalid_native_choice", $"Native card choice bounds are {min}..{max} for {cards.Length} options.");
        Type resultType = method.ReturnType.GetGenericArguments().Single();
        object completion = Activator.CreateInstance(typeof(TaskCompletionSource<>).MakeGenericType(resultType), TaskCreationOptions.RunContinuationsAsynchronously)!;
        string choiceId = $"card-choice-{_choiceOrdinal++}";
        string[] optionIds = cards.Select((card, index) =>
        {
            if (_cardInstanceIds.TryGetValue(card, out string? existing)) return existing;
            // Generated choice options need a stable selector-local identity, but
            // that identity is not the card's combat-pile identity. The shipped
            // exporter assigns a dynamic-* id only after the selected card enters
            // a pile, so leave generated options out of the persistent map here.
            return $"generated-{choiceId}-{index}-{Entry(card)}";
        }).ToArray();
        object[] snapshots = cards.Select((card, index) => (object)new { option_id = optionIds[index], model_id = Entry(card) }).ToArray();
        string provenance = string.Join(" > ", new StackTrace().GetFrames()
            .Select(frame => frame.GetMethod())
            .Where(candidate => candidate?.DeclaringType?.FullName?.StartsWith("MegaCrit.Sts2", StringComparison.Ordinal) == true)
            .Take(12)
            .Select(candidate => $"{candidate!.DeclaringType!.FullName}.{candidate.Name}"));
        _pendingChoice = new(
            choiceId, "card_choice", "choose_cards", optionIds, min, max, snapshots,
            selectedIds =>
            {
                ValidateSelection(optionIds, selectedIds, min, max);
                object[] selected = selectedIds.Select(id => cards[Array.IndexOf(optionIds, id)]).ToArray();
                Type cardModel = cards[0].GetType().Assembly.GetType("MegaCrit.Sts2.Core.Models.CardModel", true)!;
                object typed = List(cardModel, selected);
                completion.GetType().GetMethod("SetResult")!.Invoke(completion, [typed]);
            },
            () => TryCancelCompletion(completion), provenance);
        _choiceBegun?.TrySetResult();
        return ReflectionTools.Get(completion, "Task");
    }

    private async Task StartTransitionAsync(Func<Task> factory)
    {
        if (_continuationTask is not null) throw new ProtocolException("transition_pending", "A native transition is already suspended.");
        _choiceBegun = new(TaskCreationOptions.RunContinuationsAsynchronously);
        _continuationTask = factory();
        await WaitForDecisionOrCompletionAsync();
    }

    private async Task ResumeChoiceAsync(string[] optionIds)
    {
        PendingNativeChoice choice = _pendingChoice ?? throw new ProtocolException("no_choice", "No native choice is outstanding.");
        Task transition = _continuationTask ?? throw new ProtocolException("no_transition", "Choice has no suspended native transition.");
        _choiceBegun = new(TaskCreationOptions.RunContinuationsAsynchronously);
        _pendingChoice = null;
        choice.Resolve(optionIds);
        _continuationTask = transition;
        await WaitForDecisionOrCompletionAsync();
        if (_pendingRewardSelection is not null && _pendingChoice is null)
        {
            _rewardSelectionIndex = null;
            await FinalizeCustomRewardSelectionAsync();
        }
        if (_pendingRoomRewardIndex is { } rewardIndex && _pendingChoice is null && _roomRewardsSet is not null)
        {
            object? reward = ReflectionTools.Enumerate(ReflectionTools.Get(_roomRewardsSet, "Rewards")).ElementAtOrDefault(rewardIndex);
            if (reward is not null && (bool)ReflectionTools.Get(reward, "SuccessfullySelected")!) _resolvedRoomRewards.Add(rewardIndex);
            _pendingRoomRewardIndex = null;
            _rewardSelectionIndex = null;
        }
        if (_rewardMode && _cardReward is not null && _pendingChoice is null && _continuationTask is null)
        {
            _rewardCompleted = (bool)ReflectionTools.Get(_cardReward, "SuccessfullySelected")!;
            _rewardSelectionIndex = null;
        }
    }

    private async Task WaitForDecisionOrCompletionAsync()
    {
        Task transition = _continuationTask!;
        Task signal = _choiceBegun!.Task;
        Task completed = await Task.WhenAny(transition, signal).ConfigureAwait(false);
        if (completed == transition)
        {
            await transition.ConfigureAwait(false);
            _continuationTask = null;
            _choiceBegun = null;
        }
        else await signal.ConfigureAwait(false);
    }

    private IReadOnlyList<LegalAction> BuildChoiceActions(PendingNativeChoice choice)
    {
        List<LegalAction> actions = [];
        foreach (string[] selection in EnumerateSelections(choice.OptionIds, choice.MinSelect, choice.MaxSelect, 4096))
        {
            string suffix = selection.Length == 0 ? "skip" : string.Join('+', selection.Select(Uri.EscapeDataString));
            actions.Add(new($"{choice.ActionKind}:{choice.ChoiceId}:{suffix}", choice.ActionKind, new Dictionary<string, object?> { ["choice_id"] = choice.ChoiceId, ["option_ids"] = selection }));
        }
        return actions;
    }

    private static IEnumerable<string[]> EnumerateSelections(string[] options, int min, int max, int limit)
    {
        List<string[]> result = []; List<string> current = [];
        void Visit(int index)
        {
            if (result.Count > limit) return;
            if (current.Count >= min && current.Count <= max) result.Add(current.ToArray());
            if (current.Count == max) return;
            for (int i = index; i < options.Length; i++) { current.Add(options[i]); Visit(i + 1); current.RemoveAt(current.Count - 1); }
        }
        Visit(0);
        if (result.Count > limit) throw new ProtocolException("choice_too_large", $"Native choice expands beyond {limit} legal combinations.");
        return result;
    }

    private void QuiesceOutstandingTransition()
    {
        Task? continuation = _continuationTask;
        PendingNativeChoice? choice = _pendingChoice;
        _pendingChoice = null;
        _choiceBegun = null;

        if (continuation is not null && !continuation.IsCompleted)
        {
            choice?.Cancel();
            try
            {
                continuation.Wait(TimeSpan.FromMilliseconds(500));
            }
            catch (Exception)
            {
                // Unwinding/cancellation exceptions are expected when canceling a transition.
            }
            finally
            {
                _continuationTask = null;
            }
        }
        else
        {
            choice?.Cancel();
            _continuationTask = null;
        }
    }

    private void ThrowIfPoisoned()
    {
        if (_isPoisoned)
            throw new ProtocolException("worker_poisoned", "Worker state is corrupted and must be reconstructed. Discard and replace this worker.");
    }

    private static bool IsCancellationException(Exception ex) => ex switch
    {
        // TaskCanceledException is a subtype of OperationCanceledException; covered by the first arm.
        OperationCanceledException => true,
        AggregateException agg => agg.InnerExceptions.All(inner => inner is OperationCanceledException),
        _ => false
    };
    private static void ValidateSelection(string[] optionIds, string[] selectedIds, int min, int max)
    {
        if (selectedIds.Distinct(StringComparer.Ordinal).Count() != selectedIds.Length || selectedIds.Length < min || selectedIds.Length > max || selectedIds.Any(id => !optionIds.Contains(id, StringComparer.Ordinal)))
            throw new ProtocolException("invalid_choice", "Selected option IDs do not satisfy the native choice bounds.");
    }
    private static void TryCancelCompletion(object completion)
        => completion.GetType().GetMethods().First(method => method.Name == "TrySetCanceled" && method.GetParameters().Length == 0).Invoke(completion, null);
    private string GetOrAddCurrentBranch()
    {
        EnsureReset();

        // Passive-observe idempotency fast path: if no action edge is pending
        // (_lastActionId == null) and the worker already has a branch handle that
        // records the current hash, refresh its LRU position and return it directly
        // without computing the SHA-256 payload or spawning a new child node.
        // This guarantees that 1,000 consecutive Observe() or Fork() calls on an
        // unchanged worker leave branch_count strictly at 1.
        if (_lastActionId is null && _currentBranchHandle is not null &&
            _branches.TryGetValue(_currentBranchHandle, out Branch? resident) &&
            StringComparer.Ordinal.Equals(resident.ExpectedHash, _hash))
        {
            _branchOrder.Remove(_currentBranchHandle);
            _branchOrder.AddLast(_currentBranchHandle);
            return _currentBranchHandle;
        }

        string payload = JsonSerializer.Serialize(new
        {
            parent = _currentBranchHandle,
            action_id = _lastActionId,
            expected_hash = _hash,
            reset = _reset,
            kernel = TransitionKernelSnapshot()
        });
        string id = "s:" + Convert.ToHexString(SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(payload)));
        // Fast path: if the current branch handle already maps to exactly this
        // payload (same parent, same action edge, same hash, same kernel), return
        // immediately.  This prevents repeated Observe() or Fork() calls after a
        // Step from spawning redundant child branch entries for the same transition.
        if (_currentBranchHandle == id)
            return id;
        if (_branches.TryGetValue(id, out Branch? existing))
        {
            _branchOrder.Remove(id);
            _branchOrder.AddLast(id);
            _currentBranchHandle = id;
            return id;
        }
        // Store the complete action history on the branch at creation so it is
        // self-contained.  ResolveBranchHistory() reads this directly and is
        // therefore immune to ancestor eviction regardless of LRU order.
        string[] history = _history.ToArray();
        CombatSnapshot? combatSnapshot = CaptureCombatSnapshot();
        _branches[id] = new(
            _currentBranchHandle,
            _lastActionId,
            _hash,
            _reset!,
            history,
            _runMode,
            _mapMode,
            _rewardMode,
            _rewardKind,
            _rewardModelId,
            _restMode,
            _eventMode,
            _eventId,
            _customRewardMode,
            _customRewardKinds,
            _customRewardsLinked,
            combatSnapshot);
        _branchOrder.AddLast(id);
        _currentBranchHandle = id;
        while (_branches.Count > BranchCapacity && _branchOrder.First is { } oldest)
        {
            _branchOrder.RemoveFirst();
            _branches.Remove(oldest.Value);
        }
        return id;
    }

    private static List<string> ResolveBranchHistory(Branch leaf)
    {
        // History is stored directly on the Branch record at creation time, so
        // resolution is an O(1) array copy that is immune to ancestor eviction.
        return [.. leaf.History];
    }

    private CombatSnapshot? CaptureCombatSnapshot()
    {
        if (_runMode || _mapMode || _rewardMode || _restMode || _eventMode || _customRewardMode || _combat is null || _pcs is null || _player is null || _reset is null)
            return null;
        if (_pendingChoice is not null)
            return null;

        try
        {
            object playerCreature = ReflectionTools.Get(_player, "Creature")!;
            int playerHp = Convert.ToInt32(ReflectionTools.Get(playerCreature, "CurrentHp"));
            int playerMaxHp = Convert.ToInt32(ReflectionTools.Get(playerCreature, "MaxHp"));
            int playerBlock = Convert.ToInt32(ReflectionTools.Get(playerCreature, "Block"));
            int gold = Convert.ToInt32(ReflectionTools.Get(_player, "Gold"));
            int energy = Convert.ToInt32(ReflectionTools.Get(_pcs, "Energy"));
            int stars = Convert.ToInt32(ReflectionTools.Get(_pcs, "Stars"));
            int turnNumber = Convert.ToInt32(ReflectionTools.Get(_pcs, "TurnNumber"));
            string phase = ReflectionTools.Get(_pcs, "Phase")!.ToString()!;
            int roundNumber = Convert.ToInt32(ReflectionTools.Get(_combat, "RoundNumber"));
            string currentSide = ReflectionTools.Get(_combat, "CurrentSide")!.ToString()!;
            uint nextCreatureId = Convert.ToUInt32(ReflectionTools.Get(_combat, "_nextCreatureId") ?? 100u);

            // Run RNG counters
            SortedDictionary<string, int> rngCounters = RunRngCounters();

            // Cards in all 5 piles
            List<CardSnapshot> CapturePile(string name)
            {
                object pile = ReflectionTools.Get(_pcs, name)!;
                List<CardSnapshot> list = [];
                foreach (object? card in ReflectionTools.Enumerate(ReflectionTools.Get(pile, "Cards")))
                {
                    if (card is null) continue;
                    string instanceId = GetCardInstanceId(card);
                    string modelId = Entry(card);
                    int upgrades = Convert.ToInt32(ReflectionTools.Get(card, "CurrentUpgradeLevel"));
                    object cost = ReflectionTools.Get(card, "EnergyCost")!;
                    int resolvedCost = Convert.ToInt32(ReflectionTools.Invoke(cost, "GetResolved"));
                    int baseCost = Convert.ToInt32(ReflectionTools.Get(cost, "_base") ?? 0);
                    bool retain = Convert.ToBoolean(ReflectionTools.Get(card, "_hasSingleTurnRetain") ?? false);
                    bool sly = Convert.ToBoolean(ReflectionTools.Get(card, "_hasSingleTurnSly") ?? false);
                    bool exhaust = Convert.ToBoolean(ReflectionTools.Get(card, "_exhaustOnNextPlay") ?? false);
                    string? enchModel = ReflectionTools.Get(card, "Enchantment") is { } ench ? Entry(ench) : null;
                    decimal enchAmount = ReflectionTools.Get(card, "Enchantment") is { } ench2 ? Convert.ToDecimal(ReflectionTools.Get(ench2, "Amount")) : 0m;
                    var savedProps = SavedNativeState(card);
                    list.Add(new CardSnapshot(
                        instanceId,
                        modelId,
                        upgrades,
                        resolvedCost,
                        baseCost,
                        retain,
                        sly,
                        exhaust,
                        enchModel,
                        enchAmount,
                        savedProps
                    ));
                }
                return list;
            }

            // Powers on creature
            List<PowerSnapshot> CapturePowers(object ownerCreature)
            {
                List<PowerSnapshot> list = [];
                foreach (object? p in ReflectionTools.Enumerate(ReflectionTools.Get(ownerCreature, "Powers")))
                {
                    if (p is null) continue;
                    string pId = Entry(p);
                    int amount = Convert.ToInt32(ReflectionTools.Get(p, "Amount"));
                    var saved = SavedNativeState(p);
                    list.Add(new PowerSnapshot(
                        pId,
                        amount,
                        saved
                    ));
                }
                return list;
            }

            // Enemies
            List<EnemySnapshot> enemies = [];
            foreach (object? enemy in ReflectionTools.Enumerate(ReflectionTools.Get(_combat, "Enemies")))
            {
                if (enemy is null) continue;
                uint cid = Convert.ToUInt32(ReflectionTools.Get(enemy, "CombatId"));
                int hp = Convert.ToInt32(ReflectionTools.Get(enemy, "CurrentHp"));
                int maxHp = Convert.ToInt32(ReflectionTools.Get(enemy, "MaxHp"));
                int block = Convert.ToInt32(ReflectionTools.Get(enemy, "Block"));
                string? slot = ReflectionTools.Get(enemy, "SlotName")?.ToString();
                object? monster = ReflectionTools.Get(enemy, "Monster");
                string modelId = monster is null ? Entry(enemy) : Entry(monster);
                var powers = CapturePowers(enemy);

                string? stateId = null;
                bool firstMove = false;
                List<string> stateLog = [];
                int monsterRngCounter = 0;
                uint monsterRngSeed = 0;
                string? nextMoveId = null;

                if (monster is not null)
                {
                    object machine = ReflectionTools.Get(monster, "MoveStateMachine")!;
                    object curState = ReflectionTools.Get(machine, "_currentState")!;
                    stateId = ReflectionTools.Get(curState, "Id")?.ToString();
                    firstMove = Convert.ToBoolean(ReflectionTools.Get(machine, "_performedFirstMove"));
                    foreach (object? st in ReflectionTools.Enumerate(ReflectionTools.Get(machine, "StateLog")))
                    {
                        if (st is not null && ReflectionTools.Get(st, "Id")?.ToString() is { } sid)
                            stateLog.Add(sid);
                    }
                    object? mRng = ReflectionTools.Get(monster, "Rng");
                    if (mRng is not null)
                    {
                        monsterRngCounter = Convert.ToInt32(ReflectionTools.Get(mRng, "Counter"));
                        monsterRngSeed = Convert.ToUInt32(ReflectionTools.Get(mRng, "Seed"));
                    }
                    object? nextMove = ReflectionTools.Get(monster, "NextMove");
                    if (nextMove is not null)
                        nextMoveId = ReflectionTools.Get(nextMove, "Id")?.ToString();
                }

                enemies.Add(new EnemySnapshot(
                    modelId,
                    cid,
                    hp,
                    maxHp,
                    block,
                    slot,
                    powers,
                    stateId,
                    firstMove,
                    stateLog,
                    monsterRngCounter,
                    monsterRngSeed,
                    nextMoveId
                ));
            }

            // Relics
            List<RelicSnapshot> relics = [];
            foreach (object? rel in ReflectionTools.Enumerate(ReflectionTools.Get(_player, "Relics")))
            {
                if (rel is null) continue;
                string rId = Entry(rel);
                int? counter = (bool)ReflectionTools.Get(rel, "ShowCounter")! ? Convert.ToInt32(ReflectionTools.Get(rel, "DisplayAmount")) : null;
                var saved = SavedNativeState(rel);
                relics.Add(new RelicSnapshot(
                    rId,
                    counter,
                    saved
                ));
            }

            // Potions
            List<string?> potions = [];
            foreach (object? pot in ReflectionTools.Enumerate(ReflectionTools.Get(_player, "PotionSlots")))
            {
                potions.Add(pot is null ? null : Entry(pot));
            }

            OrbQueueSnapshot? orbQueueSnapshot = null;
            if (ReflectionTools.Get(_pcs, "OrbQueue") is { } queue)
            {
                int cap = Convert.ToInt32(ReflectionTools.Get(queue, "Capacity"));
                List<OrbSnapshot> orbs = [];
                foreach (object? orb in ReflectionTools.Enumerate(ReflectionTools.Get(queue, "Orbs")))
                {
                    if (orb is null) continue;
                    orbs.Add(new OrbSnapshot(
                        Entry(orb),
                        Convert.ToInt32(ReflectionTools.Get(orb, "PassiveVal")),
                        Convert.ToInt32(ReflectionTools.Get(orb, "EvokeVal")),
                        SavedNativeState(orb)
                    ));
                }
                orbQueueSnapshot = new OrbQueueSnapshot(cap, orbs);
            }

            return new CombatSnapshot(
                playerHp,
                playerMaxHp,
                playerBlock,
                gold,
                energy,
                stars,
                turnNumber,
                phase,
                roundNumber,
                currentSide,
                nextCreatureId,
                _dynamicCardOrdinal,
                new Dictionary<string, int>(rngCounters, StringComparer.Ordinal),
                CapturePile("Hand"),
                CapturePile("DrawPile"),
                CapturePile("DiscardPile"),
                CapturePile("ExhaustPile"),
                CapturePile("PlayPile"),
                CapturePowers(playerCreature),
                enemies,
                relics,
                potions,
                orbQueueSnapshot
            );
        }
        catch (Exception ex)
        {
            _lastSnapshotDebug = $"capture_ex: {ex.Message}";
            return null;
        }
    }

    private bool RestoreCombatSnapshot(CombatSnapshot snap)
    {
        if (_combat is null || _pcs is null || _player is null || _reset is null) return false;

        try
        {
            // 1. Player creature stats
            object playerCreature = ReflectionTools.Get(_player, "Creature")!;
            ReflectionTools.Set(playerCreature, "CurrentHp", snap.PlayerHp);
            ReflectionTools.Set(playerCreature, "MaxHp", snap.PlayerMaxHp);
            ReflectionTools.Set(playerCreature, "Block", snap.PlayerBlock);
            ReflectionTools.Set(_player, "Gold", snap.Gold);

            // 2. PlayerCombatState
            ReflectionTools.Set(_pcs, "Energy", snap.Energy);
            ReflectionTools.Set(_pcs, "Stars", snap.Stars);
            ReflectionTools.Set(_pcs, "TurnNumber", snap.TurnNumber);
            ReflectionTools.Set(_pcs, "Phase", Enum.Parse(T("MegaCrit.Sts2.Core.Combat.PlayerTurnPhase"), snap.Phase));

            // 3. Card instances map
            Dictionary<string, object> existingCards = new(StringComparer.Ordinal);
            foreach ((object c, string id) in _cardInstanceIds)
            {
                existingCards.TryAdd(id, c);
            }

            _cardInstanceIds.Clear();
            _dynamicCardOrdinal = snap.DynamicCardOrdinal;

            // Clear all 5 piles first
            foreach (string pileName in new[] { "Hand", "DrawPile", "DiscardPile", "ExhaustPile", "PlayPile" })
            {
                object pile = ReflectionTools.Get(_pcs, pileName)!;
                ReflectionTools.Invoke(pile, "Clear", true);
            }
            IList combatAllCards = (IList)ReflectionTools.Get(_combat, "_allCards")!;
            combatAllCards.Clear();

            void RestorePile(string pileName, List<CardSnapshot> cardSnaps)
            {
                object pile = ReflectionTools.Get(_pcs, pileName)!;
                foreach (CardSnapshot cs in cardSnaps)
                {
                    object nativeCard;
                    if (existingCards.TryGetValue(cs.InstanceId, out object? cached))
                    {
                        nativeCard = cached;
                    }
                    else
                    {
                        nativeCard = Mutable("AllCards", cs.ModelId);
                    }

                    ReflectionTools.Set(nativeCard, "_owner", _player);
                    combatAllCards.Add(nativeCard);

                    ApplyNativeProperties(nativeCard, cs.SavedProperties);
                    if (cs.EnchantmentModelId is not null)
                    {
                        object enchantment = Mutable("DebugEnchantments", cs.EnchantmentModelId);
                        ReflectionTools.Invoke(nativeCard, "EnchantInternal", enchantment, cs.EnchantmentAmount);
                    }
                    int curUpgrades = Convert.ToInt32(ReflectionTools.Get(nativeCard, "CurrentUpgradeLevel"));
                    for (int u = curUpgrades; u < cs.Upgrades; u++)
                    {
                        ReflectionTools.Invoke(nativeCard, "UpgradeInternal");
                        ReflectionTools.Invoke(nativeCard, "FinalizeUpgradeInternal");
                    }
                    ReflectionTools.Set(nativeCard, "_hasSingleTurnRetain", cs.HasSingleTurnRetain);
                    ReflectionTools.Set(nativeCard, "_hasSingleTurnSly", cs.HasSingleTurnSly);
                    ReflectionTools.Set(nativeCard, "_exhaustOnNextPlay", cs.ExhaustOnNextPlay);

                    object costObj = ReflectionTools.Get(nativeCard, "EnergyCost")!;
                    ReflectionTools.Set(costObj, "_base", cs.BaseEnergyCost);

                    ReflectionTools.Invoke(pile, "AddInternal", nativeCard, -1, true);
                    _cardInstanceIds[nativeCard] = cs.InstanceId;
                }
            }

            RestorePile("Hand", snap.Hand);
            RestorePile("DrawPile", snap.DrawPile);
            RestorePile("DiscardPile", snap.DiscardPile);
            RestorePile("ExhaustPile", snap.ExhaustPile);
            RestorePile("PlayPile", snap.PlayPile);

            // 3b. Rebind NetCombatCardDb to the restored card instances.
            // After pile reconstruction the singleton may hold stale registrations
            // (e.g. from dynamically-generated tokens like Slimed that were created
            // during a previous step but are now reconstructed as new or reused
            // objects).  Mirroring what Reset() does at construction time keeps
            // GetCardId() coherent for BuildActionsRaw / PlayAsync.
            {
                Type playerType = T("MegaCrit.Sts2.Core.Entities.Players.Player");
                object cardDb = ReflectionTools.GetStatic(T("MegaCrit.Sts2.Core.GameActions.Multiplayer.NetCombatCardDb"), "Instance")!;
                ReflectionTools.Invoke(cardDb, "ClearCardsForTesting");
                ReflectionTools.Invoke(cardDb, "StartCombat", List(playerType, [_player]));
            }

            // 4. Player powers
            RestoreCreaturePowers(playerCreature, snap.PlayerPowers);

            // 5. Relics
            foreach (RelicSnapshot rs in snap.Relics)
            {
                object? relic = ReflectionTools.Enumerate(ReflectionTools.Get(_player, "Relics"))
                    .FirstOrDefault(r => r is not null && Entry(r).Equals(rs.ModelId, StringComparison.OrdinalIgnoreCase));
                if (relic is not null)
                {
                    if (rs.Counter is int cnt) ApplyRelicCounter(relic, cnt);
                    ApplyNativeProperties(relic, rs.SavedProperties);
                }
            }

            // 6. Potion slots
            IList potionSlots = (IList)ReflectionTools.Get(_player, "PotionSlots")!;
            for (int i = 0; i < snap.PotionSlots.Count && i < potionSlots.Count; i++)
            {
                string? expectedPot = snap.PotionSlots[i];
                object? curPot = potionSlots[i];
                if (expectedPot is null && curPot is not null)
                {
                    ReflectionTools.Invoke(_player, "DiscardPotionInternal", curPot, true);
                }
                else if (expectedPot is not null && (curPot is null || !Entry(curPot).Equals(expectedPot, StringComparison.OrdinalIgnoreCase)))
                {
                    if (curPot is not null) ReflectionTools.Invoke(_player, "DiscardPotionInternal", curPot, true);
                    ReflectionTools.Invoke(_player, "AddPotionInternal", Mutable("AllPotions", expectedPot), i, true);
                }
            }

            // 7. Orbs
            if (snap.Orbs is not null && ReflectionTools.Get(_pcs, "OrbQueue") is { } queue)
            {
                ReflectionTools.Invoke(queue, "Clear");
                ReflectionTools.Invoke(queue, "AddCapacity", snap.Orbs.Capacity);
                IList orbsList = (IList)ReflectionTools.Get(queue, "_orbs")!;
                orbsList.Clear();
                foreach (OrbSnapshot os in snap.Orbs.Orbs)
                {
                    object orb = Mutable("AllOrbs", os.ModelId);
                    ReflectionTools.Set(orb, "PassiveVal", os.PassiveVal);
                    ReflectionTools.Set(orb, "EvokeVal", os.EvokeVal);
                    ApplyNativeProperties(orb, os.SavedProperties);
                    orbsList.Add(orb);
                }
            }

            // 8. Enemies
            IList combatEnemies = (IList)ReflectionTools.Get(_combat, "_enemies")!;
            combatEnemies.Clear();
            foreach (EnemySnapshot es in snap.Enemies)
            {
                if (!_combatCreaturesById.TryGetValue(es.CombatId, out object? creature))
                {
                    object monster = Mutable("Monsters", es.ModelId);
                    object enemySide = Enum.Parse(T("MegaCrit.Sts2.Core.Combat.CombatSide"), "Enemy", true);
                    creature = ReflectionTools.Invoke(_combat, "CreateCreature", monster, enemySide, es.SlotName)!;
                    ReflectionTools.Set(creature, "CombatId", es.CombatId);
                    _combatCreaturesById[es.CombatId] = creature;
                }

                ReflectionTools.Set(creature, "CombatState", _combat);
                ReflectionTools.Set(creature, "CurrentHp", es.CurrentHp);
                ReflectionTools.Set(creature, "MaxHp", es.MaxHp);
                ReflectionTools.Set(creature, "Block", es.Block);
                RestoreCreaturePowers(creature, es.Powers);

                object? monsterObj = ReflectionTools.Get(creature, "Monster");
                if (monsterObj is not null)
                {
                    object machine = ReflectionTools.Get(monsterObj, "MoveStateMachine")!;
                    object states = ReflectionTools.Get(machine, "States")!;
                    object? State(string sid) => ReflectionTools.Enumerate(states)
                        .Where(p => p is not null && StringComparer.Ordinal.Equals(Convert.ToString(ReflectionTools.Get(p!, "Key")), sid))
                        .Select(p => ReflectionTools.Get(p!, "Value")!)
                        .SingleOrDefault();

                    if (es.CurrentStateId is not null)
                    {
                        object? st = State(es.CurrentStateId);
                        if (st is not null)
                            ReflectionTools.Invoke(machine, "ForceCurrentState", st);
                    }
                    ReflectionTools.Set(machine, "_performedFirstMove", es.PerformedFirstMove);
                    IList stateLog = (IList)ReflectionTools.Get(machine, "StateLog")!;
                    stateLog.Clear();
                    foreach (string sid in es.StateLog)
                    {
                        object? st = State(sid);
                        if (st is not null) stateLog.Add(st);
                    }
                    if (es.MonsterRngSeed != 0)
                    {
                        object newRng = ReflectionTools.Create(T("MegaCrit.Sts2.Core.Random.Rng"), es.MonsterRngSeed, es.MonsterRngCounter);
                        ReflectionTools.Set(monsterObj, "Rng", newRng);
                    }
                    if (es.NextMoveId is not null)
                    {
                        object? moveState = State(es.NextMoveId);
                        if (moveState is not null)
                            ReflectionTools.Invoke(monsterObj, "SetMoveImmediate", moveState, true);
                    }
                }

                combatEnemies.Add(creature);
            }

            // 9. RNG stream counters
            ApplyRngCountersMap(snap.RngCounters);

            // 10. Combat parameters & Manager rebind
            ReflectionTools.Set(_combat, "RoundNumber", snap.RoundNumber);
            ReflectionTools.Set(_combat, "CurrentSide", Enum.Parse(T("MegaCrit.Sts2.Core.Combat.CombatSide"), snap.CurrentSide));
            ReflectionTools.Set(_combat, "_nextCreatureId", snap.NextCreatureId);
            ReflectionTools.Set(_manager!, "_state", _combat);
            ReflectionTools.Set(_manager!, "IsInProgress", true);

            return true;
        }
        catch (Exception ex)
        {
            _lastSnapshotDebug = $"restore_ex: {ex.GetType().Name}: {ex.Message} at {ex.StackTrace}";
            return false;
        }
    }

    private void RestoreCreaturePowers(object creature, List<PowerSnapshot> powerSnaps)
    {
        IList powersList = (IList)ReflectionTools.Get(creature, "_powers")!;
        powersList.Clear();
        foreach (PowerSnapshot ps in powerSnaps)
        {
            object power = Mutable("AllPowers", ps.ModelId);
            ReflectionTools.Set(power, "_amount", ps.Amount);
            ReflectionTools.Set(power, "_owner", creature);
            ApplyNativeProperties(power, ps.SavedProperties);
            powersList.Add(power);
        }
    }

    private static void ApplyNativeProperties(object model, IReadOnlyDictionary<string, object?>? props)
    {
        if (props is null || props.Count == 0) return;
        foreach ((string name, object? value) in props)
        {
            if (value is null) continue;
            PropertyInfo? property = model.GetType().GetProperty(name, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
            if (property is null || !property.CanWrite) continue;
            try
            {
                object converted = property.PropertyType == typeof(int) ? Convert.ToInt32(value)
                    : property.PropertyType == typeof(bool) ? Convert.ToBoolean(value)
                    : property.PropertyType == typeof(string) ? Convert.ToString(value)!
                    : property.PropertyType == typeof(decimal) ? Convert.ToDecimal(value)
                    : property.PropertyType.IsEnum ? Enum.ToObject(property.PropertyType, Convert.ToInt32(value))
                    : value;
                property.SetValue(model, converted);
            }
            catch { }
        }
    }

    private void ApplyRngCountersMap(Dictionary<string, int> countersMap)
    {
        if (countersMap is not { Count: > 0 }) return;
        Type rngType = T("MegaCrit.Sts2.Core.Entities.Rngs.RunRngType");
        object save = ReflectionTools.Create(T("MegaCrit.Sts2.Core.Saves.Runs.SerializableRunRngSet"));
        ReflectionTools.Set(save, "Seed", _reset!.Seed);
        IDictionary counters = (IDictionary)ReflectionTools.Get(save, "Counters")!;
        foreach ((string name, int count) in countersMap)
        {
            string normalized = name.Replace("_", "", StringComparison.Ordinal);
            object? key = Enum.GetValues(rngType).Cast<object>().SingleOrDefault(x => x.ToString()!.Replace("_", "", StringComparison.Ordinal).Equals(normalized, StringComparison.OrdinalIgnoreCase));
            if (key is not null)
                counters.Add(key, count);
        }
        ReflectionTools.Invoke(ReflectionTools.Get(_run!, "Rng")!, "LoadFromSerializable", save);
    }

    public void Dispose()
    {
        try
        {
            QuiesceOutstandingTransition();
        }
        finally
        {
            if (ReferenceEquals(_activeEnvironment, this))
                _activeEnvironment = null;
            _selectorScope?.Dispose();
            _context.Dispose();
        }
    }

    private sealed record CardSnapshot(
        string InstanceId,
        string ModelId,
        int Upgrades,
        int EnergyCost,
        int BaseEnergyCost,
        bool HasSingleTurnRetain,
        bool HasSingleTurnSly,
        bool ExhaustOnNextPlay,
        string? EnchantmentModelId,
        decimal EnchantmentAmount,
        IReadOnlyDictionary<string, object?> SavedProperties);

    private sealed record PowerSnapshot(
        string ModelId,
        int Amount,
        IReadOnlyDictionary<string, object?> SavedProperties);

    private sealed record EnemySnapshot(
        string ModelId,
        uint CombatId,
        int CurrentHp,
        int MaxHp,
        int Block,
        string? SlotName,
        List<PowerSnapshot> Powers,
        string? CurrentStateId,
        bool PerformedFirstMove,
        List<string> StateLog,
        int MonsterRngCounter,
        uint MonsterRngSeed,
        string? NextMoveId);

    private sealed record RelicSnapshot(
        string ModelId,
        int? Counter,
        IReadOnlyDictionary<string, object?> SavedProperties);

    private sealed record OrbSnapshot(
        string ModelId,
        int PassiveVal,
        int EvokeVal,
        IReadOnlyDictionary<string, object?> SavedProperties);

    private sealed record OrbQueueSnapshot(
        int Capacity,
        List<OrbSnapshot> Orbs);

    private sealed record CombatSnapshot(
        int PlayerHp,
        int PlayerMaxHp,
        int PlayerBlock,
        int Gold,
        int Energy,
        int Stars,
        int TurnNumber,
        string Phase,
        int RoundNumber,
        string CurrentSide,
        uint NextCreatureId,
        int DynamicCardOrdinal,
        Dictionary<string, int> RngCounters,
        List<CardSnapshot> Hand,
        List<CardSnapshot> DrawPile,
        List<CardSnapshot> DiscardPile,
        List<CardSnapshot> ExhaustPile,
        List<CardSnapshot> PlayPile,
        List<PowerSnapshot> PlayerPowers,
        List<EnemySnapshot> Enemies,
        List<RelicSnapshot> Relics,
        List<string?> PotionSlots,
        OrbQueueSnapshot? Orbs);

    private sealed record Branch(
        string? ParentHandle,
        string? ActionId,
        string ExpectedHash,
        ResetRequest Reset,
        string[] History,
        bool RunMode,
        bool MapMode,
        bool RewardMode,
        string RewardKind,
        string? RewardModelId,
        bool RestMode,
        bool EventMode,
        string? EventId,
        bool CustomRewardMode,
        string[] CustomRewardKinds,
        bool CustomRewardsLinked,
        CombatSnapshot? CombatSnapshot = null);
    private sealed record PendingRewardSelection(object TopReward, object SelectedReward, Task OfferTask, bool IsLinked);
    private sealed class PendingNativeChoice(
        string choiceId,
        string decisionKind,
        string actionKind,
        string[] optionIds,
        int minSelect,
        int maxSelect,
        object[] optionSnapshots,
        Action<string[]> resolver,
        Action cancel,
        string provenance = "")
    {
        public string ChoiceId { get; } = choiceId;
        public string DecisionKind { get; } = decisionKind;
        public string ActionKind { get; } = actionKind;
        public string[] OptionIds { get; } = optionIds;
        public int MinSelect { get; } = minSelect;
        public int MaxSelect { get; } = maxSelect;
        public object Snapshot() => new { choice_id = ChoiceId, kind = ActionKind, min_select = MinSelect, max_select = MaxSelect, provenance, options = optionSnapshots };
        public void Resolve(string[] selectedIds) => resolver(selectedIds);
        public void Cancel() => cancel();
    }
}

public sealed class ProtocolException(string code, string message, object? details = null) : Exception(message)
{
    public string Code { get; } = code; public object? Details { get; } = details;
}
