"""Acceptance checks for upgraded cards, native potions, and exact replay."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sts2_native_sim import NativeWorkerPool


SCENARIO = {
    "game_build": {},
    "seed": "NATIVE-COMBAT-BREADTH",
    "rng_counters": {"shuffle": 17, "combat_card_generation": 9},
    "character": "IRONCLAD",
    "ascension": 0,
    "encounter": "first",
    "current_hp": 80,
    "max_hp": 80,
    "gold": 99,
    "deck": [
        {"instance_id": "strike-upgraded", "model_id": "STRIKE_IRONCLAD", "upgrades": 1, "enchantment": {"model_id": "SHARP", "amount": 2}},
        {"instance_id": "defend-0", "model_id": "DEFEND_IRONCLAD"},
        {"instance_id": "genetic-0", "model_id": "GENETIC_ALGORITHM", "native_state": {"CurrentBlock": 7, "IncreasedBlock": 3}},
    ],
    "initial_hand": ["strike-upgraded", "defend-0"],
    "relics": [{"model_id": "PEN_NIB", "counter": 7}],
    "potions": [
        {"model_id": "ENERGY_POTION", "slot": 0},
        {"model_id": "FIRE_POTION", "slot": 1},
    ],
}

MULTI_SCENARIO = {
    **SCENARIO,
    "seed": "NATIVE-MULTI-TERMINAL",
    "rng_counters": {},
    "encounter": "BOWLBUGS_WEAK",
    "energy": 99,
    "deck": [{"instance_id": f"strike-{i}", "model_id": "STRIKE_IRONCLAD", "upgrades": 1} for i in range(10)],
    "initial_hand": [f"strike-{i}" for i in range(10)],
    "relics": [],
    "potions": [],
}

ENTRY_HOOK_SCENARIO = {
    **SCENARIO,
    "seed": "NATIVE-COMBAT-ENTRY-HOOKS",
    "rng_counters": {},
    "relics": [{"model_id": "GORGET"}],
    "potions": [],
    "invoke_combat_entry_hooks": True,
}

ORB_SCENARIO = {
    **SCENARIO,
    "seed": "NATIVE-ORB-LIFECYCLE",
    "rng_counters": {"combat_targets": 0},
    "character": "DEFECT",
    "deck": [
        *({"instance_id": f"strike-{i}", "model_id": "STRIKE_DEFECT"} for i in range(5)),
        *({"instance_id": f"defend-{i}", "model_id": "DEFEND_DEFECT"} for i in range(5)),
    ],
    "initial_hand": ["defend-0", "defend-1", "defend-2", "defend-3", "defend-4"],
    "relics": [{"model_id": "CRACKED_CORE"}],
    "potions": [],
    "invoke_combat_entry_hooks": True,
    "capture_orbs": True,
}


def pile_cards(state):
    return [card for pile in state["observation"]["combat"]["piles"] for card in pile["cards"]]


def potion_actions(state, slot, kind="use_potion"):
    return [a for a in state["legal_actions"] if a["kind"] == kind and a["parameters"]["slot"] == slot]


def main() -> None:
    with NativeWorkerPool(4) as pool:
        initial = pool.reset_all(SCENARIO)
        assert len({state["state_hash"] for state in initial}) == 1
        assert all(next(c for c in pile_cards(state) if c["instance_id"] == "strike-upgraded")["upgrades"] == 1 for state in initial)
        assert all(next(c for c in pile_cards(state) if c["instance_id"] == "strike-upgraded")["enchantment"] == {"model_id": "SHARP", "amount": 2} for state in initial)
        assert all(next(c for c in pile_cards(state) if c["instance_id"] == "genetic-0")["native_state"] == {"CurrentBlock": 7, "IncreasedBlock": 3} for state in initial)
        assert all(len(potion_actions(state, 0)) == 1 for state in initial)
        assert all(len(potion_actions(state, 1)) == 1 and potion_actions(state, 1)[0]["parameters"]["target_id"] is not None for state in initial)
        assert all(len(potion_actions(state, 0, "discard_potion")) == 1 for state in initial)
        assert all(state["observation"]["run"]["rng_counters"]["Shuffle"] == 17 for state in initial)
        assert all(state["observation"]["run"]["rng_counters"]["CombatCardGeneration"] == 9 for state in initial)
        assert all(state["observation"]["inventory"]["relics"][0]["counter"] == 7 for state in initial)
        assert all(state["observation"]["inventory"]["relics"][0]["native_state"]["AttacksPlayed"] == 7 for state in initial)

        initial_handle = initial[0]["state_handle"]
        initial_energy = initial[0]["observation"]["combat"]["energy"]
        energy_action = potion_actions(initial[0], 0)[0]["action_id"]
        after_energy = pool.map(lambda worker, action: worker.step(action), [energy_action] * 4)
        assert len({state["state_hash"] for state in after_energy}) == 1
        assert all(state["observation"]["combat"]["energy"] > initial_energy for state in after_energy)
        assert all(state["observation"]["inventory"]["potions"][0] is None for state in after_energy)

        worker = pool.workers[0]
        restored = worker.restore(initial_handle)
        assert restored["state_hash"] == initial[0]["state_hash"]
        enemy_before = next(c for c in restored["observation"]["combat"]["creatures"] if c["side"] == "Enemy")["hp"]
        fire_action = potion_actions(restored, 1)[0]["action_id"]
        after_fire = worker.step(fire_action)
        enemy_after = next(c for c in after_fire["observation"]["combat"]["creatures"] if c["side"] == "Enemy")["hp"]
        assert enemy_after < enemy_before
        fire_handle = after_fire["state_handle"]
        worker.restore(initial_handle)
        assert worker.step(fire_action)["state_hash"] == worker.restore(fire_handle)["state_hash"]

        worker.restore(initial_handle)
        discard_action = potion_actions(worker.observe(), 0, "discard_potion")[0]["action_id"]
        discarded = worker.step(discard_action)
        assert discarded["observation"]["inventory"]["potions"][0] is None
        assert discarded["observation"]["combat"]["energy"] == initial_energy

        worker.restore(initial_handle)
        strike_action = next(a["action_id"] for a in worker.observe()["legal_actions"] if a["kind"] == "play_card" and a["parameters"]["instance_id"] == "strike-upgraded")
        after_strike = worker.step(strike_action)
        assert after_strike["observation"]["inventory"]["relics"][0]["counter"] == 8

        multi = pool.reset_all(MULTI_SCENARIO)
        assert all(len([c for c in s["observation"]["combat"]["creatures"] if c["side"] == "Enemy"]) == 2 for s in multi)
        assert all(len({a["parameters"]["target_id"] for a in s["legal_actions"] if a["kind"] == "play_card"}) == 2 for s in multi)
        death_observed = False
        previous_enemy_count = 2
        while not multi[0]["terminated"]:
            enemies = [c for c in multi[0]["observation"]["combat"]["creatures"] if c["side"] == "Enemy" and c["alive"]]
            target = enemies[0]["combat_id"]
            action = next(a["action_id"] for a in multi[0]["legal_actions"] if a["kind"] == "play_card" and a["parameters"]["target_id"] == target)
            multi = pool.map(lambda target_worker, action_id: target_worker.step(action_id), [action] * 4)
            assert len({s["state_hash"] for s in multi}) == 1
            current_enemy_count = len([c for c in multi[0]["observation"]["combat"]["creatures"] if c["side"] == "Enemy"])
            if current_enemy_count < previous_enemy_count or any(c["side"] == "Enemy" and not c["alive"] for c in multi[0]["observation"]["combat"]["creatures"]):
                death_observed = True
            previous_enemy_count = current_enemy_count
        assert death_observed and all(s["victory"] and not s["legal_actions"] for s in multi)

        entry_hooks = pool.reset_all(ENTRY_HOOK_SCENARIO)
        assert len({state["state_hash"] for state in entry_hooks}) == 1
        assert all(
            any(
                power["model_id"] == "PLATING_POWER" and power["amount"] == 4
                for power in state["observation"]["combat"]["creatures"][0]["powers"]
            )
            for state in entry_hooks
        )

        orb_initial = pool.reset_all(ORB_SCENARIO)
        assert len({state["state_hash"] for state in orb_initial}) == 1
        assert all(state["observation"]["combat"]["orbs"] == {
            "capacity": 3,
            "entries": [{"model_id": "LIGHTNING_ORB", "passive": 3, "evoke": 8, "native_state": {}}],
        } for state in orb_initial)
        enemy_hp = next(c["hp"] for c in orb_initial[0]["observation"]["combat"]["creatures"] if c["side"] == "Enemy")
        orb_after_turn = pool.map(lambda worker, _: worker.step("end_turn"), [None] * 4)
        assert len({state["state_hash"] for state in orb_after_turn}) == 1
        assert all(state["observation"]["run"]["rng_counters"]["CombatTargets"] == 1 for state in orb_after_turn)
        assert all(next(c["hp"] for c in state["observation"]["combat"]["creatures"] if c["side"] == "Enemy") == enemy_hp - 3 for state in orb_after_turn)

        print(json.dumps({
            "success": True,
            "workers": 4,
            "initial_hash": initial[0]["state_hash"],
            "energy_hash": after_energy[0]["state_hash"],
            "fire_hash": after_fire["state_hash"],
            "enemy_damage": enemy_before - enemy_after,
            "terminal_hash": multi[0]["state_hash"],
            "entry_hook_hash": entry_hooks[0]["state_hash"],
            "orb_turn_hash": orb_after_turn[0]["state_hash"],
        }, indent=2))


if __name__ == "__main__":
    main()
