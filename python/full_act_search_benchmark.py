"""Full-Act replay-depth and search-boundary benchmark.

Resident native transitions are reported separately from reconstruction-heavy
restore and complete fork/step/restore cycles.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from run_seeded_full_act_corpus import choose_action, scenario
from sts2_native_sim import NativeWorker


def summarize(values: list[float]) -> dict:
    return {
        "mean_ms": statistics.mean(values),
        "median_ms": statistics.median(values),
        "min_ms": min(values),
        "max_ms": max(values),
        "samples": len(values),
    }


def measure(operation, samples: int) -> dict:
    values = []
    for _ in range(samples):
        started = time.perf_counter()
        operation()
        values.append((time.perf_counter() - started) * 1000)
    return summarize(values)


def measure_after(setup, operation, samples: int) -> dict:
    values = []
    for _ in range(samples):
        setup()
        started = time.perf_counter()
        operation()
        values.append((time.perf_counter() - started) * 1000)
    return summarize(values)


def selected_kind(state: dict) -> str | None:
    action_id = choose_action(state)
    return next((action["kind"] for action in state["legal_actions"] if action["action_id"] == action_id), None)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", default="NATIVE-FULL-ACT-2")
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--sustained-cycles", type=int, default=20)
    args = parser.parse_args()

    with NativeWorker() as worker:
        state = worker.run_reset(scenario(args.seed))
        states = [{
            "depth": 0,
            "hash": state["state_hash"],
            "decision": state["observation"]["decision"]["kind"],
            "next_action": choose_action(state),
            "next_kind": selected_kind(state),
            "composition": {},
        }]
        action_history: list[str] = []
        composition: Counter[str] = Counter()
        while state["observation"]["decision"]["kind"] not in {"map_terminal", "terminal"}:
            action_id = choose_action(state)
            if action_id is None:
                raise RuntimeError(f"stalled at depth {len(states) - 1}")
            action = next(action for action in state["legal_actions"] if action["action_id"] == action_id)
            composition[action["kind"]] += 1
            action_history.append(action_id)
            state = worker.run_step(action_id)
            states.append({
                "depth": len(states),
                "hash": state["state_hash"],
                "decision": state["observation"]["decision"]["kind"],
                "next_action": choose_action(state),
                "next_kind": selected_kind(state),
                "composition": dict(composition),
            })

        final = states[-1]
        requested_depths = [0, 16, 64, 128, len(states) - 2]
        checkpoints = [states[min(depth, len(states) - 2)] for depth in dict.fromkeys(requested_depths)]
        results = []

        def recreate_checkpoint(checkpoint: dict) -> None:
            rebuilt = worker.run_reset(scenario(args.seed))
            for action_id in action_history[:checkpoint["depth"]]:
                rebuilt = worker.run_step(action_id)
            if rebuilt["state_hash"] != checkpoint["hash"]:
                raise RuntimeError(f"checkpoint reconstruction mismatch at depth {checkpoint['depth']}")
            checkpoint["handle"] = rebuilt["state_handle"]

        def checked_restore(checkpoint: dict) -> dict:
            restored = worker.restore(checkpoint["handle"])
            if restored["state_hash"] != checkpoint["hash"]:
                raise RuntimeError(f"restore mismatch at depth {checkpoint['depth']}")
            return restored

        for checkpoint in checkpoints:
            recreate_checkpoint(checkpoint)

            def nonresident_restore() -> None:
                restored = checked_restore(checkpoint)
                if restored["transition"].get("resident_prefix_hit"):
                    raise RuntimeError(f"depth {checkpoint['depth']} unexpectedly used resident restore")

            def resident_step() -> None:
                stepped = worker.run_step(checkpoint["next_action"])
                expected = states[checkpoint["depth"] + 1]["hash"]
                if stepped["state_hash"] != expected:
                    raise RuntimeError(f"step mismatch at depth {checkpoint['depth']}")

            def search_cycle() -> None:
                branch = worker.fork()
                resident_step()
                restored = worker.restore(branch)
                if restored["state_hash"] != checkpoint["hash"]:
                    raise RuntimeError(f"search-cycle restore mismatch at depth {checkpoint['depth']}")

            restore_stats = measure_after(lambda: worker.run_step(checkpoint["next_action"]), nonresident_restore, args.samples)
            resident_stats = measure_after(lambda: checked_restore(checkpoint), resident_step, args.samples)
            search_stats = measure_after(lambda: checked_restore(checkpoint), search_cycle, args.samples)
            results.append({
                "replay_depth": checkpoint["depth"],
                "decision": checkpoint["decision"],
                "next_action_kind": checkpoint["next_kind"],
                "action_composition": checkpoint["composition"],
                "restore_reconstruct_replay_plus_observation": restore_stats,
                "resident_native_step_plus_observation": resident_stats,
                "complete_search_fork_step_restore_plus_observations": search_stats,
            })

        sustained_checkpoint = checkpoints[len(checkpoints) // 2]
        recreate_checkpoint(sustained_checkpoint)
        before = worker.diagnostics()
        memory_before = worker.memory_bytes
        started = time.perf_counter()
        for _ in range(args.sustained_cycles):
            branch = worker.fork()
            worker.run_step(sustained_checkpoint["next_action"])
            restored = worker.restore(branch)
            if restored["state_hash"] != sustained_checkpoint["hash"]:
                raise RuntimeError("sustained search restore mismatch")
        sustained_seconds = time.perf_counter() - started
        after = worker.diagnostics()
        memory_after = worker.memory_bytes

        output = {
            "success": True,
            "interpretation": {
                "resident": "shipped native transition plus complete observation; setup restore excluded",
                "restore": "non-resident reconstruction and deterministic replay plus complete observation",
                "search": "content-addressed fork, one resident native step, non-resident restore, and observations",
            },
            "seed": args.seed,
            "full_act": {
                "actions": final["depth"],
                "outcome": final["decision"],
                "final_hash": final["hash"],
                "action_composition": final["composition"],
            },
            "by_replay_depth": results,
            "protocol_round_trip": {
                "diagnostics_small_payload": measure(worker.diagnostics, 100),
                "observe_full_payload": measure(worker.run_observe, 100),
            },
            "sustained_search": {
                "cycles": args.sustained_cycles,
                "checkpoint_depth": sustained_checkpoint["depth"],
                "elapsed_seconds": sustained_seconds,
                "cycles_per_second": args.sustained_cycles / sustained_seconds,
                "branch_count_before": before["branch_count"],
                "branch_count_after": after["branch_count"],
                "branch_capacity": after["branch_capacity"],
                "memory_before": memory_before,
                "memory_after": memory_after,
                "memory_growth_bytes": memory_after - memory_before,
            },
        }
        print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
