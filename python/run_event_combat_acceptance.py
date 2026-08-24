"""Four-worker native map -> event -> nested combat -> map acceptance."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from acceptance import SCENARIO
from run_composed_utility_rooms_acceptance import finish_combat, step_all
from sts2_native_sim import NativeWorkerPool


def main() -> None:
    scenario = copy.deepcopy(SCENARIO)
    scenario.update({
        "seed": "EVENT-COMBAT-SEED-51",
        "current_hp": 9999,
        "max_hp": 9999,
        "deck": [{"instance_id": f"bludgeon-{index}", "model_id": "BLUDGEON", "upgrades": 1} for index in range(10)],
        "initial_hand": [],
    })
    with NativeWorkerPool(4) as pool:
        states = pool.map(lambda worker, reset: worker.run_reset(reset), [scenario] * 4)
        points = {(p["coord"]["col"], p["coord"]["row"]): p for p in states[0]["observation"]["map"]["points"]}
        start, unknown = next(
            (action, child)
            for action in states[0]["legal_actions"]
            for child in points[(action["parameters"]["col"], action["parameters"]["row"])]["children"]
            if points[(child["col"], child["row"])]["point_type"] == "Unknown"
        )
        states = step_all(pool, states, [start["action_id"]] * 4)
        states = finish_combat(pool, states)
        event_action = next(a["action_id"] for a in states[0]["legal_actions"] if a["parameters"]["col"] == unknown["col"] and a["parameters"]["row"] == unknown["row"])
        states = step_all(pool, states, [event_action] * 4)
        assert states[0]["observation"]["event"]["model_id"] == "DENSE_VEGETATION"

        rest = next(a["action_id"] for a in states[0]["legal_actions"] if a["parameters"]["text_key"].endswith(".REST"))
        states = step_all(pool, states, [rest] * 4)
        fight = states[0]["legal_actions"][0]["action_id"]
        states = step_all(pool, states, [fight] * 4)
        assert states[0]["observation"]["decision"]["kind"] == "combat_action"
        assert states[0]["observation"]["combat"]["turn"] == 1
        nested_hash = states[0]["state_hash"]
        nested_handle = states[0]["state_handle"]

        states = finish_combat(pool, states)
        assert states[0]["observation"]["decision"]["kind"] == "map_choice"
        assert len(states[0]["observation"]["map"]["visited"]) == 2
        returned_hash = states[0]["state_hash"]
        assert pool.workers[0].restore(nested_handle)["state_hash"] == nested_hash

        print(json.dumps({
            "success": True,
            "workers": 4,
            "event_id": "DENSE_VEGETATION",
            "nested_combat_hash": nested_hash,
            "returned_map_hash": returned_hash,
        }, indent=2))


if __name__ == "__main__":
    main()
