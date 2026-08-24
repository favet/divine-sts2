#!/usr/bin/env python3
"""
High-Throughput Multi-Worker Full-Act Trajectory Exporter.

Streams authoritative, multi-task labeled JSONL trajectory shards from parallel
sandboxed SlayTheSpire2.exe headless workers to artifacts/trajectories/.

Multi-task training targets:
- V_win: Binary terminal victory outcome (1.0 for win, 0.0 for defeat).
- V_hp_loss: Normalized cumulative HP lost over the trajectory.
- V_relic_ev: Total relics accumulated by endgame.
- V_boss_readiness: Evaluation of deck size, upgrade density, and combat survival margin.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import random
import string
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure sts2_native_sim is in python path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from full_act_bridge_acceptance import select_policy_action
from sts2_native_sim.full_app_client import FullAppBridgeClient, FullAppClientConfig


def generate_seed() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=10))


@dataclass
class TrajectoryTransition:
    step: int
    phase: str
    floor: int
    state_hash: str
    observation: Dict[str, Any]
    legal_actions: List[Dict[str, Any]]
    chosen_action: str
    latency_ms: float


ALL_CHARACTERS = ["IRONCLAD", "SILENT", "DEFECT", "NECROBINDER", "REGENT"]

LABEL_PROVENANCE = {
    "v_win": "full_application_native_terminal_outcome",
    "v_hp_loss": "full_application_native_terminal_hp",
    "v_relic_ev": "full_application_native_relic_count",
    "v_boss_readiness_heuristic": "python_approximate",
}


def run_trajectory_collection(
    worker_id: int,
    num_runs: int,
    output_dir: Path,
    ascension: int = 0,
) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    shard_path = output_dir / f"shard_worker_{worker_id}_{int(time.time())}.jsonl"

    stats: Dict[str, Any] = {
        "worker_id": worker_id,
        "runs_completed": 0,
        "total_transitions": 0,
        "total_time_seconds": 0.0,
        "transitions_per_second": 0.0,
        "latencies_ms": [],
        "victories": 0,
        "shard_file": str(shard_path),
        "character_counts": {c: 0 for c in ALL_CHARACTERS},
        "phase_counts": {},
    }

    t0_worker = time.perf_counter()

    with open(shard_path, "w", encoding="utf-8") as out_f:
        for run_idx in range(num_runs):
            seed = generate_seed()
            # Stratified round-robin character selection
            character = ALL_CHARACTERS[(worker_id + run_idx) % len(ALL_CHARACTERS)]

            # Stagger launches: each worker waits worker_id * 3s on first run
            if run_idx == 0:
                time.sleep(worker_id * 3.0)

            cfg = FullAppClientConfig(worker_id=worker_id)
            client = FullAppBridgeClient(cfg)
            try:
                client.launch(requested_character=character)
                start_res = client.start_run(seed=seed, character=character, ascension=ascension)
                obs = start_res.get("observation", {})
            except Exception as exc:
                print(f"  [Worker {worker_id}] Run {run_idx} launch/start_run failed: {exc}", flush=True)
                try:
                    client.close()
                except Exception:
                    pass
                continue

            # Validate observed character matches requested character
            obs_char = obs.get("character", "")
            if obs_char and obs_char.upper() != character.upper() and not obs_char.upper().endswith(character.upper()):
                client.close()
                raise RuntimeError(
                    f"Character mismatch on Worker {worker_id}: requested '{character}' but observed '{obs_char}'"
                )

            print(
                f"  [Worker {worker_id}] Run {run_idx + 1}/{num_runs} starting:"
                f" seed={seed} character={character} (verified observed={obs_char})",
                flush=True,
            )

            transitions: List[TrajectoryTransition] = []
            step_idx = 0
            max_steps_per_run = 400
            run_timeout_seconds = 600.0
            t0_run = time.perf_counter()

            initial_hp = obs.get("player_hp", 80)

            try:
                while step_idx < max_steps_per_run:
                    if obs.get("is_terminal", False):
                        break

                    # 600s hard wall-clock timeout safety guard
                    if time.perf_counter() - t0_run > run_timeout_seconds:
                        print(
                            f"  [Worker {worker_id}] WARNING: Run {run_idx + 1} exceeded {run_timeout_seconds}s timeout; halting run.",
                            flush=True,
                        )
                        break

                    legal = client.legal_actions()
                    if not legal:
                        break

                    chosen_act = select_policy_action(obs, legal)

                    t0_step = time.perf_counter()
                    step_res = client.step(chosen_act)
                    lat_ms = (time.perf_counter() - t0_step) * 1000.0
                    stats["latencies_ms"].append(lat_ms)

                    next_obs = step_res.get("observation", {})
                    h = next_obs.get("state_hash", "")
                    ph = obs.get("phase", "unknown")
                    stats["phase_counts"][ph] = stats["phase_counts"].get(ph, 0) + 1

                    transitions.append(
                        TrajectoryTransition(
                            step=step_idx,
                            phase=ph,
                            floor=obs.get("floor", 0),
                            state_hash=h,
                            observation=obs,
                            legal_actions=legal,
                            chosen_action=chosen_act,
                            latency_ms=lat_ms,
                        )
                    )

                    obs = next_obs
                    step_idx += 1

                if step_idx >= max_steps_per_run and not obs.get("is_terminal", False):
                    print(
                        f"  [Worker {worker_id}] WARNING: Run {run_idx + 1} reached max_steps_per_run={max_steps_per_run} without terminal state.",
                        flush=True,
                    )
            except Exception as exc:
                print(f"  [Worker {worker_id}] Run {run_idx} step loop failed at step {step_idx}: {exc}", flush=True)
            finally:
                try:
                    client.close()
                except Exception:
                    pass

            # Label multi-task targets across full episode
            is_win = bool(obs.get("is_victory", False))
            if is_win:
                stats["victories"] += 1

            final_hp = obs.get("player_hp", 0)
            hp_loss = max(0, initial_hp - final_hp)
            relic_count = len(obs.get("relics", []))
            deck_size = len(obs.get("deck_cards", []))
            boss_readiness = float(relic_count * 2.0 + (final_hp / max(1, obs.get("player_max_hp", 80))) * 5.0)

            for tr in transitions:
                record = {
                    "seed": seed,
                    "character": character,
                    "ascension": ascension,
                    "step": tr.step,
                    "phase": tr.phase,
                    "floor": tr.floor,
                    "state_hash": tr.state_hash,
                    "observation": tr.observation,
                    "legal_actions": tr.legal_actions,
                    "action": tr.chosen_action,
                    "targets": {
                        "v_win": 1.0 if is_win else 0.0,
                        "v_hp_loss": float(hp_loss),
                        "v_relic_ev": float(relic_count),
                        "v_boss_readiness_heuristic": boss_readiness,
                    },
                    "label_provenance": LABEL_PROVENANCE,
                    "latency_ms": tr.latency_ms,
                }
                out_f.write(json.dumps(record) + "\n")
                stats["total_transitions"] += 1

            out_f.flush()
            if transitions:
                stats["runs_completed"] += 1
                stats["character_counts"][character] += 1
                print(
                    f"  [Worker {worker_id}] Run {run_idx + 1}/{num_runs} complete:"
                    f" seed={seed} character={character} steps={step_idx} win={is_win}",
                    flush=True,
                )

    total_time = time.perf_counter() - t0_worker
    stats["total_time_seconds"] = total_time
    stats["transitions_per_second"] = stats["total_transitions"] / max(0.001, total_time)
    return stats


def export_trajectories(num_workers: int = 4, runs_per_worker: int = 5) -> int:
    print("=" * 80, flush=True)
    print("STS2 HIGH-THROUGHPUT MULTI-WORKER TRAJECTORY EXPORT PIPELINE", flush=True)
    print("=" * 80, flush=True)

    output_dir = Path(__file__).resolve().parent.parent / "artifacts" / "trajectories"
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Launching {num_workers} parallel workers ({runs_per_worker} runs each, {num_workers * runs_per_worker} total)...", flush=True)
    t0 = time.perf_counter()

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = [
            executor.submit(
                run_trajectory_collection,
                worker_id=i,
                num_runs=runs_per_worker,
                output_dir=output_dir,
            )
            for i in range(num_workers)
        ]
        results = [f.result() for f in futures]

    total_time = time.perf_counter() - t0
    total_transitions = sum(r["total_transitions"] for r in results)
    total_runs = sum(r["runs_completed"] for r in results)
    total_victories = sum(r["victories"] for r in results)
    aggregate_tps = total_transitions / max(0.001, total_time)

    # Aggregate character and phase distributions
    char_dist = {c: 0 for c in ALL_CHARACTERS}
    phase_dist = {}
    for r in results:
        for c, count in r.get("character_counts", {}).items():
            char_dist[c] += count
        for ph, count in r.get("phase_counts", {}).items():
            phase_dist[ph] = phase_dist.get(ph, 0) + count

    all_lats = []
    for r in results:
        all_lats.extend(r["latencies_ms"])

    all_lats_sorted = sorted(all_lats) if all_lats else [0.0]
    p50 = all_lats_sorted[int(len(all_lats_sorted) * 0.5)]
    p95 = all_lats_sorted[int(len(all_lats_sorted) * 0.95)]
    p99 = all_lats_sorted[int(len(all_lats_sorted) * 0.99)]
    mean_lat = sum(all_lats) / len(all_lats) if all_lats else 0.0

    summary = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "num_workers": num_workers,
        "total_runs": total_runs,
        "total_victories": total_victories,
        "win_rate": total_victories / max(1, total_runs),
        "total_transitions_exported": total_transitions,
        "wall_clock_time_seconds": total_time,
        "aggregate_throughput_transitions_per_second": aggregate_tps,
        "character_distribution": char_dist,
        "phase_distribution": {
            ph: {
                "count": count,
                "percentage": round(count / max(1, total_transitions) * 100, 2),
            }
            for ph, count in phase_dist.items()
        },
        "label_provenance": LABEL_PROVENANCE,
        "latency_metrics_ms": {
            "mean": mean_lat,
            "p50": p50,
            "p95": p95,
            "p99": p99,
        },
        "worker_shards": [r["shard_file"] for r in results],
    }

    summary_file = output_dir / "export_summary.json"
    with open(summary_file, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print("\n" + "=" * 80, flush=True)
    print("TRAJECTORY EXPORT PIPELINE COMPLETE", flush=True)
    print(f"  - Total Runs Collected: {total_runs}", flush=True)
    print(f"  - Total Victories: {total_victories} (Win Rate: {total_victories / max(1, total_runs):.1%})", flush=True)
    print(f"  - Total Transitions: {total_transitions}", flush=True)
    print(f"  - Wall Time: {total_time:.2f}s", flush=True)
    print(f"  - Aggregate Throughput: {aggregate_tps:.2f} transitions/sec", flush=True)
    print(f"  - Character Distribution: {char_dist}", flush=True)
    print(f"  - Latency: P50={p50:.2f}ms, P95={p95:.2f}ms, Mean={mean_lat:.2f}ms", flush=True)
    print(f"  - Shards written to: {output_dir}", flush=True)
    print(f"  - Summary report: {summary_file}", flush=True)
    print("=" * 80, flush=True)

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workers", nargs="?", type=int, default=4)
    parser.add_argument("runs", nargs="?", type=int, default=5)
    args = parser.parse_args(argv)
    return export_trajectories(num_workers=args.workers, runs_per_worker=args.runs)


if __name__ == "__main__":
    sys.exit(main())
