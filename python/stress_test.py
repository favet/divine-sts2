"""Branch-metadata and sustained-reset memory stress test."""
from __future__ import annotations
import copy, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from acceptance import SCENARIO
from sts2_native_sim import NativeWorker

def main():
    with NativeWorker() as worker:
        worker.reset(SCENARIO)
        before_observe = worker.diagnostics()
        memory_before_observe = worker.memory_bytes
        for _ in range(1000): worker.observe()
        after_observe = worker.diagnostics()
        memory_after_observe = worker.memory_bytes
        assert after_observe["branch_count"] == before_observe["branch_count"]

        for i in range(50):
            scenario = copy.deepcopy(SCENARIO); scenario["seed"] = f"STRESS-WARM-{i}"; worker.reset(scenario)
        memory_after_warmup = worker.memory_bytes
        step_count = 0
        for i in range(300):
            scenario = copy.deepcopy(SCENARIO); scenario["seed"] = f"STRESS-{i}"; worker.reset(scenario)
            action = next(a["action_id"] for a in worker.legal_actions() if a["kind"] == "play_card")
            worker.step(action); step_count += 1
        time.sleep(1)
        final = worker.diagnostics(); memory_final = worker.memory_bytes
        assert final["branch_count"] <= final["branch_capacity"] == 256
        growth = memory_final - memory_after_warmup
        # Generous failure gate: catches native-state retention, not allocator noise.
        assert growth < 256 * 1024 * 1024, f"worker grew by {growth} bytes"
        print(json.dumps({
            "success": True, "observe_calls": 1000,
            "branches_before_observe": before_observe["branch_count"], "branches_after_observe": after_observe["branch_count"],
            "unique_warmup_resets": 50, "unique_stress_resets": 300, "native_steps": step_count,
            "branch_count_after_stress": final["branch_count"], "branch_capacity": final["branch_capacity"],
            "memory_before_observe": memory_before_observe, "memory_after_observe": memory_after_observe,
            "memory_after_warmup": memory_after_warmup, "memory_final": memory_final, "sustained_growth_bytes": growth
        }, indent=2))

if __name__ == "__main__": main()
