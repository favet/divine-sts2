"""Four-worker acceptance for composed native map-to-combat room entry."""
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
    scenario["seed"] = "NATIVE-COMPOSED-ROOM-ENTRY"
    with NativeWorkerPool(4) as pool:
        mapped = pool.map(lambda worker, reset: worker.run_reset(reset), [scenario] * 4)
        assert len({state["state_hash"] for state in mapped}) == 1
        assert len({json.dumps(state["legal_actions"], sort_keys=True) for state in mapped}) == 1
        map_handle = mapped[0]["state_handle"]
        enter = mapped[0]["legal_actions"][0]["action_id"]

        entered = pool.map(lambda worker, action: worker.run_step(action), [enter] * 4)
        assert len({state["state_hash"] for state in entered}) == 1
        assert len({json.dumps(state["legal_actions"], sort_keys=True) for state in entered}) == 1
        assert all(state["observation"]["combat"]["phase"] == "Play" for state in entered)
        assert all(state["observation"]["combat"]["turn"] == 1 for state in entered)
        assert all(any(creature["model_id"] == "NIBBIT" for creature in state["observation"]["combat"]["creatures"]) for state in entered)
        assert all(sum(len(pile["cards"]) for pile in state["observation"]["combat"]["piles"]) == len(scenario["deck"]) for state in entered)
        entered_handle = entered[0]["state_handle"]

        plays = [next(action["action_id"] for action in state["legal_actions"] if action["kind"] == "play_card") for state in entered]
        played = pool.map(lambda worker, action: worker.run_step(action), plays)
        assert len({state["state_hash"] for state in played}) == 1
        turned = pool.map(lambda worker, _: worker.run_step("end_turn"), range(4))
        assert len({state["state_hash"] for state in turned}) == 1
        assert all(state["observation"]["combat"]["turn"] == 2 for state in turned)

        worker = pool.workers[0]
        assert worker.restore(map_handle)["state_hash"] == mapped[0]["state_hash"]
        assert worker.restore(entered_handle)["state_hash"] == entered[0]["state_hash"]

        print(json.dumps({
            "success": True,
            "workers": 4,
            "map_hash": mapped[0]["state_hash"],
            "entered_hash": entered[0]["state_hash"],
            "played_hash": played[0]["state_hash"],
            "full_turn_hash": turned[0]["state_hash"],
            "map_action": enter,
            "encounter": [creature["model_id"] for creature in entered[0]["observation"]["combat"]["creatures"] if creature["side"] == "Enemy"],
            "entry_elapsed_ms": [state["transition"]["elapsed_ms"] for state in entered],
        }, indent=2))


if __name__ == "__main__":
    main()
