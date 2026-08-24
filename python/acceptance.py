"""Persistent-environment milestone acceptance test."""
from __future__ import annotations
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from sts2_native_sim import NativeWorkerPool

DECK = ([{"instance_id": f"strike-{i}", "model_id": "STRIKE_IRONCLAD"} for i in range(5)] +
        [{"instance_id": f"defend-{i}", "model_id": "DEFEND_IRONCLAD"} for i in range(5)])
SCENARIO = {
    "game_build": {}, "seed": "NATIVESIMSPIKE", "rng_counters": {}, "character": "IRONCLAD", "ascension": 0,
    "encounter": "first", "current_hp": 80, "max_hp": 80, "gold": 99,
    "deck": DECK, "initial_hand": ["strike-0"], "relics": [], "potions": []
}

def main() -> None:
    started = time.perf_counter()
    with NativeWorkerPool(4) as pool:
        initial = pool.reset_all(SCENARIO)
        assert len({x["state_hash"] for x in initial}) == 1
        assert len({json.dumps(x["legal_actions"], sort_keys=True) for x in initial}) == 1
        assert all(sum(len(p["cards"]) for p in x["observation"]["combat"]["piles"]) == len(DECK) for x in initial)
        assert all(len({c["instance_id"] for p in x["observation"]["combat"]["piles"] for c in p["cards"]}) == len(DECK) for x in initial)
        play_ids = [next(a["action_id"] for a in state["legal_actions"] if a["kind"] == "play_card") for state in initial]
        played_all = pool.map(lambda worker, action: worker.step(action), play_ids)
        assert len({x["state_hash"] for x in played_all}) == 1
        turned_all = pool.map(lambda worker, _: worker.step("end_turn"), range(4))
        assert len({x["state_hash"] for x in turned_all}) == 1
        assert all(x["observation"]["combat"]["turn"] == 2 for x in turned_all)
        initial = pool.reset_all(SCENARIO)
        observed_twice = [pool.workers[0].observe(), pool.workers[0].observe()]
        assert observed_twice[0]["state_hash"] == observed_twice[1]["state_hash"] == initial[0]["state_hash"]
        worker = pool.workers[0]
        original = initial[0]["state_handle"]
        play = next(x["action_id"] for x in initial[0]["legal_actions"] if x["kind"] == "play_card")
        after_play = worker.step(play)
        branch_after_play = after_play["state_handle"]
        portable_branch = worker.export_branch()
        next_turn = worker.step("end_turn")
        assert next_turn["transition"]["elapsed_ms"] < 100, "native noninteractive mode did not suppress presentation wait"
        assert next_turn["observation"]["combat"]["turn"] > initial[0]["observation"]["combat"]["turn"]
        continuation_a = next_turn["state_hash"]
        worker.restore(branch_after_play)
        continuation_b = worker.step("end_turn")["state_hash"]
        assert continuation_a == continuation_b
        pool.restore_portable(1, portable_branch)
        continuation_c = pool.workers[1].step("end_turn")["state_hash"]
        assert continuation_a == continuation_c
        restored = worker.restore(original)
        assert restored["state_hash"] == initial[0]["state_hash"]
        before_cards = sum(len(p["cards"]) for p in initial[0]["observation"]["combat"]["piles"])
        after_cards = sum(len(p["cards"]) for p in after_play["observation"]["combat"]["piles"])
        assert before_cards == after_cards
        iterations = 10
        tick = time.perf_counter()
        for _ in range(iterations): worker.restore(branch_after_play); worker.step("end_turn")
        elapsed = time.perf_counter() - tick
        result = {"success": True, "workers": 4, "initial_hash": initial[0]["state_hash"], "restored_hash": restored["state_hash"], "end_to_end_transitions_per_second": iterations / elapsed, "memory_bytes_per_worker": [w.memory_bytes for w in pool.workers], "wall_seconds": time.perf_counter() - started}
        print(json.dumps(result, indent=2))

if __name__ == "__main__": main()
