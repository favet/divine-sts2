"""Separated resident, reconstruction, search-style, and protocol benchmarks."""
from __future__ import annotations
import json, statistics, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from acceptance import SCENARIO
from sts2_native_sim import NativeWorker

def summary(values):
    return {"mean_ms": statistics.mean(values), "median_ms": statistics.median(values), "min_ms": min(values), "max_ms": max(values), "samples": len(values)}

def timed(operation, samples=20):
    values = []
    for _ in range(samples):
        started = time.perf_counter(); operation(); values.append((time.perf_counter() - started) * 1000)
    return summary(values)

def timed_after(setup, operation, samples=20):
    values = []
    for _ in range(samples):
        setup(); started = time.perf_counter(); operation(); values.append((time.perf_counter() - started) * 1000)
    return summary(values)

def main():
    with NativeWorker() as worker:
        initial = worker.reset(SCENARIO); h0 = initial["state_handle"]
        play = next(a["action_id"] for a in initial["legal_actions"] if a["kind"] == "play_card")
        after_play = worker.step(play); h1 = after_play["state_handle"]
        after_turn = worker.step("end_turn"); h2 = after_turn["state_handle"]

        def search_cycle():
            branch = worker.fork()
            worker.step(play)
            worker.restore(branch)
            worker.step("end_turn")

        result = {
            "native_noninteractive_mode": True,
            "resident": {
                "play_card_step_plus_observation": timed_after(lambda: worker.restore(h0), lambda: worker.step(play)),
                "complete_end_enemy_start_turn_plus_observation": timed_after(lambda: worker.restore(h1), lambda: worker.step("end_turn")),
            },
            "reset": timed(lambda: worker.reset(SCENARIO)),
            "restore_reconstruct_replay": {
                "depth_0_no_actions": timed_after(lambda: worker.restore(h1), lambda: worker.restore(h0), 10),
                "depth_1_play_card": timed_after(lambda: worker.restore(h0), lambda: worker.restore(h1), 10),
                "depth_2_play_plus_full_turn": timed_after(lambda: worker.restore(h0), lambda: worker.restore(h2), 10),
            },
            "search_style_fork_play_restore_end_turn": timed_after(lambda: worker.restore(h0), search_cycle, 10),
            "protocol_round_trip": {
                "diagnostics_small_payload": timed(worker.diagnostics, 100),
                "observe_full_payload": timed(worker.observe, 100),
            },
            "memory_bytes": worker.memory_bytes,
            "hashes": {"initial": initial["state_hash"], "after_play": after_play["state_hash"], "after_full_turn": after_turn["state_hash"]},
        }
        print(json.dumps(result, indent=2))

if __name__ == "__main__": main()
