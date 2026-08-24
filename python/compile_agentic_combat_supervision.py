"""Compile successful agentic combat logs into native V10 training transitions.

This is a schema adapter, not a simulator: every label is an action actually taken
from the exact state recorded in a victorious run. Card model IDs are resolved from
the compiled game database rather than maintained as handwritten aliases.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
COMBAT_STATES = {"monster", "elite", "boss"}


def normalized_title(value: str) -> str:
    value = value.strip()
    if value.endswith("+"):
        value = value[:-1]
    return re.sub(r"[^A-Z0-9]+", "", value.upper())


def load_card_ids(database_path: Path) -> dict[tuple[str, str], str]:
    cards = json.loads(database_path.read_text(encoding="utf-8"))
    by_title: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for card in cards.values():
        by_title[normalized_title(str(card.get("title", "")))].append(card)

    resolved: dict[tuple[str, str], str] = {}
    for title, matches in by_title.items():
        for character in ("SILENT", "IRONCLAD", "DEFECT", "NECROBINDER", "REGENT"):
            preferred = [
                card for card in matches
                if str(card.get("class", "")).upper().replace("THE ", "") == character
                or str(card.get("id", "")).upper().endswith(f"_{character}")
            ]
            if len(preferred) == 1:
                resolved[(character, title)] = str(preferred[0]["id"])
            elif len(matches) == 1:
                resolved[(character, title)] = str(matches[0]["id"])
    return resolved


def card_model_id(card: dict[str, Any], character: str, lookup: dict[tuple[str, str], str]) -> str | None:
    return lookup.get((character, normalized_title(str(card.get("name", "")))))


def action_id(kind: str, card_id: str = "", target_id: int = 0, ordinal: int = 0) -> str:
    return f"expert:{kind}:{card_id}:{target_id}:{ordinal}"


def compile_transition(
    state: dict[str, Any],
    decision: dict[str, Any],
    character: str,
    lookup: dict[tuple[str, str], str],
    stats: Counter,
) -> dict[str, Any] | None:
    combat = state.get("combat") or {}
    player = combat.get("player") or {}
    enemies = combat.get("enemies") or []
    hand = player.get("hand") or []
    choice = decision.get("action") or {}
    kind = choice.get("action")
    if kind not in {"play_card", "use_potion", "end_turn"}:
        stats["non_combat_decision"] += 1
        return None

    encoded_hand = []
    hand_ids: dict[int, str] = {}
    for position, card in enumerate(hand):
        model_id = card_model_id(card, character, lookup)
        if model_id is None:
            stats[f"unknown_card:{card.get('name')}"] += 1
            model_id = "UNKNOWN"
        hand_ids[int(card.get("index", position))] = model_id
        encoded_hand.append({
            "card_id": model_id,
            "cost": int(card.get("energy_cost", 1)),
            "upgrades": 1 if str(card.get("name", "")).endswith("+") else 0,
            "damage": float(card.get("total_damage") or card.get("damage") or 0),
            "block": float(card.get("block") or 0),
            "target_type": card.get("target_type"),
            "playable": bool(card.get("playable", False)),
        })

    legal_actions: list[dict[str, Any]] = []
    equivalent_actions: dict[tuple[str, str, int], str] = {}

    def add_legal(action_type: str, model_id: str = "", target_id: int = 0, ordinal: int = 0) -> str:
        key = (action_type, model_id, target_id)
        if key in equivalent_actions:
            return equivalent_actions[key]
        identifier = action_id(action_type, model_id, target_id, ordinal)
        legal_actions.append({
            "action_id": identifier,
            "action_type": action_type,
            "metadata": {"card_id": model_id, "target_id": target_id},
        })
        equivalent_actions[key] = identifier
        return identifier

    for position, card in enumerate(hand):
        if not card.get("playable", False):
            continue
        index = int(card.get("index", position))
        model_id = hand_ids[index]
        target_type = str(card.get("target_type", ""))
        if target_type == "AnyEnemy":
            for enemy_index, enemy in enumerate(enemies):
                if float(enemy.get("hp", 0)) > 0:
                    add_legal("play_card", model_id, enemy_index + 1, index)
        else:
            add_legal("play_card", model_id, 0, index)

    potions = player.get("potions") or []
    for position, potion in enumerate(potions):
        if not potion.get("occupied") or not potion.get("can_use"):
            continue
        potion_index = int(potion.get("index", position))
        potion_id = f"POTION_{potion_index}"
        if str(potion.get("target_type", "")) == "AnyEnemy":
            for enemy_index, enemy in enumerate(enemies):
                if float(enemy.get("hp", 0)) > 0:
                    add_legal("use_potion", potion_id, enemy_index + 1, potion_index)
        else:
            add_legal("use_potion", potion_id, 0, potion_index)

    end_turn_id = add_legal("end_turn")
    chosen: str | None = None
    if kind == "end_turn":
        chosen = end_turn_id
    elif kind == "play_card":
        index = choice.get("card_index")
        if not isinstance(index, int) or index not in hand_ids:
            stats["bad_chosen_card_index"] += 1
            return None
        card = next((item for position, item in enumerate(hand) if int(item.get("index", position)) == index), None)
        if card is None or not card.get("playable", False):
            stats["chosen_card_not_playable"] += 1
            return None
        target_id = int(choice.get("target_index", -1)) + 1 if card.get("target_type") == "AnyEnemy" else 0
        chosen = equivalent_actions.get(("play_card", hand_ids[index], target_id))
    elif kind == "use_potion":
        potion_index = choice.get("option_index")
        if not isinstance(potion_index, int):
            stats["bad_chosen_potion_index"] += 1
            return None
        potion = next((item for position, item in enumerate(potions) if int(item.get("index", position)) == potion_index), None)
        if potion is None:
            stats["chosen_potion_missing"] += 1
            return None
        target_id = int(choice.get("target_index", -1)) + 1 if potion.get("target_type") == "AnyEnemy" else 0
        chosen = equivalent_actions.get(("use_potion", f"POTION_{potion_index}", target_id))

    if chosen is None:
        stats[f"chosen_not_legal:{kind}"] += 1
        return None
    if len(legal_actions) > 32:
        stats["too_many_legal_actions"] += 1
        return None

    enemy_tokens = []
    for enemy in enemies:
        total_damage = sum(
            float(intent.get("total_damage") or 0)
            for intent in (enemy.get("intents") or [])
            if str(intent.get("type", "")).lower() == "attack"
        )
        enemy_tokens.append({
            "enemy_id": enemy.get("enemy_id") or normalized_title(str(enemy.get("name", "UNKNOWN"))),
            "hp": enemy.get("hp", 0),
            "max_hp": enemy.get("max_hp", 1),
            "block": enemy.get("block", 0),
            "damage": total_damage,
            "repeats": 1,
            "is_alive": float(enemy.get("hp", 0)) > 0,
            "powers": enemy.get("powers") or [],
        })

    stats[f"kept:{kind}"] += 1
    return {
        "phase": "combat",
        "episode_id": state.get("run_id"),
        "character": character,
        "floor": state.get("floor", 1),
        "observation": {
            "phase": "combat",
            "player_hp": player.get("hp", state.get("hp", 1)),
            "player_max_hp": player.get("max_hp", state.get("hp_max", 1)),
            "player_block": player.get("block", 0),
            "player_energy": player.get("energy", 0),
            "combat": {
                "turn": combat.get("round", 1),
                "hand": encoded_hand,
                "enemies": enemy_tokens,
                "player_powers": player.get("powers") or [],
                "relics": player.get("relics") or [],
                "potions": player.get("potions") or [],
                "draw_pile_size": combat.get("draw_pile_size", 0),
                "discard_pile_size": combat.get("discard_pile_size", 0),
                "exhaust_pile_size": combat.get("exhaust_pile_size", 0),
                "stars": player.get("stars", 0),
            },
        },
        "legal_actions": legal_actions,
        "action": chosen,
        "advantage_weight": 1.0 + float(state.get("floor", 1)) / 15.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs", type=Path, default=REPO_ROOT / "community_runs" / "agentic_sts")
    parser.add_argument("--database", type=Path, default=REPO_ROOT / "game_database" / "compiled_cards.json")
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "artifacts" / "training" / "agentic_silent_winners.jsonl.gz")
    args = parser.parse_args()

    lookup = load_card_ids(args.database)
    stats: Counter = Counter()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    files = sorted(args.logs.glob("the_silent_*.jsonl.gz"))
    with gzip.open(args.output, "wt", encoding="utf-8") as output:
        for path in files:
            states: dict[int, dict[str, Any]] = {}
            with gzip.open(path, "rt", encoding="utf-8") as source:
                for line in source:
                    event = json.loads(line)
                    step = event.get("step")
                    if event.get("event") == "state" and event.get("state_type") in COMBAT_STATES and isinstance(step, int):
                        states[step] = event
                    elif event.get("event") == "decision" and isinstance(step, int):
                        state = states.get(step)
                        if state is None:
                            continue
                        transition = compile_transition(state, event, "SILENT", lookup, stats)
                        if transition is not None:
                            output.write(json.dumps(transition, separators=(",", ":")) + "\n")

    kept = sum(value for key, value in stats.items() if key.startswith("kept:"))
    attempted = kept + sum(value for key, value in stats.items() if key.startswith(("bad_", "chosen_", "too_many_")))
    print(json.dumps({
        "source_files": len(files),
        "kept": kept,
        "attempted_combat_decisions": attempted,
        "coverage": kept / max(1, attempted),
        "output": str(args.output),
        "stats": dict(stats.most_common()),
    }, indent=2))


if __name__ == "__main__":
    main()
