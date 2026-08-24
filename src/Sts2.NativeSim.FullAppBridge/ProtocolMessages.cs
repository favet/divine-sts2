using System.Text.Json.Serialization;

namespace Sts2.NativeSim.FullAppBridge;

public sealed class RpcRequest
{
    [JsonPropertyName("id")]
    public int Id { get; set; }

    [JsonPropertyName("method")]
    public string Method { get; set; } = "";

    [JsonPropertyName("params")]
    public Dictionary<string, object?>? Params { get; set; }
}

public sealed class RpcResponse
{
    [JsonPropertyName("id")]
    public int Id { get; set; }

    [JsonPropertyName("result")]
    public object? Result { get; set; }

    [JsonPropertyName("error")]
    public string? Error { get; set; }
}

public sealed class LegalActionDto
{
    [JsonPropertyName("action_id")]
    public string ActionId { get; set; } = "";

    [JsonPropertyName("action_type")]
    public string ActionType { get; set; } = "";

    [JsonPropertyName("description")]
    public string Description { get; set; } = "";

    [JsonPropertyName("metadata")]
    public Dictionary<string, object?>? Metadata { get; set; }
}

public sealed class ObservationDto
{
    [JsonPropertyName("schema_version")]
    public int SchemaVersion { get; set; } = 2;

    [JsonPropertyName("phase")]
    public string Phase { get; set; } = "";

    [JsonPropertyName("is_terminal")]
    public bool IsTerminal { get; set; }

    [JsonPropertyName("is_victory")]
    public bool IsVictory { get; set; }

    [JsonPropertyName("seed")]
    public string Seed { get; set; } = "";

    [JsonPropertyName("character")]
    public string Character { get; set; } = "";

    [JsonPropertyName("ascension")]
    public int Ascension { get; set; }

    [JsonPropertyName("act")]
    public int Act { get; set; }

    [JsonPropertyName("floor")]
    public int Floor { get; set; }

    [JsonPropertyName("gold")]
    public int Gold { get; set; }

    [JsonPropertyName("player_hp")]
    public int PlayerHp { get; set; }

    [JsonPropertyName("player_max_hp")]
    public int PlayerMaxHp { get; set; }

    [JsonPropertyName("player_block")]
    public int PlayerBlock { get; set; }

    [JsonPropertyName("player_energy")]
    public int PlayerEnergy { get; set; }

    [JsonPropertyName("player_powers")]
    public Dictionary<string, int> PlayerPowers { get; set; } = new();

    [JsonPropertyName("deck_cards")]
    public List<string> DeckCards { get; set; } = new();

    [JsonPropertyName("relics")]
    public List<string> Relics { get; set; } = new();

    [JsonPropertyName("potions")]
    public List<string> Potions { get; set; } = new();

    [JsonPropertyName("combat")]
    public CombatObservationDto? Combat { get; set; }

    [JsonPropertyName("room")]
    public RoomObservationDto? Room { get; set; }

    [JsonPropertyName("state_hash")]
    public string StateHash { get; set; } = "";
}

public sealed class CombatObservationDto
{
    [JsonPropertyName("turn")]
    public int Turn { get; set; }

    [JsonPropertyName("hand")]
    public List<CardObservationDto> Hand { get; set; } = new();

    [JsonPropertyName("draw_pile_count")]
    public int DrawPileCount { get; set; }

    [JsonPropertyName("discard_pile_count")]
    public int DiscardPileCount { get; set; }

    [JsonPropertyName("exhaust_pile_count")]
    public int ExhaustPileCount { get; set; }

    [JsonPropertyName("enemies")]
    public List<EnemyObservationDto> Enemies { get; set; } = new();
}

public sealed class CardObservationDto
{
    [JsonPropertyName("index")]
    public int Index { get; set; }

    [JsonPropertyName("card_id")]
    public string CardId { get; set; } = "";

    [JsonPropertyName("cost")]
    public int Cost { get; set; }

    [JsonPropertyName("can_play")]
    public bool CanPlay { get; set; }

    [JsonPropertyName("target_type")]
    public string TargetType { get; set; } = "";

    [JsonPropertyName("upgrades")]
    public int Upgrades { get; set; }
}

public sealed class EnemyObservationDto
{
    [JsonPropertyName("combat_id")]
    public ulong CombatId { get; set; }

    [JsonPropertyName("model_id")]
    public string ModelId { get; set; } = "";

    [JsonPropertyName("hp")]
    public int Hp { get; set; }

    [JsonPropertyName("max_hp")]
    public int MaxHp { get; set; }

    [JsonPropertyName("block")]
    public int Block { get; set; }

    [JsonPropertyName("is_alive")]
    public bool IsAlive { get; set; }

    [JsonPropertyName("intent")]
    public string Intent { get; set; } = "";

    [JsonPropertyName("powers")]
    public Dictionary<string, int> Powers { get; set; } = new();
}

public sealed class RoomObservationDto
{
    [JsonPropertyName("room_type")]
    public string RoomType { get; set; } = "";

    [JsonPropertyName("options")]
    public List<string> Options { get; set; } = new();

    [JsonPropertyName("details")]
    public Dictionary<string, object?> Details { get; set; } = new();
}
