using System.Collections;
using System.Diagnostics;
using System.Reflection;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Serialization;
using Godot;
using HarmonyLib;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.GameActions;
using MegaCrit.Sts2.Core.Modding;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Runs;

namespace Sts2.NativeSim.TraceExporter;

[ModInitializer(nameof(Initialize))]
public static class TraceExporterMod
{
    public static void Initialize()
    {
        bool enabled = StringComparer.Ordinal.Equals(System.Environment.GetEnvironmentVariable("STS2_NATIVE_TRACE"), "1")
            || System.Environment.GetCommandLineArgs().Contains("--native-sim-trace", StringComparer.Ordinal);
        if (!enabled) return;
        new Harmony("sts2-native-sim.trace-exporter").PatchAll(typeof(TraceExporterMod).Assembly);
    }
}

[HarmonyPatch(typeof(CombatManager), nameof(CombatManager.StartCombatInternal))]
internal static class CombatStartPatch
{
    private static void Prefix(CombatManager __instance) => NativeTraceExporter.Attach(__instance.DebugOnlyGetState());
}

internal static class NativeTraceExporter
{
    private static readonly JsonSerializerOptions Json = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull
    };
    private static readonly object Gate = new();
    private static readonly Dictionary<CardModel, string> CardIds = new(ReferenceEqualityComparer.Instance);
    private static readonly Dictionary<GameAction, string> PendingActions = new(ReferenceEqualityComparer.Instance);
    private static CombatState? _combat;
    private static Player? _player;
    private static StreamWriter? _writer;
    private static int _sequence;
    private static int _dynamicOrdinal;
    private static bool _waitingForTurn;
    private static bool _unsupported;
    private static object? _build;

    public static void Attach(CombatState? combat)
    {
        if (combat is null || combat.Players.Count != 1) return;
        lock (Gate)
        {
            Close();
            _combat = combat;
            _player = combat.Players[0];
            _sequence = 0;
            _dynamicOrdinal = 0;
            _waitingForTurn = false;
            _unsupported = false;
            CardIds.Clear();
            PendingActions.Clear();
            MapDeckInstances(_player);
            combat.CreaturesChanged += MapCombatCards;
            CombatManager.Instance.TurnStarted += OnTurnStarted;
            RunManager.Instance.ActionExecutor.BeforeActionExecuted += BeforeAction;
            RunManager.Instance.ActionExecutor.AfterActionExecuted += AfterAction;
        }
    }

    private static void OnTurnStarted(CombatState combat)
    {
        lock (Gate)
        {
            if (_unsupported || combat.CurrentSide != CombatSide.Player || _player?.PlayerCombatState?.Phase != PlayerTurnPhase.Play) return;
            if (_writer is null) StartTrace();
            else if (_waitingForTurn) { AppendCheckpoint("end_turn"); _waitingForTurn = false; }
        }
    }

    private static void BeforeAction(GameAction action)
    {
        lock (Gate)
        {
            if (_writer is null || _unsupported) return;
            string? stable = StableAction(action);
            if (stable is not null) PendingActions[action] = stable;
            action.BeforePausedForPlayerChoice -= ChoiceUnsupported;
            action.BeforePausedForPlayerChoice += ChoiceUnsupported;
        }
    }

    private static void AfterAction(GameAction action)
    {
        lock (Gate)
        {
            if (_writer is null || _unsupported || !PendingActions.Remove(action, out string? stable)) return;
            if (action is EndPlayerTurnAction) _waitingForTurn = true;
            else if (action is PlayCardAction or UsePotionAction or DiscardPotionGameAction) AppendCheckpoint(stable);
        }
    }

    private static void ChoiceUnsupported(GameAction action)
    {
        lock (Gate)
        {
            _unsupported = true;
            Write(new { type = "unsupported", sequence = _sequence, reason = "blocking_player_choice", action = action.GetType().Name });
            Close();
        }
    }

    private static void StartTrace()
    {
        if (_combat is null || _player is null) return;
        try
        {
            string directory = Path.Combine(OS.GetUserDataDir(), "native_sim_traces");
            Directory.CreateDirectory(directory);
            string filename = $"combat-{DateTime.UtcNow:yyyyMMdd-HHmmssfff}-{Sanitize(_combat.Encounter?.Id.Entry ?? "unknown")}.jsonl";
            _writer = new StreamWriter(Path.Combine(directory, filename), false, new System.Text.UTF8Encoding(false)) { AutoFlush = true };
            object build = _build ??= BuildFingerprint();
            Write(new
            {
                type = "header", format_version = 1,
                source = StringComparer.Ordinal.Equals(System.Environment.GetEnvironmentVariable("STS2_NATIVE_TRACE_SOURCE"), "simulator_self_smoke")
                    ? "simulator_self_smoke" : "shipped_game",
                comparison = "exact",
                projection = new[]
                {
                    "schema_version", "game_build", "run",
                    "combat.turn/phase/energy/max_energy/stars/creatures(next_move,intents,powers)/piles(full_cards)",
                    "inventory", "outstanding_choice", "decision/legal_actions", "terminal", "victory"
                },
                game_build = build, reset = ResetSnapshot(build)
            });
            AppendCheckpoint(null);
        }
        catch (Exception error)
        {
            GD.PushError($"STS2 native trace exporter could not start: {error}");
            Close();
        }
    }

    private static void AppendCheckpoint(string? actionId)
    {
        if (_writer is null || _combat is null || _player is null) return;
        MapCombatCards();
        Write(new { type = "checkpoint", sequence = _sequence++, action_id = actionId, observation = Observation() });
    }

    private static object ResetSnapshot(object build)
    {
        Player player = _player!;
        List<object> deck = [];
        foreach (CardModel card in player.Deck.Cards)
        {
            deck.Add(new
            {
                instance_id = CardIds[card], model_id = card.Id.Entry, upgrades = card.CurrentUpgradeLevel,
                native_state = SavedState(card),
                enchantment = card.Enchantment is null ? null : new { model_id = card.Enchantment.Id.Entry, amount = card.Enchantment.Amount }
            });
        }
        return new
        {
            game_build = build, seed = CurrentRun.Rng.StringSeed,
            rng_counters = RngCounters(), character = player.Character.Id.Entry,
            ascension = CurrentRun.AscensionLevel, encounter = _combat!.Encounter!.Id.Entry,
            current_hp = player.Creature.CurrentHp, max_hp = player.Creature.MaxHp, gold = player.Gold,
            deck, initial_hand = player.PlayerCombatState!.Hand.Cards.Select(CardId).ToArray(),
            initial_draw_pile = player.PlayerCombatState.DrawPile.Cards.Select(CardId).ToArray(),
            relics = player.Relics.Select(x => new { model_id = x.Id.Entry, counter = x.ShowCounter ? x.DisplayAmount : (int?)null, native_state = SavedState(x) }).ToArray(),
            potions = player.PotionSlots.Select((x, slot) => (potion: x, slot)).Where(x => x.potion is not null)
                .Select(x => new { model_id = x.potion!.Id.Entry, x.slot, native_state = new Dictionary<string, object?>() }).ToArray(),
            invoke_combat_entry_hooks = true,
            capture_orbs = true,
            turn = player.PlayerCombatState.TurnNumber, energy = player.PlayerCombatState.Energy, stars = player.PlayerCombatState.Stars,
            enemies = _combat.Enemies.Select(enemy => new
            {
                model_id = enemy.ModelId.Entry, current_hp = enemy.CurrentHp, max_hp = enemy.MaxHp, block = enemy.Block,
                next_move_id = enemy.Monster?.NextMove.Id,
                move_history = enemy.Monster?.MoveStateMachine?.StateLog.Select(state => state.Id).ToArray()
            }).ToArray()
        };
    }

    private static object Observation()
    {
        Player player = _player!;
        bool playerAlive = player.Creature.IsAlive;
        bool enemyAlive = _combat!.Enemies.Any(x => x.IsAlive);
        object[] legalActions = LegalActions();
        bool terminal = !playerAlive || !enemyAlive;
        Dictionary<string, object?> combat = new()
        {
            ["turn"] = player.PlayerCombatState!.TurnNumber,
            ["phase"] = player.PlayerCombatState.Phase.ToString(),
            ["energy"] = player.PlayerCombatState.Energy,
            ["max_energy"] = player.PlayerCombatState.MaxEnergy,
            ["stars"] = player.PlayerCombatState.Stars,
            ["creatures"] = _combat.Creatures.Select(CreatureSnapshot).ToArray(),
            ["piles"] = new[] { player.PlayerCombatState.Hand, player.PlayerCombatState.DrawPile, player.PlayerCombatState.DiscardPile, player.PlayerCombatState.ExhaustPile, player.PlayerCombatState.PlayPile }.Select(PileSnapshot).ToArray(),
            ["orbs"] = new
            {
                capacity = player.PlayerCombatState.OrbQueue.Capacity,
                entries = player.PlayerCombatState.OrbQueue.Orbs.Select(orb => new
                {
                    model_id = orb.Id.Entry, passive = orb.PassiveVal, evoke = orb.EvokeVal, native_state = SavedState(orb)
                }).ToArray()
            }
        };
        return new
        {
            schema_version = 2,
            game_build = _build ??= BuildFingerprint(),
            run = new { seed = CurrentRun.Rng.StringSeed, ascension = CurrentRun.AscensionLevel, gold = player.Gold, rng_counters = RngCounters() },
            combat,
            inventory = new
            {
                relics = player.Relics.Select(x => new { model_id = x.Id.Entry, counter = x.ShowCounter ? x.DisplayAmount : (int?)null, native_state = SavedState(x) }).ToArray(),
                potions = player.PotionSlots.Select((x, slot) => x is null ? null : new { slot, model_id = x.Id.Entry }).ToArray()
            },
            outstanding_choice = (object?)null,
            decision = new { kind = terminal ? "terminal" : "combat_action", legal_actions = legalActions },
            terminal,
            victory = playerAlive && !enemyAlive
        };
    }

    private static object PileSnapshot(CardPile pile) => new
    {
        name = pile.Type switch { PileType.Hand => "Hand", PileType.Draw => "DrawPile", PileType.Discard => "DiscardPile", PileType.Exhaust => "ExhaustPile", PileType.Play => "PlayPile", _ => pile.Type.ToString() },
        type = pile.Type.ToString(),
        cards = pile.Cards.Select(x => new
        {
            instance_id = CardId(x), net_id = NetCardId(x), model_id = x.Id.Entry,
            card_type = x.Type.ToString(), target_type = x.TargetType.ToString(),
            energy_cost = x.EnergyCost.GetResolved(), costs_x = x.EnergyCost.CostsX, upgrades = x.CurrentUpgradeLevel,
            enchantment = x.Enchantment is null ? null : new { model_id = x.Enchantment.Id.Entry, amount = x.Enchantment.Amount },
            native_state = SavedState(x)
        }).ToArray()
    };

    private static object CreatureSnapshot(Creature creature)
    {
        object? monster = GetMember(creature, "Monster");
        object? move = monster is null ? null : GetMember(monster, "NextMove");
        object[] intents = move is null ? [] : Enumerate(GetMember(move, "Intents")).Select(x => IntentSnapshot(x, creature)).ToArray();
        object[] powers = Enumerate(GetMember(creature, "Powers"))
            .Select(x => new { model_id = ModelEntry(x), amount = GetMember(x, "Amount") }).ToArray();
        return new
        {
            combat_id = creature.CombatId, model_id = creature.ModelId.Entry, side = creature.Side.ToString(),
            hp = creature.CurrentHp, max_hp = creature.MaxHp, block = creature.Block, alive = creature.IsAlive,
            next_move = move is null ? null : new { id = GetMember(move, "Id"), intents }, powers
        };
    }

    private static object IntentSnapshot(object intent, Creature owner)
    {
        object? damage = null;
        object? repeats = null;
        if (Inherits(intent.GetType(), "MegaCrit.Sts2.Core.MonsterMoves.Intents.AttackIntent"))
        {
            damage = Invoke(intent, "GetSingleDamage", GetMember(GetMember(owner, "CombatState")!, "Allies"), owner);
            repeats = GetMember(intent, "Repeats");
        }
        return new
        {
            intent_type = GetMember(intent, "IntentType")?.ToString(),
            implementation = intent.GetType().Name,
            damage,
            repeats
        };
    }

    private static object[] LegalActions()
    {
        if (_combat is null || _player?.PlayerCombatState is not { Phase: PlayerTurnPhase.Play } pcs
            || !_player.Creature.IsAlive || !_combat.Enemies.Any(x => x.IsAlive)) return [];
        List<object> result = [];
        HashSet<string> targeted = new(StringComparer.Ordinal) { "AnyEnemy", "AnyAlly" };
        foreach (CardModel card in pcs.Hand.Cards)
        {
            if (!card.CanPlay()) continue;
            uint netId = NetCardId(card);
            string instanceId = CardId(card);
            string stableId = Uri.EscapeDataString(instanceId);
            if (!targeted.Contains(card.TargetType.ToString()))
                result.Add(Action($"play:{stableId}:none", "play_card", new Dictionary<string, object?> { ["instance_id"] = instanceId, ["card_id"] = netId, ["target_id"] = null }));
            else foreach (Creature target in _combat.Creatures)
            {
                if (!card.IsValidTarget(target)) continue;
                uint targetId = Convert.ToUInt32(target.CombatId);
                result.Add(Action($"play:{stableId}:target:{targetId}", "play_card", new Dictionary<string, object?> { ["instance_id"] = instanceId, ["card_id"] = netId, ["target_id"] = targetId }));
            }
        }
        for (int slot = 0; slot < _player.PotionSlots.Count; slot++)
        {
            PotionModel? potion = _player.PotionSlots[slot];
            if (potion is null) continue;
            foreach (Creature target in _combat.Creatures)
            {
                if (!potion.IsValidTarget(target)) continue;
                uint targetId = Convert.ToUInt32(target.CombatId);
                result.Add(Action($"use_potion:{slot}:target:{targetId}", "use_potion", new Dictionary<string, object?> { ["slot"] = slot, ["model_id"] = potion.Id.Entry, ["target_id"] = targetId }));
            }
            if (potion.IsValidTarget(null))
                result.Add(Action($"use_potion:{slot}:none", "use_potion", new Dictionary<string, object?> { ["slot"] = slot, ["model_id"] = potion.Id.Entry, ["target_id"] = null }));
            if (_player.CanRemovePotions)
                result.Add(Action($"discard_potion:{slot}", "discard_potion", new Dictionary<string, object?> { ["slot"] = slot, ["model_id"] = potion.Id.Entry }));
        }
        result.Add(Action("end_turn", "end_turn", new Dictionary<string, object?>()));
        return result.ToArray();
    }

    private static object Action(string actionId, string kind, IReadOnlyDictionary<string, object?> parameters) => new { action_id = actionId, kind, parameters };

    private static uint NetCardId(CardModel card)
    {
        Type type = typeof(CardModel).Assembly.GetType("MegaCrit.Sts2.Core.GameActions.Multiplayer.NetCombatCardDb", true)!;
        object instance = type.GetProperty("Instance", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static)!.GetValue(null)!;
        return Convert.ToUInt32(Invoke(instance, "GetCardId", card));
    }

    private static string ModelEntry(object model)
    {
        object id = GetMember(model, "Id")!;
        return Convert.ToString(GetMember(id, "Entry"))!;
    }

    private static IEnumerable<object> Enumerate(object? value) => value is IEnumerable sequence ? sequence.Cast<object>() : [];

    private static object? GetMember(object target, string name) =>
        target.GetType().GetProperty(name, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance)?.GetValue(target)
        ?? target.GetType().GetField(name, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance)?.GetValue(target);

    private static object? Invoke(object target, string name, params object?[] arguments)
    {
        MethodInfo method = target.GetType().GetMethods(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance)
            .Where(x => x.Name == name && x.GetParameters().Length == arguments.Length)
            .Single(x => x.GetParameters().Select(p => p.ParameterType).Zip(arguments, (type, argument) => argument is null || type.IsInstanceOfType(argument)).All(match => match));
        return method.Invoke(target, arguments);
    }

    private static bool Inherits(Type type, string fullName)
    {
        for (Type? current = type; current is not null; current = current.BaseType)
            if (current.FullName == fullName) return true;
        return false;
    }

    private static string? StableAction(GameAction action)
    {
        if (action is PlayCardAction play)
        {
            CardModel? card = play.NetCombatCard.ToCardModelOrNull();
            if (card is null) return null;
            string id = Uri.EscapeDataString(CardId(card));
            return play.TargetId is uint target ? $"play:{id}:target:{target}" : $"play:{id}:none";
        }
        if (action is UsePotionAction potion) return potion.TargetId is uint target ? $"use_potion:{potion.PotionIndex}:target:{target}" : $"use_potion:{potion.PotionIndex}:none";
        if (action is DiscardPotionGameAction)
        {
            object net = action.ToNetAction();
            uint slot = Convert.ToUInt32(net.GetType().GetField("potionSlotIndex")!.GetValue(net));
            return $"discard_potion:{slot}";
        }
        return action is EndPlayerTurnAction ? "end_turn" : null;
    }

    private static void MapDeckInstances(Player player)
    {
        for (int index = 0; index < player.Deck.Cards.Count; index++) CardIds[player.Deck.Cards[index]] = $"deck-{index}-{player.Deck.Cards[index].Id.Entry}";
        MapCombatCards();
    }

    private static void MapCombatCards()
    {
        if (_player?.PlayerCombatState is null) return;
        foreach (CardModel card in new[] { _player.PlayerCombatState.Hand, _player.PlayerCombatState.DrawPile, _player.PlayerCombatState.DiscardPile, _player.PlayerCombatState.ExhaustPile, _player.PlayerCombatState.PlayPile }.SelectMany(x => x.Cards)) CardId(card);
    }
    private static void MapCombatCards(ICombatState _) => MapCombatCards();

    private static string CardId(CardModel card)
    {
        if (CardIds.TryGetValue(card, out string? id)) return id;
        if (card.DeckVersion is not null && CardIds.TryGetValue(card.DeckVersion, out id)) { CardIds[card] = id; return id; }
        id = $"dynamic-{_dynamicOrdinal++}-{card.Id.Entry}";
        CardIds[card] = id;
        return id;
    }

    private static RunState CurrentRun => RunManager.Instance.DebugOnlyGetState() ?? throw new InvalidOperationException("No active run.");
    private static SortedDictionary<string, int> RngCounters() => new(CurrentRun.Rng.ToSerializable().Counters.ToDictionary(x => x.Key.ToString(), x => x.Value), StringComparer.Ordinal);

    private static SortedDictionary<string, object?> SavedState(AbstractModel model)
    {
        object? props = model switch { CardModel card => card.ToSerializable().Props, RelicModel relic => relic.ToSerializable().Props, _ => null };
        SortedDictionary<string, object?> result = new(StringComparer.Ordinal);
        if (props is null) return result;
        foreach (string fieldName in new[] { "ints", "bools", "strings", "intArrays" })
        {
            object? list = props.GetType().GetField(fieldName)?.GetValue(props);
            if (list is not IEnumerable values) continue;
            foreach (object item in values)
            {
                Type type = item.GetType();
                result[Convert.ToString(type.GetField("name")!.GetValue(item))!] = type.GetField("value")!.GetValue(item);
            }
        }
        foreach (string unsupported in new[] { "modelIds", "cards", "cardArrays" })
            if (props.GetType().GetField(unsupported)?.GetValue(props) is IEnumerable values && values.Cast<object>().Any())
                throw new NotSupportedException($"Saved property group {unsupported} on {model.Id.Entry} is not supported by reset yet.");
        return result;
    }

    private static object BuildFingerprint()
    {
        string assemblyPath = typeof(RunManager).Assembly.Location;
        string dataDirectory = Path.GetDirectoryName(assemblyPath)!;
        string pckPath = Path.Combine(Directory.GetParent(dataDirectory)!.FullName, "SlayTheSpire2.pck");
        return new
        {
            version = FileVersionInfo.GetVersionInfo(assemblyPath).ProductVersion ?? "unknown",
            assembly_sha256 = Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(assemblyPath))),
            pck_sha256 = Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(pckPath)))
        };
    }

    private static void Write(object record)
    {
        _writer?.WriteLine(JsonSerializer.Serialize(record, Json));
    }

    private static string Sanitize(string value) => string.Concat(value.Select(x => char.IsLetterOrDigit(x) || x is '-' or '_' ? x : '_'));

    private static void Close()
    {
        if (_combat is not null)
        {
            _combat.CreaturesChanged -= MapCombatCards;
            CombatManager.Instance.TurnStarted -= OnTurnStarted;
        }
        if (RunManager.Instance?.ActionExecutor is not null)
        {
            RunManager.Instance.ActionExecutor.BeforeActionExecuted -= BeforeAction;
            RunManager.Instance.ActionExecutor.AfterActionExecuted -= AfterAction;
        }
        _writer?.Dispose();
        _writer = null;
        _combat = null;
        _player = null;
    }
}
