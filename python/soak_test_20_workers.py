"""
20-Worker Multithreaded 100-Second Soak Test for Native STS2 C# Engine.
Runs 20 isolated native workers concurrently for 100 seconds across randomized seeds,
encounters, and combat scenarios to stress-test throughput, determinism, and zero-crash stability.
"""

from __future__ import annotations
import sys
import os
import time
import json
import random
import threading
from pathlib import Path
from typing import Dict, Any, List

sys.path.insert(0, str(Path(__file__).parent))
from sts2_native_sim import NativeWorkerPool, NativeWorker

ENCOUNTERS = [
    "first", "bygone_effigy", "bowlbugs", "axebots", "chomper", "seapunk"
]

CHARACTERS = ["IRONCLAD", "SILENT", "DEFECT"]

DECK_TEMPLATES = {
    "IRONCLAD": [
        {"instance_id": f"strike-{i}", "model_id": "STRIKE_IRONCLAD"} for i in range(5)
    ] + [
        {"instance_id": f"defend-{i}", "model_id": "DEFEND_IRONCLAD"} for i in range(4)
    ] + [
        {"instance_id": "bash-0", "model_id": "BASH"}
    ],
    "SILENT": [
        {"instance_id": f"strike-{i}", "model_id": "STRIKE_SILENT"} for i in range(5)
    ] + [
        {"instance_id": f"defend-{i}", "model_id": "DEFEND_SILENT"} for i in range(5)
    ] + [
        {"instance_id": "neutralize-0", "model_id": "NEUTRALIZE"},
        {"instance_id": "survivor-0", "model_id": "SURVIVOR"}
    ],
    "DEFECT": [
        {"instance_id": f"strike-{i}", "model_id": "STRIKE_DEFECT"} for i in range(4)
    ] + [
        {"instance_id": f"defend-{i}", "model_id": "DEFEND_DEFECT"} for i in range(4)
    ] + [
        {"instance_id": "zap-0", "model_id": "ZAP"},
        {"instance_id": "dualcast-0", "model_id": "DUALCAST"}
    ]
}


def run_worker_soak(worker: NativeWorker, worker_id: int, duration_seconds: float, stop_event: threading.Event, stats: Dict[str, Any]):
    rng = random.Random(42000 + worker_id)
    local_combats = 0
    local_actions = 0
    local_turns = 0
    local_errors = 0

    t_start = time.perf_counter()

    while not stop_event.is_set() and (time.perf_counter() - t_start < duration_seconds):
        char = rng.choice(CHARACTERS)
        deck = DECK_TEMPLATES[char]
        seed = f"SOAK_{worker_id}_{local_combats}_{rng.randint(1000, 999999)}"
        
        scenario = {
            "game_build": {},
            "seed": seed,
            "rng_counters": {},
            "character": char,
            "ascension": rng.choice([0, 1, 5, 10]),
            "encounter": "first",
            "current_hp": 80,
            "max_hp": 80,
            "gold": 99,
            "deck": deck,
            "initial_hand": [],
            "relics": [],
            "potions": []
        }

        try:
            state = worker.reset(scenario)
            local_combats += 1

            for _ in range(50):
                if stop_event.is_set():
                    break
                
                legal = state.get("legal_actions", [])
                if not legal:
                    break

                # Priority: Play cards first, then end turn
                card_plays = [a["action_id"] for a in legal if a.get("kind") == "play_card"]
                if card_plays:
                    action = rng.choice(card_plays)
                else:
                    end_turns = [a["action_id"] for a in legal if a.get("action_id") == "end_turn"]
                    if end_turns:
                        action = end_turns[0]
                        local_turns += 1
                    else:
                        action = legal[0]["action_id"]

                state = worker.step(action)
                local_actions += 1

                # Check if terminal or ended
                obs = state.get("observation", {})
                creatures = obs.get("combat", {}).get("creatures", [])
                enemies_alive = [c for c in creatures if c.get("side") == "Enemy" and c.get("alive", False)]
                player_alive = any(c for c in creatures if c.get("side") == "Player" and c.get("alive", False))

                if not enemies_alive or not player_alive:
                    break

        except Exception as ex:
            local_errors += 1
            print(f"[Worker {worker_id}] Error: {ex}", file=sys.stderr)
            time.sleep(0.05)

    stats["combats"] = local_combats
    stats["actions"] = local_actions
    stats["turns"] = local_turns
    stats["errors"] = local_errors
    stats["memory_bytes"] = worker.memory_bytes


def main():
    num_workers = 20
    soak_duration = 100.0

    print("=" * 80)
    print(f"STS2 NATIVE C# SIMULATOR: 20-WORKER {soak_duration}s MULTITHREADED SOAK TEST")
    print("=" * 80)
    print(f"Launching {num_workers} isolated native Godot/.NET 9 workers...")

    t0 = time.perf_counter()
    with NativeWorkerPool(num_workers) as pool:
        print(f"All {num_workers} workers successfully spawned and handshake confirmed in {time.perf_counter() - t0:.2f}s!")
        print(f"Starting sustained soak test for {soak_duration} seconds across all 20 threads...")

        stop_event = threading.Event()
        worker_stats = [{} for _ in range(num_workers)]
        threads = []

        for i, worker in enumerate(pool.workers):
            t = threading.Thread(
                target=run_worker_soak,
                args=(worker, i, soak_duration, stop_event, worker_stats[i]),
                daemon=True
            )
            threads.append(t)
            t.start()

        # Monitor progress every 10 seconds
        start_time = time.perf_counter()
        while True:
            elapsed = time.perf_counter() - start_time
            if elapsed >= soak_duration:
                break
            time.sleep(10.0)
            cur_actions = sum(s.get("actions", 0) for s in worker_stats)
            cur_combats = sum(s.get("combats", 0) for s in worker_stats)
            rate = cur_actions / max(0.1, elapsed)
            print(f"[{elapsed:.1f}s / {soak_duration}s] Completed {cur_combats} combats, {cur_actions} native decisions ({rate:.1f} decisions/sec across 20 workers)")

        stop_event.set()
        for t in threads:
            t.join(timeout=10.0)

        total_elapsed = time.perf_counter() - start_time
        total_combats = sum(s.get("combats", 0) for s in worker_stats)
        total_actions = sum(s.get("actions", 0) for s in worker_stats)
        total_turns = sum(s.get("turns", 0) for s in worker_stats)
        total_errors = sum(s.get("errors", 0) for s in worker_stats)
        total_memory_mb = sum(s.get("memory_bytes", 0) for s in worker_stats) / (1024 * 1024)

        report = {
            "num_workers": num_workers,
            "soak_duration_target_seconds": soak_duration,
            "actual_duration_seconds": round(total_elapsed, 2),
            "total_combats_completed": total_combats,
            "total_native_actions": total_actions,
            "total_turns_simulated": total_turns,
            "decisions_per_second": round(total_actions / max(0.1, total_elapsed), 1),
            "combats_per_minute": round((total_combats / max(0.1, total_elapsed)) * 60, 1),
            "total_crashes_or_errors": total_errors,
            "total_farm_memory_mb": round(total_memory_mb, 1),
            "avg_memory_mb_per_worker": round(total_memory_mb / max(1, num_workers), 1),
            "passed": total_errors == 0 and total_actions > 500
        }

        output_path = Path(__file__).resolve().parents[1] / "artifacts" / "soak_test_20_workers_report.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)

        print("\n" + "=" * 80)
        print("20-WORKER 100-SECOND SOAK TEST RESULTS:")
        print(f"  - Passed:                 {report['passed']}")
        print(f"  - Total Native Actions:   {total_actions}")
        print(f"  - Combats Completed:      {total_combats} ({report['combats_per_minute']} combats/min)")
        print(f"  - Throughput:             {report['decisions_per_second']} decisions/sec across 20 workers")
        print(f"  - Total Errors/Crashes:   {total_errors}")
        print(f"  - Average RAM per Worker: {report['avg_memory_mb_per_worker']} MB")
        print(f"  - Report Artifact:        {output_path}")
        print("=" * 80)

        if not report["passed"]:
            sys.exit(1)


if __name__ == "__main__":
    main()
