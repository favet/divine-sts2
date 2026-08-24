"""Four-worker acceptance for native card reward generation and selection."""
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
    scenario["seed"] = "NATIVE-RUN-REWARD-COORDINATOR"
    with NativeWorkerPool(4) as pool:
        initial = pool.map(lambda worker, reset: worker.reward_reset(reset), [scenario] * 4)
        assert len({state["state_hash"] for state in initial}) == 1
        assert len({json.dumps(state["legal_actions"], sort_keys=True) for state in initial}) == 1
        assert all(len(state["observation"]["reward"]["options"]) == 3 for state in initial)
        assert all(len(state["legal_actions"]) == 4 for state in initial)
        initial_handle = initial[0]["state_handle"]
        initial_deck_count = len(initial[0]["observation"]["run"]["deck"])

        pick = initial[0]["legal_actions"][0]["action_id"]
        selected = pool.map(lambda worker, action: worker.reward_step(action), [pick] * 4)
        assert len({state["state_hash"] for state in selected}) == 1
        assert all(state["observation"]["decision"]["kind"] == "reward_complete" for state in selected)
        assert all(len(state["observation"]["run"]["deck"]) == initial_deck_count + 1 for state in selected)
        selected_handle = selected[0]["state_handle"]

        worker = pool.workers[0]
        restored_initial = worker.restore(initial_handle)
        skip = next(action["action_id"] for action in restored_initial["legal_actions"] if action["parameters"]["skip"])
        skipped = worker.reward_step(skip)
        assert skipped["observation"]["decision"]["kind"] == "reward_complete"
        assert len(skipped["observation"]["run"]["deck"]) == initial_deck_count
        assert worker.restore(selected_handle)["state_hash"] == selected[0]["state_hash"]

        print(json.dumps({
            "success": True,
            "workers": 4,
            "initial_hash": initial[0]["state_hash"],
            "selected_hash": selected[0]["state_hash"],
            "skipped_hash": skipped["state_hash"],
            "options": initial[0]["observation"]["reward"]["options"],
            "deck_count_before": initial_deck_count,
            "deck_count_after_pick": initial_deck_count + 1,
        }, indent=2))


if __name__ == "__main__":
    main()
