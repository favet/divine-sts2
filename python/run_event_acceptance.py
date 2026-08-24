"""Four-worker acceptance for native event initialization, choices, and replay."""
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
    scenario.update({"seed": "NATIVE-RUN-EVENT-COORDINATOR", "current_hp": 70, "max_hp": 80, "gold": 50})
    event_id = "THIS_OR_THAT"
    with NativeWorkerPool(4) as pool:
        initial = pool.map(lambda worker, reset: worker.event_reset(reset, event_id), [scenario] * 4)
        assert len({state["state_hash"] for state in initial}) == 1
        assert len({json.dumps(state["legal_actions"], sort_keys=True) for state in initial}) == 1
        assert initial[0]["observation"]["event"]["model_id"] == event_id
        assert len(initial[0]["legal_actions"]) == 2
        initial_handle = initial[0]["state_handle"]

        plain = next(action["action_id"] for action in initial[0]["legal_actions"] if action["parameters"]["text_key"].endswith(".PLAIN"))
        chosen = pool.map(lambda worker, action: worker.event_step(action), [plain] * 4)
        assert len({state["state_hash"] for state in chosen}) == 1
        assert all(state["observation"]["decision"]["kind"] == "event_complete" for state in chosen)
        assert all(state["observation"]["run"]["current_hp"] == 64 for state in chosen)
        assert all(state["observation"]["run"]["gold"] > 50 for state in chosen)

        worker = pool.workers[0]
        chosen_handle = chosen[0]["state_handle"]
        assert worker.restore(initial_handle)["state_hash"] == initial[0]["state_hash"]
        assert worker.restore(chosen_handle)["state_hash"] == chosen[0]["state_hash"]

        print(json.dumps({
            "success": True,
            "workers": 4,
            "event_id": event_id,
            "initial_hash": initial[0]["state_hash"],
            "chosen_hash": chosen[0]["state_hash"],
            "hp_before": 70,
            "hp_after": chosen[0]["observation"]["run"]["current_hp"],
            "gold_before": 50,
            "gold_after": chosen[0]["observation"]["run"]["gold"],
        }, indent=2))


if __name__ == "__main__":
    main()
