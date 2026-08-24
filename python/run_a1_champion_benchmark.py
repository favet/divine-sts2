"""
STS2 Multi-Character Ascension 1 Benchmark Runner.
Executes autonomous Ascension 1 runs across all 5 characters
(Ironclad, Silent, Defect, Necrobinder, Regent) in parallel headless sandboxes.
"""

import os
import sys
import time
import json
import random
import statistics
from pathlib import Path
from typing import List, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "python"))

from pure_neural_agent import pure_neural_agent
from sts2_native_sim.full_app_client import FullAppBridgeClient, FullAppClientConfig

ALL_CHARACTERS = ["IRONCLAD", "SILENT", "DEFECT", "NECROBINDER", "REGENT"]
DEFAULT_MAX_STEPS = 500
TRACE_TAIL_SIZE = 25


def generate_seed() -> str:
    alphabet = "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    return "".join(random.choice(alphabet) for _ in range(10))


def state_hash(obs: Dict[str, Any]) -> str:
    return str(obs.get("state_hash", ""))


def legal_action_ids(legal_actions: List[Dict[str, Any]]) -> List[str]:
    return [str(action.get("action_id", "")) for action in legal_actions]


def combat_was_won(before: Dict[str, Any], after: Dict[str, Any]) -> bool:
    """Count the combat->noncombat edge once, never every reward-screen action."""
    return (
        before.get("phase") == "combat"
        and after.get("phase") != "combat"
        and int(after.get("player_hp", 0)) > 0
    )


def percentile(values: List[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return float(ordered[index])


def run_single_a1_character_benchmark(
    worker_id: int,
    character: str,
    seed: str,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> Dict[str, Any]:
    cfg = FullAppClientConfig(worker_id=worker_id)
    client = FullAppBridgeClient(cfg)

    result = {
        "character": character,
        "seed": seed,
        "ascension": 1,
        "worker_id": worker_id,
        "max_floor_reached": 1,
        "max_act_reached": 1,
        "combats_won": 0,
        "bosses_defeated": 0,
        "total_actions": 0,
        "is_victory": False,
        "final_hp": "0/80",
        "elapsed_seconds": 0.0,
        "termination_reason": "not_started",
        "no_progress": None,
        "action_latency_ms": {},
        "trace_tail": [],
        "phases_encountered": set(),
        "error": None
    }

    t0 = time.perf_counter()
    latencies_ms: List[float] = []
    trace_tail: List[Dict[str, Any]] = []
    try:
        client.launch(requested_character=character)
        start_res = client.start_run(seed=seed, character=character, ascension=1)
        obs = start_res.get("observation", {})
        legal = start_res.get("legal_actions")
        step_idx = 0
        while step_idx < max_steps:
            if obs.get("is_terminal", False):
                result["is_victory"] = obs.get("is_victory", False)
                result["termination_reason"] = "victory" if result["is_victory"] else "death"
                break

            phase = obs.get("phase", "unknown")
            result["phases_encountered"].add(phase)
            floor = obs.get("floor", 1)
            result["max_floor_reached"] = max(result["max_floor_reached"], floor)
            # Derive act from floor. Some full-app bridge observations have exposed
            # a room/floor-like value in the act field during transitions.
            derived_act = min(3, max(1, ((int(floor) - 1) // 16) + 1))
            result["max_act_reached"] = max(result["max_act_reached"], derived_act)
            hp_cur = obs.get("player_hp", 0)
            hp_max = obs.get("player_max_hp", 80)
            result["final_hp"] = f"{hp_cur}/{hp_max}"

            if legal is None:
                legal = client.legal_actions()
            if not legal:
                result["termination_reason"] = "no_legal_actions"
                break

            action_id = pure_neural_agent.select_action(obs, legal)
            current_legal_ids = legal_action_ids(legal)
            if action_id not in current_legal_ids:
                result["termination_reason"] = "policy_returned_illegal_action"
                result["error"] = f"Policy selected {action_id!r}; legal actions were {current_legal_ids!r}"
                break

            before_obs = obs
            before_hash = state_hash(before_obs)
            result["pending_action"] = {
                "step": step_idx,
                "phase": phase,
                "floor": floor,
                "state_hash": before_hash,
                "action": action_id,
                "legal_action_ids": current_legal_ids,
            }
            t_step = time.perf_counter()
            step_res = client.step(action_id)
            latency_ms = (time.perf_counter() - t_step) * 1000.0
            latencies_ms.append(latency_ms)
            next_obs = step_res.get("observation", {})
            next_legal = step_res.get("legal_actions")
            after_hash = state_hash(next_obs)

            trace_tail.append({
                "step": step_idx,
                "phase": phase,
                "floor": floor,
                "hp": f"{before_obs.get('player_hp', 0)}/{before_obs.get('player_max_hp', 0)}",
                "action": action_id,
                "action_description": next(
                    (a.get("description", "") for a in legal if a.get("action_id") == action_id),
                    "",
                ),
                "before_hash": before_hash,
                "after_hash": after_hash,
                "latency_ms": round(latency_ms, 3),
            })
            trace_tail = trace_tail[-TRACE_TAIL_SIZE:]

            step_idx += 1
            result["total_actions"] = step_idx
            result["pending_action"] = None

            if not next_obs:
                result["termination_reason"] = "empty_observation"
                result["error"] = f"Bridge returned no observation after {action_id!r}"
                break

            next_floor = int(next_obs.get("floor", floor))
            result["max_floor_reached"] = max(result["max_floor_reached"], next_floor)
            result["max_act_reached"] = max(
                result["max_act_reached"],
                min(3, max(1, ((next_floor - 1) // 16) + 1)),
            )
            result["final_hp"] = f"{next_obs.get('player_hp', 0)}/{next_obs.get('player_max_hp', 80)}"

            if before_hash and after_hash == before_hash:
                result["termination_reason"] = "no_progress"
                result["no_progress"] = {
                    "step": step_idx - 1,
                    "phase": phase,
                    "floor": floor,
                    "state_hash": before_hash,
                    "action": action_id,
                    "legal_action_ids": current_legal_ids,
                }
                break

            if combat_was_won(before_obs, next_obs):
                result["combats_won"] += 1
                if floor in (16, 33, 50):
                    result["bosses_defeated"] += 1

            obs = next_obs
            legal = next_legal
        else:
            result["termination_reason"] = "step_cap"

    except Exception as exc:
        result["error"] = str(exc)
        result["termination_reason"] = "exception"
        print(f"[{character} Worker {worker_id}] Error: {exc}", flush=True)
    finally:
        result["elapsed_seconds"] = time.perf_counter() - t0
        result["trace_tail"] = trace_tail
        result["action_latency_ms"] = {
            "mean": round(statistics.mean(latencies_ms), 3) if latencies_ms else 0.0,
            "p50": round(percentile(latencies_ms, 0.50), 3),
            "p95": round(percentile(latencies_ms, 0.95), 3),
            "max": round(max(latencies_ms), 3) if latencies_ms else 0.0,
        }
        try:
            client.close()
        except Exception:
            pass

    result["phases_encountered"] = sorted(list(result["phases_encountered"]))
    return result


def run_full_benchmark(max_workers: int = 3):
    print("=" * 80)
    print("STS2 MULTI-CHARACTER ASCENSION 1 CHAMPION BENCHMARK (5 CHARACTERS)")
    print("=" * 80)

    seeds = {c: generate_seed() for c in ALL_CHARACTERS}
    print("Generated Ascension 1 Seeds:")
    for c in ALL_CHARACTERS:
        print(f"  - {c:12s}: Seed={seeds[c]}")

    results = []
    # Five shipped processes have demonstrated correlated socket/process loss.
    # Keep at most three resident until a memory/stability gate proves otherwise.
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(run_single_a1_character_benchmark, i, char, seeds[char]): char
            for i, char in enumerate(ALL_CHARACTERS)
        }
        for future in as_completed(futures):
            res = future.result()
            results.append(res)
            char = res["character"]
            print(f"\n[{char}] Run Finished: Max Floor={res['max_floor_reached']}, Combats Won={res['combats_won']}, Bosses Defeated={res['bosses_defeated']}, Victory={res['is_victory']}, Termination={res['termination_reason']} (Time: {res['elapsed_seconds']:.1f}s)")

    # Write summary report
    out_dir = REPO_ROOT / "artifacts"
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_file = out_dir / "a1_champion_benchmark_results.json"

    with open(summary_file, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "benchmark": "Ascension 1 All-Characters",
            "characters_tested": len(results),
            "results": results
        }, f, indent=2)

    print("\n" + "=" * 80)
    print("ASCENSION 1 BENCHMARK SUMMARY")
    print("=" * 80)
    for r in sorted(results, key=lambda x: ALL_CHARACTERS.index(x["character"])):
        print(f"  - {r['character']:12s} | Max Floor: {r['max_floor_reached']:2d} | Combats Won: {r['combats_won']:2d} | Final HP: {r['final_hp']:7s} | Victory: {r['is_victory']}")
    print(f"\nReport written to: {summary_file}")
    print("=" * 80)


if __name__ == "__main__":
    run_full_benchmark()
