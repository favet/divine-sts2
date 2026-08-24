"""Four-worker acceptance for native custom, multi, linked, and blocking rewards."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from acceptance import SCENARIO
from sts2_native_sim import NativeWorkerPool


def step_all(pool: NativeWorkerPool, actions: list[str], *, event: bool = False, custom: bool = False) -> list[dict]:
    if event:
        result = pool.map(lambda worker, action: worker.event_step(action), actions)
    elif custom:
        result = pool.map(lambda worker, action: worker.custom_reward_step(action), actions)
    else:
        raise AssertionError("step mode required")
    assert len({state["state_hash"] for state in result}) == 1
    return result


def main() -> None:
    with NativeWorkerPool(4) as pool:
        ordinary = pool.map(lambda worker, state: worker.event_reset(state, "THE_LEGENDS_WERE_TRUE"), [copy.deepcopy(SCENARIO) for _ in range(4)])
        offer_actions = [state["legal_actions"][1]["action_id"] for state in ordinary]
        ordinary_offer = step_all(pool, offer_actions, event=True)
        assert ordinary_offer[0]["observation"]["decision"]["kind"] == "custom_reward_choice"
        ordinary_handle = ordinary_offer[0]["state_handle"]
        potion_actions = [next(a["action_id"] for a in state["legal_actions"] if a["kind"] == "choose_custom_reward") for state in ordinary_offer]
        ordinary_done = step_all(pool, potion_actions, event=True)
        assert ordinary_done[0]["observation"]["event"]["finished"]

        trial_state = copy.deepcopy(SCENARIO)
        trial_state["seed"] = "TRIAL-CUSTOM-1"
        trial = pool.map(lambda worker, state: worker.event_reset(state, "TRIAL"), [trial_state] * 4)
        trial = step_all(pool, [state["legal_actions"][0]["action_id"] for state in trial], event=True)
        assert ".NONDESCRIPT." in trial[0]["observation"]["event"]["options"][0]["text_key"]
        trial_offer = step_all(pool, [state["legal_actions"][0]["action_id"] for state in trial], event=True)
        assert len(trial_offer[0]["observation"]["outstanding_rewards"]["rewards"]) == 2
        first_cards = [next(a["action_id"] for a in state["legal_actions"] if a["parameters"].get("reward_index") == 0 and a["parameters"].get("option_index") == 0) for state in trial_offer]
        trial_half = step_all(pool, first_cards, event=True)
        trial_half_handle = trial_half[0]["state_handle"]
        second_cards = [next(a["action_id"] for a in state["legal_actions"] if a["parameters"].get("reward_index") == 1 and a["parameters"].get("option_index") == 0) for state in trial_half]
        trial_done = step_all(pool, second_cards, event=True)
        assert trial_done[0]["observation"]["event"]["finished"]
        assert len(trial_done[0]["observation"]["run"]["deck"]) == 13

        linked = pool.map(lambda worker, state: worker.custom_reward_reset(state, ["card_removal", "potion"], True), [copy.deepcopy(SCENARIO) for _ in range(4)])
        linked_initial_hash = linked[0]["state_hash"]
        linked_handle = linked[0]["state_handle"]
        removal_actions = [next(a["action_id"] for a in state["legal_actions"] if a["parameters"].get("reward_kind") == "cardremoval") for state in linked]
        linked_choices = step_all(pool, removal_actions, custom=True)
        assert linked_choices[0]["observation"]["decision"]["kind"] == "card_choice"
        linked_choice_handle = linked_choices[0]["state_handle"]
        card_actions = [state["legal_actions"][0]["action_id"] for state in linked_choices]
        linked_done = step_all(pool, card_actions, custom=True)
        assert linked_done[0]["observation"]["decision"]["kind"] == "custom_reward_complete"
        assert len(linked_done[0]["observation"]["run"]["deck"]) == 9

        worker = pool.workers[0]
        assert worker.restore(ordinary_handle)["state_hash"] == ordinary_offer[0]["state_hash"]
        assert worker.restore(trial_half_handle)["state_hash"] == trial_half[0]["state_hash"]
        assert worker.restore(linked_choice_handle)["state_hash"] == linked_choices[0]["state_hash"]
        restored_linked = worker.restore(linked_handle)
        assert restored_linked["state_hash"] == linked_initial_hash
        potion = next(a["action_id"] for a in restored_linked["legal_actions"] if a["parameters"].get("reward_kind") == "potion")
        potion_done = worker.custom_reward_step(potion)
        assert potion_done["observation"]["decision"]["kind"] == "custom_reward_complete"
        assert any(slot is not None for slot in potion_done["observation"]["run"]["potions"])

        print(json.dumps({
            "success": True,
            "workers": 4,
            "ordinary_offer_hash": ordinary_offer[0]["state_hash"],
            "trial_half_hash": trial_half[0]["state_hash"],
            "trial_done_hash": trial_done[0]["state_hash"],
            "linked_initial_hash": linked_initial_hash,
            "linked_choice_hash": linked_choices[0]["state_hash"],
            "linked_done_hash": linked_done[0]["state_hash"],
            "linked_alternate_potion_hash": potion_done["state_hash"],
        }, indent=2))


if __name__ == "__main__":
    main()
