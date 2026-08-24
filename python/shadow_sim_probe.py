"""External-process probe for the pinned zhiyue/sts2-rl-agent checkout.

This file is owned by NativeSim. It imports the separately installed shadow
simulator only through its public Python surface and emits normalized facts.
"""

from __future__ import annotations

import argparse
import json
from enum import Enum

from sts2_env.core.enums import CardId
from sts2_env.research import CombatIterator


SCENARIOS = {
    "bygone_three_strike_turn": {
        "cards": [CardId.STRIKE_IRONCLAD, CardId.STRIKE_IRONCLAD, CardId.STRIKE_IRONCLAD],
        "actions": [
            ("after_play_1", "play", CardId.STRIKE_IRONCLAD, 0),
            ("after_play_2", "play", CardId.STRIKE_IRONCLAD, 0),
            ("after_play_3", "play", CardId.STRIKE_IRONCLAD, 0),
            ("after_turn", "end_turn", None, None),
        ],
    },
    "bygone_defend_strike_turn": {
        "cards": [CardId.DEFEND_IRONCLAD, CardId.STRIKE_IRONCLAD],
        "actions": [
            ("after_defend", "play", CardId.DEFEND_IRONCLAD, None),
            ("after_strike", "play", CardId.STRIKE_IRONCLAD, 0),
            ("after_turn", "end_turn", None, None),
        ],
    },
    "bygone_bash_turn": {
        "cards": [CardId.BASH],
        "actions": [
            ("after_bash", "play", CardId.BASH, 0),
            ("after_turn", "end_turn", None, None),
        ],
    },
    "bygone_purity_choice": {
        "cards": [CardId.PURITY, CardId.STRIKE_IRONCLAD, CardId.DEFEND_IRONCLAD, CardId.STRIKE_IRONCLAD],
        "actions": [
            ("after_play", "play", CardId.PURITY, None),
            ("after_choice", "choose", [0, 1], None),
        ],
    },
    "axebot_defend_turn": {
        "encounter": "AXEBOTS_NORMAL",
        "cards": [CardId.DEFEND_IRONCLAD],
        "actions": [
            ("after_defend", "play", CardId.DEFEND_IRONCLAD, None),
            ("after_turn", "end_turn", None, None),
        ],
    },
    "bowlbugs_normal_initial": {
        "encounter": "BOWLBUGS_NORMAL",
        "cards": [CardId.DEFEND_IRONCLAD],
        "actions": [],
    },
    "seapunk_normal_initial": {
        "encounter": "SEAPUNK_NORMAL",
        "cards": [CardId.DEFEND_IRONCLAD],
        "actions": [],
    },
    "aeonglass_boss_turn": {
        "encounter": "AEONGLASS_BOSS",
        "cards": [CardId.DEFEND_IRONCLAD],
        "actions": [("after_turn", "end_turn", None, None)],
    },
}


def _name(value: object) -> str:
    if isinstance(value, Enum):
        return value.name
    return str(value)


def _power_name(value: object) -> str:
    name = _name(value)
    return name[:-6] if name.endswith("_POWER") else name


def _card_ids(cards: list[object]) -> list[str]:
    return [_name(getattr(card, "card_id")) for card in cards]


def _intent(move: object | None) -> dict | None:
    if move is None:
        return None
    intents = []
    for intent in getattr(move, "intents", []):
        intent_type = _name(getattr(intent, "intent_type", "UNKNOWN")).upper()
        is_attack = intent_type in {"ATTACK", "MULTI_ATTACK"}
        if intent_type == "MULTI_ATTACK":
            intent_type = "ATTACK"
        intents.append({
            "intent_type": intent_type,
            "damage": getattr(intent, "damage", None) if is_attack else None,
            "repeats": getattr(intent, "hits", getattr(intent, "repeats", None)) if is_attack else None,
        })
    move_id = str(getattr(move, "state_id", getattr(move, "move_id", "UNKNOWN")))
    if move_id == "INITIAL_SLEEP_MOVE":
        move_id = "SLEEP_MOVE"
    return {"id": move_id, "intents": intents}


def snapshot(combat: object) -> dict:
    player = combat.player
    player_powers = sorted(
        ({"model_id": _power_name(power_id), "amount": int(getattr(power, "amount", 0))}
         for power_id, power in player.powers.items()),
        key=lambda item: item["model_id"],
    )
    enemies = []
    for enemy in combat.enemies:
        ai = combat.enemy_ais.get(enemy.combat_id)
        powers = sorted(
            ({"model_id": _power_name(power_id), "amount": int(getattr(power, "amount", 0))}
             for power_id, power in enemy.powers.items()),
            key=lambda item: item["model_id"],
        )
        enemies.append({
            "model_id": str(enemy.monster_id),
            "hp": int(enemy.current_hp),
            "max_hp": int(enemy.max_hp),
            "block": int(enemy.block),
            "alive": bool(enemy.is_alive),
            "powers": powers,
            "next_move": _intent(getattr(ai, "current_move", None)),
        })
    pending = combat.pending_choice
    pending_choice = None
    if pending is not None:
        pending_choice = {
            "kind": "card_choice",
            "min_select": int(pending.min_choices),
            "max_select": int(pending.max_choices),
            "options": [_name(option.card.card_id) for option in pending.options],
        }
    playable = pending is None and any(combat.can_play_card(card) for card in combat.hand)
    legal_kinds = (["choose_cards"] if pending is not None else []) + (["play_card"] if playable else [])
    if pending is None and not combat.is_over:
        legal_kinds.append("end_turn")
    return {
        "turn": int(combat.turn_count),
        "player": {
            "hp": int(player.current_hp),
            "max_hp": int(player.max_hp),
            "block": int(player.block),
            "energy": int(combat.energy),
            "max_energy": int(combat.max_energy),
            "powers": player_powers,
        },
        "enemies": enemies,
        "piles": {
            "hand": _card_ids(combat.hand),
            "draw": _card_ids(combat.draw_pile),
            "discard": _card_ids(combat.discard_pile),
            "exhaust": _card_ids(combat.exhaust_pile),
            "play": _card_ids(combat.play_pile),
        },
        "legal_action_kinds": sorted(legal_kinds),
        "pending_choice": pending_choice,
        "terminated": bool(combat.is_over),
        "victory": bool(combat.player_won),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--register-powers", action="store_true")
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="bygone_three_strike_turn")
    args = parser.parse_args()
    if args.register_powers:
        import sts2_env.powers  # noqa: F401 - required registry side effect in the external package
    seed_text = f"SHADOW-MATRIX-{args.scenario}"
    scenario = SCENARIOS[args.scenario]
    instance_ids = list(range(1, len(scenario["cards"]) + 1))
    iterator = CombatIterator({
        "scenario_id": args.scenario,
        "seed": seed_text,
        "character_id": "Ironclad",
        "encounter_id": (
            "act3:setup_axebots_normal"
            if scenario.get("encounter") == "AXEBOTS_NORMAL"
            else (
                "act2:setup_bowlbugs_normal"
                if scenario.get("encounter") == "BOWLBUGS_NORMAL"
                else (
                    "act4:setup_seapunk_normal"
                    if scenario.get("encounter") == "SEAPUNK_NORMAL"
                    else (
                        "act3:setup_aeonglass_boss"
                        if scenario.get("encounter") == "AEONGLASS_BOSS"
                        else "act1:setup_bygone_effigy_elite"
                    )
                )
            )
        ),
        "current_hp": 80,
        "max_hp": 80,
        "deck": [
            {"model_id": card_id.name, "instance_id": instance_id}
            for card_id, instance_id in zip(scenario["cards"], instance_ids)
        ],
        "initial_hand_instance_ids": instance_ids,
    })
    combat = iterator.combat
    checkpoints = {"initial": snapshot(combat)}
    expected_hand = [card_id.name for card_id in scenario["cards"]]
    if checkpoints["initial"]["piles"]["hand"] != expected_hand:
        raise RuntimeError(f"unexpected initial hand: {checkpoints['initial']['piles']['hand']}")
    for checkpoint, kind, card_id, target_index in scenario["actions"]:
        if kind == "end_turn":
            combat.end_player_turn()
        elif kind == "choose":
            for option_index in card_id:
                if not combat.resolve_pending_choice(option_index):
                    raise RuntimeError(f"shadow simulator rejected choice option {option_index}")
            if not combat.resolve_pending_choice(None):
                raise RuntimeError("shadow simulator rejected choice confirmation")
        else:
            hand_index = next((index for index, card in enumerate(combat.hand) if card.card_id == card_id), None)
            if hand_index is None or not combat.play_card(hand_index, target_index):
                raise RuntimeError(f"shadow simulator rejected {card_id.name}")
        checkpoints[checkpoint] = snapshot(combat)
    print(json.dumps({
        "adapter_schema": 1,
        "scenario_id": args.scenario,
        "power_registry_bootstrapped": args.register_powers,
        "checkpoints": checkpoints,
    }, separators=(",", ":")))


if __name__ == "__main__":
    main()
