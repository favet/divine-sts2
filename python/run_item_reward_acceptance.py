"""Four-worker acceptance for native relic and potion rewards."""
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
    scenario["seed"] = "NATIVE-ITEM-REWARD-COORDINATOR"
    results = {}
    with NativeWorkerPool(4) as pool:
        for kind, inventory_key in (("relic", "relics"), ("potion", "potions")):
            initial = pool.map(lambda worker, reset: worker.item_reward_reset(reset, kind), [scenario] * 4)
            assert len({state["state_hash"] for state in initial}) == 1
            assert len({json.dumps(state["legal_actions"], sort_keys=True) for state in initial}) == 1
            assert all(len(state["observation"]["reward"]["options"]) == 1 for state in initial)
            handle = initial[0]["state_handle"]
            before = initial[0]["observation"]["run"][inventory_key]
            pick = next(action["action_id"] for action in initial[0]["legal_actions"] if not action["parameters"]["skip"])
            selected = pool.map(lambda worker, action: worker.reward_step(action), [pick] * 4)
            assert len({state["state_hash"] for state in selected}) == 1
            assert all(state["observation"]["decision"]["kind"] == "reward_complete" for state in selected)
            after = selected[0]["observation"]["run"][inventory_key]
            assert sum(item is not None for item in after) == sum(item is not None for item in before) + 1
            worker = pool.workers[0]
            assert worker.restore(handle)["state_hash"] == initial[0]["state_hash"]
            assert worker.restore(selected[0]["state_handle"])["state_hash"] == selected[0]["state_hash"]
            results[kind] = {"initial_hash": initial[0]["state_hash"], "selected_hash": selected[0]["state_hash"], "option": initial[0]["observation"]["reward"]["options"][0]}
    print(json.dumps({"success": True, "workers": 4, "rewards": results}, indent=2))


if __name__ == "__main__":
    main()
