"""Scorers over auxiliary features extracted from shipped native state.

Scoring is policy judgment, not a substitute for native transitions. Checkpoint
loading is deliberately provenance-gated so approximate-simulator weights cannot
silently enter native search.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any


BASE_FEATURE_NAMES = (
    "hp_ratio", "block_ratio", "gold_norm", "act_norm", "floor_norm",
    "deck_norm", "relic_norm", "potion_ratio", "combat_present", "turn_norm",
    "energy_norm", "stars_norm", "enemy_hp_ratio", "enemy_alive_norm",
    "enemy_block_norm", "player_power_norm", "enemy_power_norm",
    "incoming_damage_ratio", "incoming_hit_norm",
)
CHARACTERS = ("IRONCLAD", "SILENT", "DEFECT", "NECROBINDER", "REGENT")
FEATURE_NAMES = (
    *BASE_FEATURE_NAMES,
    *(f"character_{name.lower()}" for name in CHARACTERS),
    *(f"card_hash_{index:02d}" for index in range(64)),
    *(f"relic_hash_{index:02d}" for index in range(16)),
    *(f"enemy_hash_{index:02d}" for index in range(32)),
    *(f"player_power_hash_{index:02d}" for index in range(16)),
    *(f"enemy_power_hash_{index:02d}" for index in range(16)),
    *(f"intent_hash_{index:02d}" for index in range(32)),
    *(f"hand_hash_{index:02d}" for index in range(32)),
    *(f"draw_hash_{index:02d}" for index in range(32)),
    *(f"discard_hash_{index:02d}" for index in range(32)),
    *(f"exhaust_hash_{index:02d}" for index in range(32)),
    *(f"play_hash_{index:02d}" for index in range(16)),
)


def _hashed_bag(items: list[tuple[str, float]], width: int) -> list[float]:
    result = [0.0] * width
    scale = max(1.0, math.sqrt(len(items)))
    for identity, weight in items:
        digest = hashlib.sha256(identity.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "little") % width
        sign = 1.0 if digest[4] & 1 else -1.0
        result[bucket] += sign * weight / scale
    return result


def encode_scoring_features(state: dict[str, Any]) -> list[float]:
    features = state.get("scoring_features")
    if not isinstance(features, dict) or features.get("schema_version") != 3:
        raise ValueError("state has no supported native scoring_features snapshot")
    hp, max_hp = float(features["current_hp"]), max(1.0, float(features["max_hp"]))
    combat = features.get("combat")
    creatures = combat.get("creatures", []) if isinstance(combat, dict) else []
    enemies = [creature for creature in creatures if creature.get("side") == "Enemy"]
    players = [creature for creature in creatures if creature.get("side") == "Player"]
    enemy_hp = sum(max(0.0, float(creature["hp"])) for creature in enemies)
    enemy_max = max(1.0, sum(max(0.0, float(creature["max_hp"])) for creature in enemies))
    enemy_alive = sum(bool(creature.get("alive")) for creature in enemies)
    enemy_block = sum(max(0.0, float(creature.get("block", 0))) for creature in enemies)
    player_powers = sum(abs(float(power.get("amount", 0))) for creature in players for power in creature.get("powers", []))
    enemy_powers = sum(abs(float(power.get("amount", 0))) for creature in enemies for power in creature.get("powers", []))
    intents = [intent for creature in enemies for intent in (creature.get("next_move") or {}).get("intents", [])]
    incoming_damage = sum(max(0.0, float(intent.get("damage") or 0)) * max(1.0, float(intent.get("repeats") or 1)) for intent in intents)
    incoming_hits = sum(max(1.0, float(intent.get("repeats") or 1)) for intent in intents if float(intent.get("damage") or 0) > 0)
    base = [
        hp / max_hp,
        max(0.0, float(features.get("block", 0))) / max_hp,
        min(1.0, max(0.0, float(features.get("gold", 0))) / 500.0),
        min(1.0, max(0.0, float(features.get("act_index", 0))) / 3.0),
        min(1.0, max(0.0, float(features.get("act_floor", 0))) / 16.0),
        min(1.0, len(features.get("deck", [])) / 50.0),
        min(1.0, len(features.get("relics", [])) / 20.0),
        min(1.0, max(0.0, float(features.get("potion_count", 0))) / 3.0),
        1.0 if combat is not None else 0.0,
        min(1.0, max(0.0, float(combat.get("turn", 0) if combat else 0)) / 20.0),
        min(1.0, max(0.0, float(combat.get("energy", 0) if combat else 0)) / 10.0),
        min(1.0, max(0.0, float(combat.get("stars", 0) if combat else 0)) / 10.0),
        enemy_hp / enemy_max if enemies else 0.0,
        min(1.0, enemy_alive / 10.0),
        min(1.0, enemy_block / 200.0),
        min(1.0, player_powers / 100.0),
        min(1.0, enemy_powers / 100.0),
        min(2.0, incoming_damage / max_hp),
        min(1.0, incoming_hits / 20.0),
    ]
    character = str(features.get("character", "")).upper()
    character_features = [1.0 if character == name else 0.0 for name in CHARACTERS]
    cards = [(str(card.get("model_id", "UNKNOWN")), 1.0 + 0.25 * float(card.get("upgrades", 0))) for card in features.get("deck", [])]
    relics = [(str(relic), 1.0) for relic in features.get("relics", [])]
    enemy_models = [(str(creature.get("model_id", "UNKNOWN")), (1.0 if creature.get("alive") else 0.0) + max(0.0, float(creature.get("hp", 0))) / max(1.0, float(creature.get("max_hp", 1))) + min(1.0, max(0.0, float(creature.get("block", 0))) / 100.0)) for creature in enemies]
    player_power_items = [(str(power.get("model_id", "UNKNOWN")), float(power.get("amount", 0))) for creature in players for power in creature.get("powers", [])]
    enemy_power_items = [(str(power.get("model_id", "UNKNOWN")), float(power.get("amount", 0))) for creature in enemies for power in creature.get("powers", [])]
    intent_items = [(f"{intent.get('intent_type', 'UNKNOWN')}:{intent.get('implementation', 'UNKNOWN')}", 1.0 + max(0.0, float(intent.get("damage") or 0)) / 20.0 + max(0.0, float(intent.get("repeats") or 1) - 1.0) / 5.0) for intent in intents]
    piles = combat.get("piles", {}) if combat else {}
    def pile_items(name: str) -> list[tuple[str, float]]:
        return [(str(card.get("model_id", "UNKNOWN")), 1.0 + 0.25 * float(card.get("upgrades", 0))) for card in piles.get(name, [])]
    return [
        *base,
        *character_features,
        *_hashed_bag(cards, 64),
        *_hashed_bag(relics, 16),
        # Absolute encounter identity does not generalize to unseen native models.
        # Preserve the schema width but deliberately rely on native HP/block/powers/intents.
        *([0.0] * 32),
        *_hashed_bag(player_power_items, 16),
        *_hashed_bag(enemy_power_items, 16),
        *_hashed_bag(intent_items, 32),
        *_hashed_bag(pile_items("hand"), 32),
        *_hashed_bag(pile_items("draw"), 32),
        *_hashed_bag(pile_items("discard"), 32),
        *_hashed_bag(pile_items("exhaust"), 32),
        *_hashed_bag(pile_items("play"), 16),
    ]


class NativeObservedMaterialScorer:
    """Transparent baseline over observed native quantities; not a win predictor."""

    certifying = False
    provenance = "handwritten utility over shipped-native observations"

    def __call__(self, state: dict[str, Any]) -> float:
        values = encode_scoring_features(state)
        feature = dict(zip(FEATURE_NAMES, values))
        if state.get("terminated"):
            return 1_000_000.0 if state.get("victory") else -1_000_000.0
        return (
            feature["hp_ratio"] * 100.0
            + feature["block_ratio"] * 15.0
            + (1.0 - feature["enemy_hp_ratio"]) * 50.0
            - feature["enemy_alive_norm"] * 5.0
            + feature["floor_norm"] * 2.0
        )


class NativeTorchValueScorer:
    """Small value network loadable only from exact native-rollout checkpoints."""

    FORMAT = "sts2-native-value-v1"
    certifying = False

    def __init__(self, model: Any, metadata: dict[str, Any]):
        self.model, self.metadata = model, metadata

    @staticmethod
    def create_model(hidden_sizes: tuple[int, ...] = (64, 32)) -> Any:
        import torch.nn as nn
        if not hidden_sizes or any(size < 1 for size in hidden_sizes):
            raise ValueError("native value model hidden sizes must be positive")
        layers: list[Any] = []
        width = len(FEATURE_NAMES)
        for size in hidden_sizes:
            layers.extend((nn.Linear(width, size), nn.Tanh()))
            width = size
        layers.append(nn.Linear(width, 1))
        return nn.Sequential(*layers)

    @classmethod
    def load(cls, path: str | Path, expected_build: dict[str, Any], *, allow_unpromoted: bool = False) -> "NativeTorchValueScorer":
        import torch
        payload = torch.load(Path(path), map_location="cpu", weights_only=True)
        if not isinstance(payload, dict) or payload.get("format") != cls.FORMAT:
            raise ValueError("checkpoint is not a provenance-gated native value model")
        metadata = payload.get("metadata") or {}
        if metadata.get("mechanics_source") != "shipped_native" or metadata.get("label_source") != "native_terminal_rollouts":
            raise ValueError("checkpoint labels are not exclusively shipped-native terminal rollouts")
        build = metadata.get("game_build") or {}
        for key in ("assembly_sha256", "pck_sha256"):
            if not build.get(key) or build[key] != expected_build.get(key):
                raise ValueError(f"checkpoint game build mismatch for {key}")
        promotion = metadata.get("promotion") or {}
        if not allow_unpromoted and not promotion.get("promoted"):
            raise ValueError("checkpoint has not passed the untouched sibling-state promotion gate")
        search_lift = metadata.get("search_lift") or {}
        if not allow_unpromoted and not search_lift.get("promoted"):
            raise ValueError("checkpoint has not passed the fresh-seed native search-lift gate")
        if tuple(metadata.get("feature_names", ())) != FEATURE_NAMES:
            raise ValueError("checkpoint feature schema mismatch")
        hidden_sizes = tuple(int(size) for size in metadata.get("model_hidden_sizes", (64, 32)))
        model = cls.create_model(hidden_sizes)
        model.load_state_dict(payload["state_dict"])
        model.eval()
        return cls(model, metadata)

    @classmethod
    def save(cls, path: str | Path, model: Any, metadata: dict[str, Any]) -> None:
        import torch
        complete = dict(metadata)
        complete.update({"mechanics_source": "shipped_native", "label_source": "native_terminal_rollouts", "feature_names": list(FEATURE_NAMES)})
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        torch.save({"format": cls.FORMAT, "metadata": complete, "state_dict": model.state_dict()}, Path(path))

    def __call__(self, state: dict[str, Any]) -> float:
        if state.get("terminated"):
            return 1_000_000.0 if state.get("victory") else -1_000_000.0
        import torch
        encoded = torch.tensor([encode_scoring_features(state)], dtype=torch.float32)
        with torch.no_grad():
            return float(self.model(encoded).item())
