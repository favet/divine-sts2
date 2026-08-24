"""Four-worker acceptance for deterministic shipped native map routing."""
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
    scenario["seed"] = "NATIVE-RUN-MAP-COORDINATOR"
    with NativeWorkerPool(4) as pool:
        states = pool.map(lambda worker, reset: worker.map_reset(reset), [scenario] * 4)
        assert len({state["state_hash"] for state in states}) == 1
        assert len({json.dumps(state["observation"]["map"], sort_keys=True) for state in states}) == 1
        assert all(state["legal_actions"] for state in states)
        initial_hash = states[0]["state_hash"]
        mid_handle = None
        selected: list[str] = []
        while states[0]["legal_actions"]:
            action = states[0]["legal_actions"][0]["action_id"]
            selected.append(action)
            states = pool.map(lambda worker, action_id: worker.map_step(action_id), [action] * 4)
            assert len({state["state_hash"] for state in states}) == 1
            assert len({json.dumps(state["legal_actions"], sort_keys=True) for state in states}) == 1
            if len(selected) == 4:
                mid_handle = states[0]["state_handle"]
        assert mid_handle is not None
        final_hash = states[0]["state_hash"]
        restored = pool.workers[0].restore(mid_handle)
        assert restored["state_hash"] != final_hash
        for action in selected[4:]:
            restored = pool.workers[0].map_step(action)
        assert restored["state_hash"] == final_hash
        print(json.dumps({
            "success": True,
            "workers": 4,
            "initial_hash": initial_hash,
            "final_hash": final_hash,
            "path_length": len(selected),
            "selected_path": selected,
            "map_points": len(states[0]["observation"]["map"]["points"]),
        }, indent=2))


if __name__ == "__main__":
    main()
