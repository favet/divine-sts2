"""Search-boundary scaling benchmark; not a native resident-step benchmark."""
from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from acceptance import SCENARIO
from sts2_native_sim import NativeWorkerPool

def run(count: int, iterations: int) -> dict:
    with NativeWorkerPool(count) as pool:
        states = pool.reset_all(SCENARIO)
        handles = [state["state_handle"] for state in states]
        actions = [next(a["action_id"] for a in state["legal_actions"] if a["kind"] == "play_card") for state in states]
        hashes = []
        started = time.perf_counter()
        for _ in range(iterations):
            pool.map(lambda worker, handle: worker.restore(handle), handles)
            results = pool.map(lambda worker, action: worker.step(action), actions)
            hashes.extend(result["state_hash"] for result in results)
        elapsed = time.perf_counter() - started
        return {"workers": count, "transitions": count * iterations, "seconds": elapsed, "aggregate_transitions_per_second": count * iterations / elapsed, "deterministic": len(set(hashes)) == 1, "memory_bytes_per_worker": [w.memory_bytes for w in pool.workers]}

def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--iterations", type=int, default=20); args = parser.parse_args()
    results = [run(workers, args.iterations) for workers in (1, 2, 4, 8)]
    print(json.dumps({"boundary": "non-resident restore(reconstruct+replay) -> native PlayCardAction -> complete observation", "interpretation": "search-style reconstruction throughput; resident native step throughput is reported by latency_benchmark.py", "results": results}, indent=2))

if __name__ == "__main__": main()
