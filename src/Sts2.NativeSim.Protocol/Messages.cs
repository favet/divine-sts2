using System.Text.Json;
using System.Text.Json.Serialization;

namespace Sts2.NativeSim.Protocol;

public static class ProtocolConstants { public const int Version = 1; public const int ObservationSchemaVersion = 2; }
public sealed record RpcRequest([property: JsonPropertyName("id")] string Id, [property: JsonPropertyName("method")] string Method, [property: JsonPropertyName("params")] JsonElement Parameters);
public sealed record RpcResponse([property: JsonPropertyName("id")] string Id, [property: JsonPropertyName("ok")] bool Ok, [property: JsonPropertyName("result")] object? Result = null, [property: JsonPropertyName("error")] ProtocolError? Error = null);
public sealed record ProtocolError([property: JsonPropertyName("code")] string Code, [property: JsonPropertyName("message")] string Message, [property: JsonPropertyName("details")] object? Details = null);
public sealed record GameBuildSpec([property: JsonPropertyName("version")] string? Version = null, [property: JsonPropertyName("assembly_sha256")] string? AssemblySha256 = null, [property: JsonPropertyName("pck_sha256")] string? PckSha256 = null);
public sealed record EnchantmentSpec([property: JsonPropertyName("model_id")] string ModelId, [property: JsonPropertyName("amount")] int Amount = 1);
public sealed record CardSpec([property: JsonPropertyName("instance_id")] string InstanceId, [property: JsonPropertyName("model_id")] string ModelId, [property: JsonPropertyName("upgrades")] int Upgrades = 0, [property: JsonPropertyName("native_state")] IReadOnlyDictionary<string, JsonElement>? NativeState = null, [property: JsonPropertyName("enchantment")] EnchantmentSpec? Enchantment = null);
public sealed record RelicSpec([property: JsonPropertyName("model_id")] string ModelId, [property: JsonPropertyName("counter")] int? Counter = null, [property: JsonPropertyName("native_state")] IReadOnlyDictionary<string, JsonElement>? NativeState = null);
public sealed record PotionSpec([property: JsonPropertyName("model_id")] string ModelId, [property: JsonPropertyName("slot")] int Slot, [property: JsonPropertyName("native_state")] IReadOnlyDictionary<string, JsonElement>? NativeState = null);
public sealed record EnemySpec(
    [property: JsonPropertyName("model_id")] string ModelId,
    [property: JsonPropertyName("current_hp")] int CurrentHp,
    [property: JsonPropertyName("max_hp")] int MaxHp,
    [property: JsonPropertyName("next_move_id")] string? NextMoveId = null,
    [property: JsonPropertyName("move_history")] IReadOnlyList<string>? MoveHistory = null,
    [property: JsonPropertyName("block")] int? Block = null);
public sealed record ResetRequest(
    [property: JsonPropertyName("game_build")] GameBuildSpec GameBuild,
    [property: JsonPropertyName("seed")] string Seed,
    [property: JsonPropertyName("rng_counters")] IReadOnlyDictionary<string, int>? RngCounters,
    [property: JsonPropertyName("character")] string Character,
    [property: JsonPropertyName("ascension")] int Ascension,
    [property: JsonPropertyName("encounter")] string Encounter,
    [property: JsonPropertyName("current_hp")] int CurrentHp,
    [property: JsonPropertyName("max_hp")] int MaxHp,
    [property: JsonPropertyName("deck")] IReadOnlyList<CardSpec> Deck,
    [property: JsonPropertyName("initial_hand")] IReadOnlyList<string>? InitialHand,
    [property: JsonPropertyName("relics")] IReadOnlyList<RelicSpec>? Relics,
    [property: JsonPropertyName("potions")] IReadOnlyList<PotionSpec>? Potions,
    [property: JsonPropertyName("gold")] int Gold,
    [property: JsonPropertyName("turn")] int Turn = 1,
    [property: JsonPropertyName("energy")] int? Energy = null,
    [property: JsonPropertyName("run_context")] IReadOnlyDictionary<string, JsonElement>? RunContext = null,
    [property: JsonPropertyName("stars")] int? Stars = null,
    [property: JsonPropertyName("enemies")] IReadOnlyList<EnemySpec>? Enemies = null,
    [property: JsonPropertyName("initial_draw_pile")] IReadOnlyList<string>? InitialDrawPile = null,
    [property: JsonPropertyName("invoke_combat_entry_hooks")] bool InvokeCombatEntryHooks = false,
    [property: JsonPropertyName("capture_orbs")] bool CaptureOrbs = true,
    [property: JsonPropertyName("use_character_starting_loadout")] bool UseCharacterStartingLoadout = false);
public sealed record StepRequest([property: JsonPropertyName("action_id")] string ActionId);
public sealed record EventResetRequest(
    [property: JsonPropertyName("state")] ResetRequest State,
    [property: JsonPropertyName("event_id")] string EventId);
public sealed record ItemRewardResetRequest(
    [property: JsonPropertyName("state")] ResetRequest State,
    [property: JsonPropertyName("reward_kind")] string RewardKind,
    [property: JsonPropertyName("model_id")] string? ModelId = null);
public sealed record CustomRewardResetRequest(
    [property: JsonPropertyName("state")] ResetRequest State,
    [property: JsonPropertyName("reward_kinds")] IReadOnlyList<string> RewardKinds,
    [property: JsonPropertyName("linked")] bool Linked = false);
public sealed record RestoreRequest([property: JsonPropertyName("state_handle")] string StateHandle);
public sealed record LegalAction([property: JsonPropertyName("action_id")] string ActionId, [property: JsonPropertyName("kind")] string Kind, [property: JsonPropertyName("parameters")] IReadOnlyDictionary<string, object?> Parameters);
public sealed record EnvironmentResult(
    [property: JsonPropertyName("observation")] object Observation,
    [property: JsonPropertyName("state_hash")] string StateHash,
    [property: JsonPropertyName("legal_actions")] IReadOnlyList<LegalAction> LegalActions,
    [property: JsonPropertyName("terminated")] bool Terminated,
    [property: JsonPropertyName("victory")] bool Victory,
    [property: JsonPropertyName("state_handle")] string StateHandle,
    [property: JsonPropertyName("transition")] object? Transition = null,
    [property: JsonPropertyName("scoring_features")] object? ScoringFeatures = null);
