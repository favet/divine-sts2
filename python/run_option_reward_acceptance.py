"""Replayable four-worker acceptance for a native relic-triggered option choice."""
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
    scenario["seed"] = "NATIVE-SCROLL-BOXES-REWARD"
    with NativeWorkerPool(4) as pool:
        rewards = pool.map(
            lambda worker, state: worker.item_reward_reset(state, "relic", "SCROLL_BOXES"),
            [copy.deepcopy(scenario) for _ in range(4)],
        )
        assert all(state["observation"]["reward"]["options"][0]["model_id"] == "SCROLL_BOXES" for state in rewards)
        reward_actions = [next(action["action_id"] for action in state["legal_actions"] if not action["parameters"]["skip"]) for state in rewards]
        pending = pool.map(lambda worker, action: worker.reward_step(action), reward_actions)
        assert len({state["state_hash"] for state in pending}) == 1
        assert all(state["observation"]["decision"]["kind"] == "option_choice" for state in pending)
        assert all(len(state["legal_actions"]) == 2 and all(action["kind"] == "choose_option" for action in state["legal_actions"]) for state in pending)
        assert len({json.dumps(state["legal_actions"], sort_keys=True) for state in pending}) == 1
        portable = pool.workers[0].export_branch()
        portable_pending = pool.restore_portable(1, portable)
        assert portable_pending["state_hash"] == pending[0]["state_hash"]
        assert portable_pending["observation"]["decision"]["kind"] == "option_choice"

        pending_handles = [state["state_handle"] for state in pending]
        first = pending[0]["legal_actions"][0]["action_id"]
        second = pending[0]["legal_actions"][1]["action_id"]
        selected = pool.map(lambda worker, action: worker.reward_step(action), [first] * 4)
        assert len({state["state_hash"] for state in selected}) == 1
        assert all(state["observation"]["decision"]["kind"] == "reward_complete" for state in selected)
        assert all(len(state["observation"]["run"]["deck"]) == len(scenario["deck"]) + 3 for state in selected)

        restored_pending = pool.map(lambda worker, handle: worker.restore(handle), pending_handles)
        assert len({state["state_hash"] for state in restored_pending}) == 1
        assert restored_pending[0]["state_hash"] == pending[0]["state_hash"]
        assert all(state["observation"]["decision"]["kind"] == "option_choice" for state in restored_pending)
        replayed = pool.map(lambda worker, action: worker.reward_step(action), [first] * 4)
        assert len({state["state_hash"] for state in replayed}) == 1
        assert replayed[0]["state_hash"] == selected[0]["state_hash"]

        worker = pool.workers[0]
        worker.restore(pending_handles[0])
        alternate = worker.reward_step(second)
        assert alternate["state_hash"] != selected[0]["state_hash"]
        assert len(alternate["observation"]["run"]["deck"]) == len(scenario["deck"]) + 3

        print(json.dumps({
            "success": True,
            "workers": 4,
            "pending_hash": pending[0]["state_hash"],
            "selected_hash": selected[0]["state_hash"],
            "alternate_hash": alternate["state_hash"],
            "bundle_actions": len(pending[0]["legal_actions"]),
            "final_card_count": len(selected[0]["observation"]["run"]["deck"]),
        }, indent=2))


if __name__ == "__main__":
    main()
