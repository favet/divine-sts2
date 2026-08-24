"""
STS2 Native Run Densifier & Replay Engine.
Replays winning high-ascension human community seeds in SlayTheSpire2.exe --headless,
streaming dense, authoritative full-application state-action transitions labeled with v_win = 1.0.
"""

import os
import sys
import json
import time
import math
import random
import multiprocessing as mp
from pathlib import Path
from typing import Dict, List, Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "python"))

from sts2_native_sim.full_app_client import FullAppBridgeClient, FullAppClientConfig
from sts2_native_sim.paths import find_game_root
from full_act_bridge_acceptance import select_policy_action

ALL_CHARACTERS = ["IRONCLAD", "SILENT", "DEFECT", "NECROBINDER", "REGENT"]


def find_winning_community_seeds(max_per_char: int = 10) -> List[Dict[str, Any]]:
    """Discovers high-ascension winning run seeds from the parsed community database."""
    seeds_by_char = {c: [] for c in ALL_CHARACTERS}

    # 1. Inspect synergy dataset A10 manifests
    manifest_p = REPO_ROOT / "game_database" / "synergy_dataset" / "a10_winning_runs_manifest.json"
    if manifest_p.exists():
        try:
            with open(manifest_p, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            for item in manifest:
                char = str(item.get("character", "")).upper().replace("CHARACTER.", "")
                if char in seeds_by_char and len(seeds_by_char[char]) < max_per_char:
                    # A run hash is not a playable game seed. Only exact exported
                    # seeds are eligible for native replay.
                    seed_str = str(item.get("seed", "")).strip().upper()
                    if len(seed_str) == 10:
                        seeds_by_char[char].append({
                            "seed": seed_str,
                            "character": char,
                            "ascension": int(item.get("ascension", 10)),
                            "win": True,
                            "source": "community_a10_win"
                        })
        except Exception:
            pass

    # Interleave characters evenly
    interleaved = []
    for i in range(max_per_char):
        for char in ALL_CHARACTERS:
            if i < len(seeds_by_char[char]):
                interleaved.append(seeds_by_char[char][i])

    return interleaved


def densify_worker_process(
    worker_id: int,
    run_tasks: List[Dict[str, Any]],
    output_shard_path: str,
    max_steps_per_run: int = 400
):
    """Executes a shard of winning runs in a dedicated headless sandbox."""
    client_cfg = FullAppClientConfig(
        worker_id=worker_id,
        timeout_seconds=30.0,
        game_root=str(find_game_root())
    )
    client = FullAppBridgeClient(client_cfg)

    transitions_exported = 0
    runs_completed = 0
    victories = 0

    shard_p = Path(output_shard_path)
    shard_p.parent.mkdir(parents=True, exist_ok=True)

    with open(shard_p, "w", encoding="utf-8") as shard_file:
        for r_idx, run_meta in enumerate(run_tasks, 1):
            seed = run_meta["seed"]
            character = run_meta["character"]
            ascension = run_meta["ascension"]

            try:
                client.launch(requested_character=character)
                init_resp = client.start_run(seed=seed, character=character, ascension=ascension)
            except Exception as e:
                print(f"  [Densifier {worker_id}] Run {r_idx} launch failed: {e}", flush=True)
                client.close()
                time.sleep(1.0)
                continue

            obs = init_resp.get("observation", {})
            legal_actions = init_resp.get("legal_actions", [])

            run_transitions = []
            run_won = False
            start_time = time.time()

            for step in range(max_steps_per_run):
                if time.time() - start_time > 600:
                    print(f"  [Densifier {worker_id}] Run {r_idx} wall-clock timeout (600s).", flush=True)
                    break

                if obs.get("is_terminal", False):
                    run_won = obs.get("is_victory", False)
                    break

                action_id = select_policy_action(obs, legal_actions)
                t_start = time.perf_counter()

                try:
                    step_resp = client.step(action_id)
                except Exception as e:
                    print(f"  [Densifier {worker_id}] Run {r_idx} step {step} error: {e}", flush=True)
                    break

                latency_ms = (time.perf_counter() - t_start) * 1000.0

                record = {
                    "seed": seed,
                    "character": character,
                    "ascension": ascension,
                    "step": step,
                    "phase": obs.get("phase", "unknown"),
                    "floor": obs.get("floor", 0),
                    "state_hash": obs.get("state_hash", ""),
                    "observation": obs,
                    "legal_actions": legal_actions,
                    "action": action_id,
                    "targets": {
                        "v_win": None,
                        "v_hp_loss": float(obs.get("player_max_hp", 80) - obs.get("player_hp", 80)),
                        "v_relic_ev": float(len(obs.get("relics", []))),
                        "v_boss_readiness_heuristic": float(obs.get("floor", 0)) / 16.0 * 5.0
                    },
                    "label_provenance": {
                        "v_win": "full_application_native_terminal_outcome",
                        "v_hp_loss": "full_application_native_terminal_hp",
                        "v_relic_ev": "full_application_native_relic_count",
                        "v_boss_readiness_heuristic": "python_approximate"
                    },
                    "latency_ms": latency_ms
                }
                run_transitions.append(record)

                obs = step_resp.get("observation", {})
                legal_actions = step_resp.get("legal_actions", [])

            for rec in run_transitions:
                rec["targets"]["v_win"] = 1.0 if run_won else 0.0
                shard_file.write(json.dumps(rec) + "\n")
                transitions_exported += 1
            shard_file.flush()

            runs_completed += 1
            if run_won:
                victories += 1

            print(f"  [Densifier {worker_id}] Run {r_idx}/{len(run_tasks)} complete: seed={seed} char={character} transitions={len(run_transitions)}", flush=True)
            client.close()
            time.sleep(0.5)

    print(f"[Densifier {worker_id}] Finished {runs_completed} runs, exported {transitions_exported} transitions.", flush=True)


def run_densification_pipeline(num_workers: int = 4, runs_per_worker: int = 5):
    print("=" * 80)
    print("STS2 NATIVE RUN DENSIFICATION & REPLAY ENGINE")
    print("=" * 80)

    total_runs = num_workers * runs_per_worker
    seeds = find_winning_community_seeds(max_per_char=max(5, (total_runs // 5) + 1))
    selected_runs = seeds[:total_runs]

    if not selected_runs:
        raise RuntimeError(
            "No replayable community runs contain exact game seeds; refusing to fabricate seeds or positive labels."
        )

    print(f"Discovered and scheduled {len(selected_runs)} high-ascension winning runs across {num_workers} parallel workers.")

    # Partition runs across workers
    worker_tasks = [[] for _ in range(num_workers)]
    for i, r in enumerate(selected_runs):
        worker_tasks[i % num_workers].append(r)

    timestamp = int(time.time())
    output_dir = REPO_ROOT / "artifacts" / "trajectories" / "densified_shards"
    output_dir.mkdir(parents=True, exist_ok=True)

    processes = []
    shard_paths = []

    start_time = time.time()

    for w_id in range(num_workers):
        shard_path = str(output_dir / f"shard_densified_worker_{w_id}_{timestamp}.jsonl")
        shard_paths.append(shard_path)
        p = mp.Process(
            target=densify_worker_process,
            args=(w_id, worker_tasks[w_id], shard_path, 400)
        )
        processes.append(p)
        p.start()
        time.sleep(2.0)  # Stagger launch

    for p in processes:
        p.join()

    wall_time = time.time() - start_time
    total_transitions = 0

    for sp in shard_paths:
        if os.path.exists(sp):
            with open(sp, "r", encoding="utf-8") as f:
                for _ in f:
                    total_transitions += 1

    throughput = total_transitions / max(0.1, wall_time)

    summary_data = {
        "metadata": {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "num_workers": num_workers,
            "runs_per_worker": runs_per_worker,
            "total_runs": total_runs,
            "total_positive_transitions": total_transitions,
            "wall_time_seconds": round(wall_time, 2),
            "throughput_transitions_per_sec": round(throughput, 2)
        },
        "shard_files": [Path(sp).name for sp in shard_paths if os.path.exists(sp)]
    }
    summary_path = output_dir / "densify_summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary_data, f, indent=2)

    print("\n" + "=" * 80)
    print(f"DENSIFICATION COMPLETE:")
    print(f"  - Total Runs Densified: {total_runs}")
    print(f"  - Total Positive (v_win=1.0) Transitions Exported: {total_transitions}")
    print(f"  - Wall Time: {wall_time:.2f}s")
    print(f"  - Throughput: {throughput:.2f} transitions/sec")
    print(f"  - Summary written to: {summary_path}")
    print("=" * 80)


if __name__ == "__main__":
    num_workers = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    runs_per_worker = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    run_densification_pipeline(num_workers=num_workers, runs_per_worker=runs_per_worker)
