"""Four-worker acceptance for map, combat, rewards, room exit, and map return."""
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
    scenario.update({
        "seed": "NATIVE-COMPOSED-ROOM-CYCLE",
        "deck": [{"instance_id": f"bludgeon-{index}", "model_id": "BLUDGEON", "upgrades": 1} for index in range(10)],
        "initial_hand": [],
        "potions": [{"model_id": "FIRE_POTION", "slot": 0}],
    })
    with NativeWorkerPool(4) as pool:
        mapped = pool.map(lambda worker, reset: worker.run_reset(reset), [scenario] * 4)
        enter = mapped[0]["legal_actions"][0]["action_id"]
        entered = pool.map(lambda worker, action: worker.run_step(action), [enter] * 4)
        potion_actions = [next(action["action_id"] for action in state["legal_actions"] if action["kind"] == "use_potion") for state in entered]
        softened = pool.map(lambda worker, action: worker.run_step(action), potion_actions)
        attack_actions = [next(action["action_id"] for action in state["legal_actions"] if action["kind"] == "play_card") for state in softened]
        won = pool.map(lambda worker, action: worker.run_step(action), attack_actions)
        assert len({state["state_hash"] for state in won}) == 1
        assert all(not state["terminated"] and [action["kind"] for action in state["legal_actions"]] == ["generate_room_rewards"] for state in won)

        rewards = pool.map(lambda worker, _: worker.run_step("generate_room_rewards"), range(4))
        assert len({state["state_hash"] for state in rewards}) == 1
        assert len({json.dumps(state["observation"]["room_rewards"], sort_keys=True) for state in rewards}) == 1
        reward_handle = rewards[0]["state_handle"]
        initial_gold = rewards[0]["observation"]["run"]["gold"]
        initial_deck = len(rewards[0]["observation"]["run"]["deck"])

        gold_actions = [next(action["action_id"] for action in state["legal_actions"] if action["parameters"].get("reward_kind") == "gold" and action["parameters"]["option_index"] == 0) for state in rewards]
        with_gold = pool.map(lambda worker, action: worker.run_step(action), gold_actions)
        assert len({state["state_hash"] for state in with_gold}) == 1
        assert all(state["observation"]["run"]["gold"] > initial_gold for state in with_gold)
        card_actions = [next(action["action_id"] for action in state["legal_actions"] if action["parameters"].get("reward_kind") == "card" and action["parameters"]["option_index"] == 0) for state in with_gold]
        with_card = pool.map(lambda worker, action: worker.run_step(action), card_actions)
        assert len({state["state_hash"] for state in with_card}) == 1
        assert all(len(state["observation"]["run"]["deck"]) == initial_deck + 1 for state in with_card)

        returned = pool.map(lambda worker, _: worker.run_step("leave_room_rewards"), range(4))
        assert len({state["state_hash"] for state in returned}) == 1
        assert all(state["observation"]["decision"]["kind"] == "map_choice" for state in returned)
        assert all(len(state["observation"]["map"]["visited"]) == 1 for state in returned)
        returned_handle = returned[0]["state_handle"]

        unknown_actions = [next(action["action_id"] for action in state["legal_actions"] if action["parameters"]["point_type"] == "Unknown") for state in returned]
        events = pool.map(lambda worker, action: worker.run_step(action), unknown_actions)
        assert len({state["state_hash"] for state in events}) == 1
        assert all(state["observation"]["event"]["model_id"] == "BRAIN_LEECH" for state in events)
        share_actions = [next(action["action_id"] for action in state["legal_actions"] if action["parameters"]["text_key"].endswith("SHARE_KNOWLEDGE")) for state in events]
        event_choices = pool.map(lambda worker, action: worker.run_step(action), share_actions)
        assert len({state["state_hash"] for state in event_choices}) == 1
        assert all(state["observation"]["decision"]["kind"] == "card_choice" for state in event_choices)
        card_choices = [next(action["action_id"] for action in state["legal_actions"] if len(action["parameters"]["option_ids"]) == 1) for state in event_choices]
        event_done = pool.map(lambda worker, action: worker.run_step(action), card_choices)
        assert len({state["state_hash"] for state in event_done}) == 1
        assert all(state["observation"]["event"]["finished"] for state in event_done)
        event_done_handle = event_done[0]["state_handle"]
        after_event = pool.map(lambda worker, _: worker.run_step("leave_event"), range(4))
        assert len({state["state_hash"] for state in after_event}) == 1
        assert all(len(state["observation"]["map"]["visited"]) == 2 for state in after_event)

        worker = pool.workers[0]
        assert worker.restore(reward_handle)["state_hash"] == rewards[0]["state_hash"]
        assert worker.restore(returned_handle)["state_hash"] == returned[0]["state_hash"]
        assert worker.restore(event_done_handle)["state_hash"] == event_done[0]["state_hash"]

        print(json.dumps({
            "success": True,
            "workers": 4,
            "won_hash": won[0]["state_hash"],
            "reward_hash": rewards[0]["state_hash"],
            "returned_map_hash": returned[0]["state_hash"],
            "rewards": rewards[0]["observation"]["room_rewards"]["rewards"],
            "gold_before": initial_gold,
            "gold_after": with_gold[0]["observation"]["run"]["gold"],
            "deck_before": initial_deck,
            "deck_after": len(with_card[0]["observation"]["run"]["deck"]),
            "next_map_actions": returned[0]["legal_actions"],
            "event_id": events[0]["observation"]["event"]["model_id"],
            "event_choice_actions": len(event_choices[0]["legal_actions"]),
            "deck_after_event": len(event_done[0]["observation"]["run"]["deck"]),
            "after_event_map_hash": after_event[0]["state_hash"],
        }, indent=2))


if __name__ == "__main__":
    main()
