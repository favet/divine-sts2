"""Compile shipped-DLL rollout shards into outcome-weighted training corpora."""
from __future__ import annotations

import argparse
import gzip
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CARD_DATABASE = REPO_ROOT / "game_database" / "compiled_cards.json"
CARD_DATABASE = json.loads(DEFAULT_CARD_DATABASE.read_text(encoding="utf-8")) if DEFAULT_CARD_DATABASE.exists() else {}

def open_text(path: Path, mode: str):
    return gzip.open(path, mode + "t", encoding="utf-8") if path.suffix == ".gz" else path.open(mode, encoding="utf-8")


def shard_paths(inputs: list[str]) -> list[Path]:
    found: set[Path] = set()
    for value in inputs:
        path = Path(value)
        if path.is_dir():
            found.update(path.glob("*.jsonl"))
            found.update(path.glob("*.jsonl.gz"))
        elif path.is_file():
            found.add(path)
    return sorted(found)


def records(paths: Iterable[Path]):
    for path in paths:
        with open_text(path, "r") as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)


def episode_return(summary: dict[str, Any], act_entry_bonus: float = 0.0,
                   act2_clear_bonus: float = 0.0) -> float:
    progress = min(1.0, max(0.0, float(summary.get("max_floor", 0))) / 48.0)
    max_act = int(summary.get("max_act_index", 0))
    return (
        progress
        + act_entry_bonus * float(max_act >= 1)
        + act2_clear_bonus * float(max_act >= 2)
        + (1.0 if summary.get("victory") else 0.0)
    )


def combat_sample(row: dict[str, Any], weight: float, result: float) -> dict[str, Any] | None:
    obs = row.get("observation") or {}
    combat = obs.get("combat") or {}
    features = row.get("scoring_features") or {}
    if row.get("decision_kind") not in {"combat_action", "card_choice"} or not combat:
        return None
    native_actions = row.get("legal_actions") or []
    if len(native_actions) <= 1:
        return None
    piles = {pile.get("name"): pile.get("cards") or [] for pile in combat.get("piles") or []}
    hand = piles.get("Hand", [])
    by_instance = {card.get("instance_id"): card for card in hand}
    choice_options = {
        option.get("option_id"): option.get("model_id")
        for option in (obs.get("outstanding_choice") or {}).get("options") or []
    }
    enemy_creatures = [
        creature for creature in combat.get("creatures") or []
        if str(creature.get("side", "")).lower() == "enemy"
    ]
    target_slots = {int(creature.get("combat_id") or (index + 1)): index + 1 for index, creature in enumerate(enemy_creatures)}
    legal_actions = []
    for action in native_actions:
        params = action.get("parameters") or {}
        kind = action.get("kind", "")
        card = by_instance.get(params.get("instance_id"), {})
        option_ids = params.get("option_ids") or []
        choice_card_id = choice_options.get(option_ids[0]) if len(option_ids) == 1 else None
        target_id_raw = params.get("target_id")
        target_id_val = int(target_id_raw) if target_id_raw is not None else 0
        legal_actions.append({
            "action_id": action.get("action_id"),
            "action_type": "play_card" if kind in {"choose_cards", "choose_option"} else kind,
            "metadata": {
                "card_id": choice_card_id or card.get("model_id") or params.get("model_id", ""),
                "target_id": target_slots.get(target_id_val, target_id_val),
            },
        })
    enemies = []
    for creature in enemy_creatures:
        damage = 0.0
        repeats = 1.0
        for intent in (creature.get("next_move") or {}).get("intents") or []:
            damage = max(damage, float(intent.get("damage") or 0.0))
            repeats = max(repeats, float(intent.get("repeats") or 1.0))
        enemies.append({
            "enemy_id": creature.get("model_id"),
            "hp": creature.get("hp"), "max_hp": creature.get("max_hp"),
            "block": creature.get("block", 0), "damage": damage,
            "repeats": repeats, "is_alive": creature.get("alive", True),
            "powers": creature.get("powers") or [],
        })
    player_creature = next(
        (creature for creature in combat.get("creatures") or [] if str(creature.get("side", "")).lower() == "player"),
        {},
    )
    scoring_piles = (features.get("combat") or {}).get("piles") or {}
    inventory = obs.get("inventory") or {}
    floor = int(features.get("act_index", 0)) * 16 + int(features.get("act_floor", 0))
    encoded_hand = []
    for card in hand:
        model_id = str(card.get("model_id") or "")
        upgrades = int(card.get("upgrades", 0))
        definition = CARD_DATABASE.get(model_id, {})
        base = definition.get("base_vars") or {}
        upgrade = definition.get("upgrades") or {}
        encoded_hand.append({
            "card_id": model_id, "cost": card.get("energy_cost", definition.get("cost", 1)),
            "upgrades": upgrades,
            "damage": float(base.get("damage", 0)) + upgrades * float(upgrade.get("damage", 0)),
            "block": float(base.get("block", 0)) + upgrades * float(upgrade.get("block", 0)),
            "playable": True, "target_type": card.get("target_type"),
        })
    return {
        "schema_version": 1,
        "mechanics_source": "shipped_sts2_dll",
        "label_source": "native_on_policy_awr",
        "episode_id": row.get("episode_id"), "seed": row.get("seed"),
        "character": row.get("character"), "ascension": row.get("ascension"),
        "step": row.get("step"), "phase": "combat", "floor": floor,
        "decision_kind": row.get("decision_kind"),
        "state_hash": row.get("state_hash"),
        "observation": {
            "phase": "combat", "player_hp": features.get("current_hp"),
            "player_max_hp": features.get("max_hp"), "player_block": features.get("block", 0),
            "player_energy": combat.get("energy", 0),
            "combat": {
                "turn": combat.get("turn", 1),
                "hand": encoded_hand,
                "enemies": enemies,
                "player_powers": player_creature.get("powers") or [],
                "relics": [{"model_id": relic} for relic in features.get("relics") or []],
                "potions": [
                    {"model_id": potion.get("model_id"), "occupied": True}
                    for potion in inventory.get("potions") or [] if potion
                ],
                "orbs": combat.get("orbs") or {"capacity": 0, "entries": []},
                "draw_pile_size": len(scoring_piles.get("draw", [])),
                "discard_pile_size": len(scoring_piles.get("discard", [])),
                "exhaust_pile_size": len(scoring_piles.get("exhaust", [])),
                "stars": combat.get("stars", 0),
            },
        },
        "legal_actions": legal_actions, "action": row.get("action"),
        "policy_source": row.get("policy_source"), "episode_return": result,
        "advantage_weight": weight,
    }


def compile_rollouts(args: argparse.Namespace) -> dict[str, Any]:
    paths = shard_paths(args.inputs)
    if not paths:
        raise ValueError("no .jsonl or .jsonl.gz shards found")
    summaries = {
        row["episode_id"]: row for row in records(paths)
        if row.get("record_type") == "episode_summary" and row.get("valid_terminal")
    }
    if not summaries:
        raise ValueError("no valid terminal episode summaries found")
    returns = {
        episode_id: episode_return(summary, args.act_entry_bonus, args.act2_clear_bonus)
        for episode_id, summary in summaries.items()
    }
    episode_buckets: dict[str, set[tuple[str, int, int]]] = defaultdict(set)
    macro_rows = 0
    for row in records(paths):
        episode_id = row.get("episode_id")
        if row.get("record_type") != "transition" or episode_id not in summaries:
            continue
        f = row.get("scoring_features") or {}
        floor = int(f.get("act_index", 0)) * 16 + int(f.get("act_floor", 0))
        episode_buckets[episode_id].add((str(row.get("character")), int(row.get("ascension", 0)), floor))
    bucket_values: dict[tuple[str, int, int], list[float]] = defaultdict(list)
    for episode_id, buckets in episode_buckets.items():
        for bucket in buckets:
            bucket_values[bucket].append(returns[episode_id])
    baselines = {bucket: sum(values) / len(values) for bucket, values in bucket_values.items()}

    combat_path = Path(args.combat_output)
    macro_path = Path(args.macro_output)
    combat_path.parent.mkdir(parents=True, exist_ok=True)
    macro_path.parent.mkdir(parents=True, exist_ok=True)
    combat_rows = 0
    with open_text(combat_path, "w") as combat_handle, open_text(macro_path, "w") as macro_handle:
        for row in records(paths):
            episode_id = row.get("episode_id")
            if row.get("record_type") != "transition" or episode_id not in summaries:
                continue
            f = row.get("scoring_features") or {}
            floor = int(f.get("act_index", 0)) * 16 + int(f.get("act_floor", 0))
            bucket = (str(row.get("character")), int(row.get("ascension", 0)), floor)
            advantage = returns[episode_id] - baselines.get(bucket, returns[episode_id])
            weight = min(args.max_weight, max(args.min_weight, math.exp(advantage / args.beta)))
            sample = combat_sample(row, weight, returns[episode_id])
            if sample is not None:
                combat_handle.write(json.dumps(sample, separators=(",", ":")) + "\n")
                combat_rows += 1
            else:
                annotated = {
                    **row,
                    "mechanics_source": "shipped_sts2_dll",
                    "label_source": "native_on_policy_awr",
                    "episode_return": returns[episode_id],
                    "advantage_weight": weight,
                }
                macro_handle.write(json.dumps(annotated, separators=(",", ":")) + "\n")
                macro_rows += 1
    report = {
        "input_shards": len(paths), "valid_episodes": len(summaries),
        "combat_samples": combat_rows, "macro_samples": macro_rows,
        "combat_output": str(combat_path.resolve()), "macro_output": str(macro_path.resolve()),
        "weight_range": [args.min_weight, args.max_weight], "beta": args.beta,
        "fitness": {
            "act_entry_bonus": args.act_entry_bonus,
            "act2_clear_bonus": args.act2_clear_bonus,
        },
    }
    print(json.dumps(report, indent=2))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", help="rollout shard files or directories")
    parser.add_argument("--combat-output", default="artifacts/training/native_combat_awr.jsonl.gz")
    parser.add_argument("--macro-output", default="artifacts/training/native_macro_awr.jsonl.gz")
    parser.add_argument("--beta", type=float, default=0.25)
    parser.add_argument("--min-weight", type=float, default=0.25)
    parser.add_argument("--max-weight", type=float, default=4.0)
    parser.add_argument("--act-entry-bonus", type=float, default=0.0,
                        help="fitness bonus for reaching Act 2 (max_act_index >= 1)")
    parser.add_argument("--act2-clear-bonus", type=float, default=0.0,
                        help="fitness bonus for beating Act 2 (max_act_index >= 2)")
    parser.add_argument("--card-database", type=Path, help="optional compiled card metadata; native numeric observations are used when omitted")
    args = parser.parse_args()
    if args.beta <= 0 or args.min_weight <= 0 or args.max_weight < args.min_weight:
        parser.error("weights and beta must be positive and max-weight >= min-weight")
    if args.card_database:
        global CARD_DATABASE
        CARD_DATABASE = json.loads(args.card_database.read_text(encoding="utf-8"))
    compile_rollouts(args)


if __name__ == "__main__":
    main()
