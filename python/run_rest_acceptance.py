"""Four-worker acceptance for native rest-site options and Smith continuation."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from acceptance import SCENARIO
from sts2_native_sim import NativeWorkerPool


def main() -> None:
    scenario = copy.deepcopy(SCENARIO)
    scenario.update({"seed": "NATIVE-RUN-REST-COORDINATOR", "current_hp": 40})
    with NativeWorkerPool(4) as pool:
        initial = pool.map(lambda worker, reset: worker.rest_reset(reset), [scenario] * 4)
        assert len({state["state_hash"] for state in initial}) == 1
        assert len({json.dumps(state["legal_actions"], sort_keys=True) for state in initial}) == 1
        assert {option["option_id"] for option in initial[0]["observation"]["rest_site"]["options"]} >= {"HEAL", "SMITH"}
        initial_handle = initial[0]["state_handle"]

        heal = next(action["action_id"] for action in initial[0]["legal_actions"] if action["parameters"]["option_id"] == "HEAL")
        healed = pool.map(lambda worker, action: worker.rest_step(action), [heal] * 4)
        assert len({state["state_hash"] for state in healed}) == 1
        assert all(state["observation"]["run"]["current_hp"] > 40 for state in healed)
        assert all(state["observation"]["decision"]["kind"] == "rest_complete" for state in healed)

        worker = pool.workers[0]
        restored = worker.restore(initial_handle)
        smith = next(action["action_id"] for action in restored["legal_actions"] if action["parameters"]["option_id"] == "SMITH")
        choosing = worker.rest_step(smith)
        assert choosing["observation"]["decision"]["kind"] == "card_choice"
        choice = next(action for action in choosing["legal_actions"] if len(action["parameters"]["option_ids"]) == 1)
        smithed = worker.rest_step(choice["action_id"])
        assert smithed["observation"]["decision"]["kind"] == "rest_complete"
        assert sum(card["upgrades"] for card in smithed["observation"]["run"]["deck"]) == 1
        smithed_handle = smithed["state_handle"]
        worker.restore(initial_handle)
        assert worker.restore(smithed_handle)["state_hash"] == smithed["state_hash"]

        print(json.dumps({
            "success": True,
            "workers": 4,
            "initial_hash": initial[0]["state_hash"],
            "healed_hash": healed[0]["state_hash"],
            "smithed_hash": smithed["state_hash"],
            "hp_before": 40,
            "hp_after": healed[0]["observation"]["run"]["current_hp"],
            "smith_choice_actions": len(choosing["legal_actions"]),
        }, indent=2))


if __name__ == "__main__":
    main()
