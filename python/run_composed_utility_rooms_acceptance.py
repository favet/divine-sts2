"""Native composed-path acceptance for merchant, rest-site, and treasure rooms."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from acceptance import SCENARIO
from sts2_native_sim import NativeWorkerPool


TARGETS = {"Shop", "RestSite", "Treasure"}


def best_path(observation: dict) -> list[tuple[int, int, str]]:
    points = {(p["coord"]["col"], p["coord"]["row"]): p for p in observation["map"]["points"]}
    starts = [(a["parameters"]["col"], a["parameters"]["row"]) for a in observation["decision"]["legal_actions"]]

    def walk(coord: tuple[int, int]) -> list[list[tuple[int, int, str]]]:
        point = points[coord]
        here = (coord[0], coord[1], point["point_type"])
        children = [(c["col"], c["row"]) for c in point["children"]]
        if not children or coord[1] >= 9:
            return [[here]]
        return [[here] + tail for child in children for tail in walk(child)]

    paths = [path for start in starts for path in walk(start)]
    viable = [path for path in paths if TARGETS.issubset({step[2] for step in path})]
    if not viable:
        raise AssertionError("generated native map has no path through shop, rest, and treasure by floor 9")
    return min(viable, key=lambda path: (sum(step[2] in {"Unknown", "Elite"} for step in path), [(s[1], s[0]) for s in path]))


def matching_map_action(state: dict, step: tuple[int, int, str]) -> str:
    col, row, _ = step
    return next(a["action_id"] for a in state["legal_actions"] if a["kind"] == "choose_map" and a["parameters"]["col"] == col and a["parameters"]["row"] == row)


def step_all(pool: NativeWorkerPool, states: list[dict], action_ids: list[str]) -> list[dict]:
    result = pool.map(lambda worker, action: worker.run_step(action), action_ids)
    assert len({state["state_hash"] for state in result}) == 1
    return result


def finish_combat(pool: NativeWorkerPool, states: list[dict]) -> list[dict]:
    while True:
        kinds = [action["kind"] for action in states[0]["legal_actions"]]
        if "generate_room_rewards" in kinds:
            states = step_all(pool, states, ["generate_room_rewards"] * 4)
            return step_all(pool, states, ["leave_room_rewards"] * 4)
        plays = [action for action in states[0]["legal_actions"] if action["kind"] == "play_card"]
        action = plays[0]["action_id"] if plays else "end_turn"
        states = step_all(pool, states, [action] * 4)


def main() -> None:
    scenario = copy.deepcopy(SCENARIO)
    scenario.update({
        "seed": "NATIVE-COMPOSED-ROOM-TYPES",
        "current_hp": 9999,
        "max_hp": 9999,
        "gold": 999,
        "deck": [{"instance_id": f"bludgeon-{index}", "model_id": "BLUDGEON", "upgrades": 1} for index in range(10)],
        "initial_hand": [],
    })
    with NativeWorkerPool(4) as pool:
        states = pool.map(lambda worker, reset: worker.run_reset(reset), [scenario] * 4)
        path = best_path(states[0]["observation"])
        shop_before = shop_after = rest_done = treasure_open = treasure_done = None
        restore_handles: list[tuple[str, str]] = []

        for path_step in path:
            states = step_all(pool, states, [matching_map_action(state, path_step) for state in states])
            room_type = path_step[2]
            if room_type in {"Monster", "Elite", "Boss"}:
                states = finish_combat(pool, states)
            elif room_type == "Shop":
                shop_before = states[0]
                ordinary = next(a for a in states[0]["legal_actions"] if a["kind"] == "buy_shop" and a["parameters"]["entry_kind"] != "card_removal")
                states = step_all(pool, states, [ordinary["action_id"]] * 4)
                removal = next(a for a in states[0]["legal_actions"] if a["kind"] == "buy_shop" and a["parameters"]["entry_kind"] == "card_removal")
                states = step_all(pool, states, [removal["action_id"]] * 4)
                assert states[0]["observation"]["decision"]["kind"] == "card_choice"
                card_choice = states[0]["legal_actions"][0]["action_id"]
                states = step_all(pool, states, [card_choice] * 4)
                shop_after = states[0]
                restore_handles.append((states[0]["state_handle"], states[0]["state_hash"]))
                states = step_all(pool, states, ["leave_shop"] * 4)
            elif room_type == "RestSite":
                rest_action = states[0]["legal_actions"][0]["action_id"]
                states = step_all(pool, states, [rest_action] * 4)
                if states[0]["observation"]["decision"]["kind"] == "card_choice":
                    states = step_all(pool, states, [states[0]["legal_actions"][0]["action_id"]] * 4)
                rest_done = states[0]
                restore_handles.append((states[0]["state_handle"], states[0]["state_hash"]))
                states = step_all(pool, states, ["leave_rest"] * 4)
            elif room_type == "Treasure":
                states = step_all(pool, states, ["open_treasure"] * 4)
                treasure_open = states[0]
                choice = next(a["action_id"] for a in states[0]["legal_actions"] if a["kind"] == "choose_treasure")
                states = step_all(pool, states, [choice] * 4)
                treasure_done = states[0]
                restore_handles.append((states[0]["state_handle"], states[0]["state_hash"]))
                states = step_all(pool, states, ["leave_treasure"] * 4)
                break
            elif room_type == "Unknown":
                while states[0]["observation"]["decision"]["kind"] != "event_complete":
                    states = step_all(pool, states, [states[0]["legal_actions"][0]["action_id"]] * 4)
                states = step_all(pool, states, ["leave_event"] * 4)
            else:
                raise AssertionError(f"unexpected room type on selected path: {room_type}")

        assert shop_before and shop_after and rest_done and treasure_open and treasure_done
        assert shop_after["observation"]["run"]["gold"] < shop_before["observation"]["run"]["gold"]
        assert len(shop_after["observation"]["run"]["deck"]) == len(shop_before["observation"]["run"]["deck"])
        assert treasure_open["observation"]["treasure"]["relic_options"]
        assert len(treasure_done["observation"]["run"]["relics"]) == len(treasure_open["observation"]["run"]["relics"]) + 1
        assert len(states[0]["observation"]["map"]["visited"]) == len(path)

        worker = pool.workers[0]
        for handle, expected_hash in restore_handles:
            assert worker.restore(handle)["state_hash"] == expected_hash

        print(json.dumps({
            "success": True,
            "workers": 4,
            "path": path,
            "shop_hash": shop_after["state_hash"],
            "rest_hash": rest_done["state_hash"],
            "treasure_open_hash": treasure_open["state_hash"],
            "treasure_done_hash": treasure_done["state_hash"],
            "final_map_hash": states[0]["state_hash"],
        }, indent=2))


if __name__ == "__main__":
    main()
