"""Native blocking card-choice, continuation, fork, replay, and worker determinism test."""
from __future__ import annotations
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from sts2_native_sim import NativeWorkerPool

DECK = [
    {"instance_id": "acrobatics-0", "model_id": "ACROBATICS"},
    *({"instance_id": f"strike-{i}", "model_id": "STRIKE_IRONCLAD"} for i in range(5)),
    *({"instance_id": f"defend-{i}", "model_id": "DEFEND_IRONCLAD"} for i in range(5)),
]
SCENARIO = {
    "game_build": {}, "seed": "NATIVE-CHOICE", "rng_counters": {}, "character": "IRONCLAD", "ascension": 0,
    "encounter": "first", "current_hp": 80, "max_hp": 80, "gold": 99, "deck": DECK,
    "initial_hand": ["acrobatics-0", "strike-0", "defend-0"], "relics": [], "potions": []
}
PURITY_DECK = [
    {"instance_id": "purity-0", "model_id": "PURITY"},
    {"instance_id": "strike-0", "model_id": "STRIKE_IRONCLAD"},
    {"instance_id": "defend-0", "model_id": "DEFEND_IRONCLAD"},
    {"instance_id": "strike-1", "model_id": "STRIKE_IRONCLAD"},
]
PURITY_SCENARIO = {**SCENARIO, "seed": "NATIVE-MULTI-CHOICE", "deck": PURITY_DECK, "initial_hand": [c["instance_id"] for c in PURITY_DECK]}
DISCOVERY_DECK = [
    {"instance_id": "discovery-0", "model_id": "DISCOVERY"},
    {"instance_id": "strike-0", "model_id": "STRIKE_IRONCLAD"},
    {"instance_id": "defend-0", "model_id": "DEFEND_IRONCLAD"},
]
DISCOVERY_SCENARIO = {**SCENARIO, "seed": "NATIVE-GENERATED-OPTIONS", "deck": DISCOVERY_DECK, "initial_hand": ["discovery-0"]}

def main():
    with NativeWorkerPool(4) as pool:
        initial = pool.reset_all(SCENARIO)
        plays = [next(a["action_id"] for a in state["legal_actions"] if a["parameters"].get("instance_id") == "acrobatics-0") for state in initial]
        choices = pool.map(lambda worker, action: worker.step(action), plays)
        assert len({state["state_hash"] for state in choices}) == 1
        assert all(state["observation"]["decision"]["kind"] == "card_choice" for state in choices)
        assert all(state["legal_actions"] and all(a["kind"] == "choose_cards" for a in state["legal_actions"]) for state in choices)

        # Abandon an actively suspended continuation, then reconstruct it.
        pool.workers[3].restore(initial[3]["state_handle"])
        rebuilt_pending = pool.workers[3].restore(choices[3]["state_handle"])
        assert rebuilt_pending["state_hash"] == choices[3]["state_hash"]

        choice_handle = choices[0]["state_handle"]
        first_action = choices[0]["legal_actions"][0]["action_id"]
        second_action = choices[0]["legal_actions"][1]["action_id"]
        continued = pool.map(lambda worker, action: worker.step(action), [first_action] * 4)
        assert len({state["state_hash"] for state in continued}) == 1
        assert all(state["observation"]["decision"]["kind"] == "combat_action" for state in continued)

        worker = pool.workers[0]
        restored = worker.restore(choice_handle)
        assert restored["state_hash"] == choices[0]["state_hash"]
        alternate = worker.step(second_action)
        assert alternate["state_hash"] != continued[0]["state_hash"]
        total = sum(len(p["cards"]) for p in alternate["observation"]["combat"]["piles"])
        assert total == len(DECK)

        purity = worker.reset(PURITY_SCENARIO)
        purity_play = next(a["action_id"] for a in purity["legal_actions"] if a["parameters"].get("instance_id") == "purity-0")
        purity_choice = worker.step(purity_play)
        assert len(purity_choice["legal_actions"]) == 8  # every subset of three cards, including skip
        purity_handle = purity_choice["state_handle"]
        skipped = worker.step(purity_choice["legal_actions"][0]["action_id"])
        worker.restore(purity_handle)
        multi = next(a for a in purity_choice["legal_actions"] if len(a["parameters"]["option_ids"]) == 2)
        selected_multiple = worker.step(multi["action_id"])
        assert skipped["state_hash"] != selected_multiple["state_hash"]

        discovery_initial = pool.reset_all(DISCOVERY_SCENARIO)
        discovery_plays = [next(a["action_id"] for a in state["legal_actions"] if a["parameters"].get("instance_id") == "discovery-0") for state in discovery_initial]
        generated_choices = pool.map(lambda target, action: target.step(action), discovery_plays)
        assert len({state["state_hash"] for state in generated_choices}) == 1
        generated_ids = [[o["option_id"] for o in state["observation"]["outstanding_choice"]["options"]] for state in generated_choices]
        assert len({json.dumps(ids) for ids in generated_ids}) == 1 and all(i.startswith("generated-") for i in generated_ids[0])
        generated_action = next(a["action_id"] for a in generated_choices[0]["legal_actions"] if a["parameters"]["option_ids"])
        generated_continuations = pool.map(lambda target, action: target.step(action), [generated_action] * 4)
        assert len({state["state_hash"] for state in generated_continuations}) == 1

        print(json.dumps({"success": True, "workers": 4, "choice_hash": choices[0]["state_hash"], "continued_hash": continued[0]["state_hash"], "alternate_hash": alternate["state_hash"], "single_choice_actions": len(choices[0]["legal_actions"]), "multi_choice_actions": len(purity_choice["legal_actions"]), "generated_option_ids": generated_ids[0], "total_cards": total}, indent=2))

if __name__ == "__main__": main()
