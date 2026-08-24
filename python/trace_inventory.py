"""Summarize mechanic coverage already present in exported differential traces.

This is read-only inventory, not replay validation. Certification still comes from
`differential_replay.py`; the inventory prevents captured coverage from being
overlooked when planning the next trace.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


def summarize(path: Path) -> dict[str, Any]:
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not records or records[0].get("type") != "header":
        raise ValueError(f"{path}: missing trace header")
    header = records[0]
    checkpoints = [record for record in records[1:] if record.get("type") == "checkpoint"]
    reset = header.get("reset") or {}
    deck = {card["instance_id"]: card["model_id"] for card in reset.get("deck", [])}
    actions: Counter[str] = Counter()
    played_cards: Counter[str] = Counter()
    enemy_models: set[str] = set()
    enemy_moves: set[str] = set()
    intents: set[str] = set()
    powers: dict[str, dict[str, set[int | float]]] = {"Player": {}, "Enemy": {}}
    observed_cards: set[str] = set()
    resource_states: set[tuple[int, int, int]] = set()
    orb_capacities: set[int] = set()
    orb_models: set[str] = set()
    orb_states: set[tuple[str, int | float, int | float]] = set()
    turns: set[int] = set()

    for checkpoint in checkpoints:
        action_id = checkpoint.get("action_id")
        if action_id is None:
            actions["reset"] += 1
        elif action_id == "end_turn":
            actions["end_turn"] += 1
        elif action_id.startswith("play:"):
            actions["play_card"] += 1
            instance_id = action_id.split(":", 2)[1]
            played_cards[deck.get(instance_id, _card_model(checkpoint["observation"], instance_id))] += 1
        elif action_id.startswith("use_potion:"):
            actions["use_potion"] += 1
        elif action_id.startswith("discard_potion:"):
            actions["discard_potion"] += 1
        else:
            actions[action_id.split(":", 1)[0]] += 1

        observation = checkpoint["observation"]
        combat = observation["combat"]
        turns.add(combat["turn"])
        resource_states.add((combat["energy"], combat["max_energy"], combat.get("stars", 0)))
        if "orbs" in combat:
            orb_capacities.add(combat["orbs"]["capacity"])
            for orb in combat["orbs"].get("entries", []):
                orb_models.add(orb["model_id"])
                orb_states.add((orb["model_id"], orb["passive"], orb["evoke"]))
        for pile in combat.get("piles", []):
            observed_cards.update(card["model_id"] for card in pile.get("cards", []))
        for creature in combat["creatures"]:
            side = creature["side"]
            if side == "Enemy":
                enemy_models.add(creature["model_id"])
            move = creature.get("next_move")
            if move is not None:
                if side == "Enemy":
                    enemy_moves.add(move["id"])
                intents.update(intent["intent_type"] for intent in move.get("intents", []))
            for power in creature.get("powers", []):
                powers.setdefault(side, {}).setdefault(power["model_id"], set()).add(power["amount"])

    final = checkpoints[-1]["observation"] if checkpoints else {}
    return {
        "trace": str(path),
        "source": header.get("source"),
        "comparison": header.get("comparison"),
        "game_build": header.get("game_build"),
        "encounter": reset.get("encounter"),
        "encounter_tier": encounter_tier(reset.get("encounter")),
        "character": reset.get("character"),
        "ascension": reset.get("ascension"),
        "checkpoints": len(checkpoints),
        "actions": dict(sorted(actions.items())),
        "played_cards": dict(sorted(played_cards.items())),
        "observed_cards": sorted(observed_cards),
        "turns": sorted(turns),
        "enemy_models": sorted(enemy_models),
        "enemy_moves": sorted(enemy_moves),
        "intents": sorted(intents),
        "powers": {
            side.lower(): {name: sorted(amounts) for name, amounts in sorted(side_powers.items())}
            for side, side_powers in sorted(powers.items())
        },
        # Retained for format-1 report consumers.
        "enemy_powers": {name: sorted(amounts) for name, amounts in sorted(powers.get("Enemy", {}).items())},
        "relics": [relic["model_id"] for relic in reset.get("relics", [])],
        "potions": [potion["model_id"] for potion in reset.get("potions", [])],
        "character_resources": {
            "energy": sorted({state[0] for state in resource_states}),
            "max_energy": sorted({state[1] for state in resource_states}),
            "stars": sorted({state[2] for state in resource_states}),
            "orb_capacity": sorted(orb_capacities),
        },
        "orbs": sorted(orb_models),
        "orb_states": [
            {"model_id": model, "passive": passive, "evoke": evoke}
            for model, passive, evoke in sorted(orb_states)
        ],
        "terminal": final.get("terminal"),
        "victory": final.get("victory"),
        "terminal_outcome": "victory" if final.get("victory") is True else "loss" if final.get("terminal") is True else "incomplete",
    }


def _card_model(observation: dict[str, Any], instance_id: str) -> str:
    for pile in observation.get("combat", {}).get("piles", []):
        for card in pile.get("cards", []):
            if card.get("instance_id") == instance_id:
                return card.get("model_id", "<dynamic-or-unknown>")
    return "<dynamic-or-unknown>"


def encounter_tier(encounter: str | None) -> str | None:
    if not encounter:
        return None
    if encounter.endswith("_BOSS"):
        return "boss"
    if encounter.endswith("_ELITE"):
        return "elite"
    if encounter.endswith("_WEAK") or encounter.endswith("_NORMAL"):
        return "hallway"
    return "unknown"


def semantic_checkpoint_hashes(path: Path) -> list[str]:
    """Exact novelty signatures used only for scheduling, never for certification."""
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    header = records[0]
    reset = header["reset"]
    signatures: list[str] = []
    for checkpoint in records[1:]:
        observation = checkpoint["observation"]
        combat = observation["combat"]
        signature = {
            "build": header.get("game_build"),
            "character": reset.get("character"),
            "encounter": reset.get("encounter"),
            "action": checkpoint.get("action_id"),
            "turn": combat.get("turn"),
            "energy": combat.get("energy"),
            "stars": combat.get("stars"),
            "orbs": combat.get("orbs"),
            "creatures": [
                {
                    "model": creature.get("model_id"),
                    "side": creature.get("side"),
                    "alive": creature.get("alive"),
                    "move": (creature.get("next_move") or {}).get("id"),
                    "powers": sorted((power.get("model_id"), power.get("amount")) for power in creature.get("powers", [])),
                }
                for creature in combat.get("creatures", [])
            ],
            "relics": sorted(relic.get("model_id") for relic in observation.get("inventory", {}).get("relics", [])),
            "potions": sorted(potion.get("model_id") for potion in observation.get("inventory", {}).get("potions", []) if potion),
            "terminal": observation.get("terminal"),
            "victory": observation.get("victory"),
        }
        encoded = json.dumps(signature, sort_keys=True, separators=(",", ":")).encode("utf-8")
        signatures.append(hashlib.sha256(encoded).hexdigest())
    return signatures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("traces", nargs="+", type=Path)
    parser.add_argument("--semantic-hashes-only", action="store_true")
    args = parser.parse_args()
    value = ({str(path): semantic_checkpoint_hashes(path) for path in args.traces}
             if args.semantic_hashes_only else [summarize(path) for path in args.traces])
    print(json.dumps(value, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
