"""
Reinforcement Learning Rollout Engine Performance Benchmark Suite.
Validates reconstructed native combat backend throughput, latency, memory behavior, determinism, and worker reliability.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import os
import platform
import queue
import random
import statistics
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import psutil

REPO_ROOT = Path(__file__).resolve().parent
PYTHON_DIR = REPO_ROOT / "python"
if str(PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_DIR))

from sts2_native_sim import NativeSimError, NativeWorker, NativeWorkerPool
from sts2_native_sim.paths import find_dotnet, find_game_assembly, find_game_root, find_godot

# ─── System / Preflight Discovery ─────────────────────────────────────────────

def get_preflight_info() -> Dict[str, Any]:
    # Git info
    git_sha = "unknown"
    git_clean = False
    try:
        res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT), capture_output=True, text=True, check=True)
        git_sha = res.stdout.strip()
        res_stat = subprocess.run(["git", "status", "--porcelain"], cwd=str(REPO_ROOT), capture_output=True, text=True, check=True)
        git_clean = (len(res_stat.stdout.strip()) == 0)
    except Exception as e:
        git_sha = str(e)

    # Dotnet info
    dotnet_path = find_dotnet()
    dotnet_ver = "unknown"
    try:
        res = subprocess.run([str(dotnet_path), "--version"], capture_output=True, text=True)
        dotnet_ver = res.stdout.strip()
    except Exception:
        pass

    # Game files & hashes
    game_root = find_game_root()
    assembly_path = find_game_assembly()
    pck_path = game_root / "SlayTheSpire2.pck"
    godot_path = find_godot()

    assembly_sha256 = hashlib.sha256(assembly_path.read_bytes()).hexdigest() if assembly_path.is_file() else "missing"
    pck_sha256 = hashlib.sha256(pck_path.read_bytes()).hexdigest() if pck_path.is_file() else "missing"

    # Worker hello for build info
    worker_build = {}
    try:
        with NativeWorker() as w:
            worker_build = w.build
    except Exception as e:
        worker_build = {"error": str(e)}

    # Hardware & OS info
    cpu_model = platform.processor()
    try:
        res = subprocess.run(["powershell", "-Command", "Get-CimInstance Win32_Processor | Select-Object -ExpandProperty Name"], capture_output=True, text=True)
        if res.stdout.strip():
            cpu_model = res.stdout.strip()
    except Exception:
        pass

    gpu_info = "unknown"
    try:
        res = subprocess.run(["powershell", "-Command", "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name"], capture_output=True, text=True)
        gpu_info = "; ".join(line.strip() for line in res.stdout.splitlines() if line.strip())
    except Exception:
        pass

    ram = psutil.virtual_memory()

    return {
        "git_commit_sha": git_sha,
        "working_tree_clean": git_clean,
        "game_build": worker_build,
        "assembly_path": str(assembly_path),
        "assembly_sha256": assembly_sha256,
        "pck_path": str(pck_path),
        "pck_sha256": pck_sha256,
        "godot_path": str(godot_path),
        "dotnet_path": str(dotnet_path),
        "dotnet_version": dotnet_ver,
        "python_version": sys.version,
        "cpu_model": cpu_model,
        "cpu_physical_cores": psutil.cpu_count(logical=False),
        "cpu_logical_cores": psutil.cpu_count(logical=True),
        "physical_ram_total_gb": round(ram.total / (1024**3), 2),
        "physical_ram_available_gb": round(ram.available / (1024**3), 2),
        "physical_ram_used_percent": ram.percent,
        "operating_system": f"{platform.system()} {platform.release()} ({platform.version()})",
        "gpu_info": gpu_info,
        "initial_process_count": len(psutil.pids()),
    }


# ─── Representative Scenario Catalog ──────────────────────────────────────────

BENCHMARK_SCENARIOS = [
    {
        "name": "ironclad_hallway",
        "character": "IRONCLAD",
        "encounter": "first",
        "deck": [{"instance_id": f"ic-{i}", "model_id": m} for i, m in enumerate(["STRIKE_IRONCLAD"]*5 + ["DEFEND_IRONCLAD"]*4 + ["BASH"])],
        "initial_hand": ["ic-0", "ic-1", "ic-9"],
        "relics": [{"model_id": "BURNING_BLOOD"}],
        "potions": [{"model_id": "FIRE_POTION", "slot": 0}],
        "invoke_combat_entry_hooks": True
    },
    {
        "name": "silent_hallway_potions",
        "character": "SILENT",
        "encounter": "NIBBITS_WEAK",
        "deck": [{"instance_id": f"sil-{i}", "model_id": m} for i, m in enumerate(["STRIKE_SILENT"]*5 + ["DEFEND_SILENT"]*5 + ["NEUTRALIZE", "SURVIVOR"])],
        "initial_hand": ["sil-0", "sil-10", "sil-11"],
        "relics": [{"model_id": "RING_OF_THE_SNAKE"}],
        "potions": [{"model_id": "ENERGY_POTION", "slot": 0}],
        "invoke_combat_entry_hooks": True
    },
    {
        "name": "defect_orbs_multi",
        "character": "DEFECT",
        "encounter": "BOWLBUGS_WEAK",
        "deck": [{"instance_id": f"def-{i}", "model_id": m} for i, m in enumerate(["STRIKE_DEFECT"]*4 + ["DEFEND_DEFECT"]*4 + ["ZAP", "DUALCAST"])],
        "initial_hand": ["def-0", "def-8", "def-9"],
        "relics": [{"model_id": "CRACKED_CORE"}],
        "potions": [],
        "invoke_combat_entry_hooks": True,
        "capture_orbs": True
    },
    {
        "name": "necrobinder_summon",
        "character": "NECROBINDER",
        "encounter": "CORPSE_SLUGS_WEAK",
        "deck": [{"instance_id": f"necro-{i}", "model_id": m} for i, m in enumerate(["STRIKE_NECROBINDER"]*4 + ["DEFEND_NECROBINDER"]*4 + ["SUMMON_FORTH", "REANIMATE"])],
        "initial_hand": ["necro-0", "necro-8", "necro-9"],
        "relics": [],
        "potions": [],
        "invoke_combat_entry_hooks": True
    },
    {
        "name": "regent_stars",
        "character": "REGENT",
        "encounter": "SEAPUNK_WEAK",
        "deck": [{"instance_id": f"reg-{i}", "model_id": m} for i, m in enumerate(["STRIKE_REGENT"]*4 + ["DEFEND_REGENT"]*4 + ["SOVEREIGN_BLADE", "CLOAK_OF_STARS"])],
        "initial_hand": ["reg-0", "reg-8", "reg-9"],
        "relics": [],
        "potions": [],
        "invoke_combat_entry_hooks": True
    },
    {
        "name": "elite_terror_eel",
        "character": "IRONCLAD",
        "encounter": "TERROR_EEL_ELITE",
        "deck": [{"instance_id": f"ic-e-{i}", "model_id": m} for i, m in enumerate(["STRIKE_IRONCLAD"]*5 + ["DEFEND_IRONCLAD"]*4 + ["BASH"])],
        "initial_hand": ["ic-e-0", "ic-e-1", "ic-e-9"],
        "relics": [{"model_id": "BURNING_BLOOD"}],
        "potions": [],
        "invoke_combat_entry_hooks": True
    },
    {
        "name": "boss_waterfall_giant",
        "character": "SILENT",
        "encounter": "WATERFALL_GIANT_BOSS",
        "deck": [{"instance_id": f"sil-b-{i}", "model_id": m} for i, m in enumerate(["STRIKE_SILENT"]*5 + ["DEFEND_SILENT"]*5 + ["NEUTRALIZE", "SURVIVOR"])],
        "initial_hand": ["sil-b-0", "sil-b-10", "sil-b-11"],
        "relics": [{"model_id": "RING_OF_THE_SNAKE"}],
        "potions": [],
        "invoke_combat_entry_hooks": True
    },
    {
        "name": "choice_and_generation",
        "character": "IRONCLAD",
        "encounter": "first",
        "deck": [
            {"instance_id": "disc-0", "model_id": "DISCOVERY"},
            {"instance_id": "pur-0", "model_id": "PURITY"},
            {"instance_id": "acro-0", "model_id": "ACROBATICS"},
            {"instance_id": "ic-g-0", "model_id": "STRIKE_IRONCLAD"},
            {"instance_id": "ic-g-1", "model_id": "DEFEND_IRONCLAD"},
        ],
        "initial_hand": ["disc-0", "pur-0", "acro-0"],
        "relics": [],
        "potions": [],
        "invoke_combat_entry_hooks": True
    }
]

def create_scenario_payload(scenario_def: Dict[str, Any], seed: str) -> Dict[str, Any]:
    return {
        "game_build": {},
        "seed": seed,
        "rng_counters": {},
        "character": scenario_def["character"],
        "ascension": 0,
        "encounter": scenario_def["encounter"],
        "current_hp": 80,
        "max_hp": 80,
        "gold": 99,
        "deck": scenario_def["deck"],
        "initial_hand": scenario_def.get("initial_hand", []),
        "relics": scenario_def.get("relics", []),
        "potions": scenario_def.get("potions", []),
        "invoke_combat_entry_hooks": scenario_def.get("invoke_combat_entry_hooks", False),
        "capture_orbs": scenario_def.get("capture_orbs", False),
    }


# ─── Action Policies ─────────────────────────────────────────────────────────

def select_action(state: Dict[str, Any], policy: str = "greedy", rng: Optional[random.Random] = None) -> Optional[str]:
    legals = state.get("legal_actions", [])
    if not legals:
        return None

    if policy == "epsilon_random":
        assert rng is not None
        if rng.random() < 0.20:
            return rng.choice(legals)["action_id"]

    # 1. Blocking choice resolution (choose_cards / option selection)
    choices = [a for a in legals if a.get("kind") == "choose_cards"]
    if choices:
        return choices[0]["action_id"]

    # 2. Potions if available
    potions = [a for a in legals if a.get("kind") == "use_potion"]
    if potions and policy in ("greedy", "heuristic"):
        return potions[0]["action_id"]

    # 3. Policy differentiation
    if policy == "greedy":
        # Aggressive attacks first
        attacks = [a for a in legals if a.get("kind") == "play_card" and ("strike" in a["action_id"].lower() or "bash" in a["action_id"].lower() or "attack" in a.get("parameters", {}).get("card_type", "").lower())]
        if attacks:
            return attacks[0]["action_id"]
        cards = [a for a in legals if a.get("kind") == "play_card"]
        if cards:
            return cards[0]["action_id"]
    elif policy == "heuristic":
        # Skills / Powers / defensive first, then attacks
        skills = [a for a in legals if a.get("kind") == "play_card" and ("defend" in a["action_id"].lower() or "survivor" in a["action_id"].lower() or "zap" in a["action_id"].lower())]
        if skills:
            return skills[0]["action_id"]
        cards = [a for a in legals if a.get("kind") == "play_card"]
        if cards:
            return cards[0]["action_id"]
    else: # epsilon_random fallback
        cards = [a for a in legals if a.get("kind") == "play_card"]
        if cards:
            return cards[0]["action_id"]

    # 4. End turn
    ends = [a for a in legals if a.get("action_id") == "end_turn"]
    if ends:
        return ends[0]["action_id"]

    return legals[0]["action_id"]


# ─── Statistics Calculation ──────────────────────────────────────────────────

def compute_distribution(samples: List[float]) -> Dict[str, float]:
    if not samples:
        return {
            "count": 0, "min": 0.0, "p25": 0.0, "p50": 0.0, "p75": 0.0,
            "p90": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "mean": 0.0, "stdev": 0.0
        }
    s = sorted(samples)
    n = len(s)
    return {
        "count": n,
        "min": round(s[0], 4),
        "p25": round(s[int(n * 0.25)], 4),
        "p50": round(s[int(n * 0.50)], 4),
        "p75": round(s[int(n * 0.75)], 4),
        "p90": round(s[int(n * 0.90)], 4),
        "p95": round(s[int(n * 0.95)], 4),
        "p99": round(s[min(int(n * 0.99), n - 1)], 4),
        "max": round(s[-1], 4),
        "mean": round(statistics.mean(s), 4),
        "stdev": round(statistics.stdev(s), 4) if n > 1 else 0.0,
    }


# ─── Benchmark Engine Class ──────────────────────────────────────────────────

class RLRolloutBenchmarkEngine:
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.stability_events_file = self.output_dir / "rl-stability-events.jsonl"
        if self.stability_events_file.exists():
            self.stability_events_file.unlink()
        self.latency_samples_csv = self.output_dir / "rl-latency-samples.csv"
        self.worker_scaling_csv = self.output_dir / "rl-worker-scaling.csv"
        self.throughput_summary_json = self.output_dir / "rl-throughput-summary.json"
        self.determinism_report_json = self.output_dir / "rl-determinism-report.json"

        self.latency_records: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    def log_event(self, event_type: str, details: Dict[str, Any]) -> None:
        event = {
            "timestamp": time.time(),
            "iso_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event_type": event_type,
            "details": details
        }
        with self._lock:
            with open(self.stability_events_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(event) + "\n")

    def record_latency(self, phase: str, action_kind: str, duration_ms: float, worker_id: int = 0) -> None:
        rec = {
            "phase": phase,
            "action_kind": action_kind,
            "duration_ms": round(duration_ms, 4),
            "worker_id": worker_id
        }
        with self._lock:
            self.latency_records.append(rec)

    # ── Protocol Overhead ─────────────────────────────────────────────────────
    def benchmark_protocol_overhead(self, worker: NativeWorker, iterations: int = 200) -> Dict[str, Any]:
        print(f"\n[Micro-benchmark] Measuring raw IPC/protocol latency ({iterations} iterations)...")
        results = {}
        # Ensure worker is reset before observe
        worker.reset(create_scenario_payload(BENCHMARK_SCENARIOS[0], "PROTOCOL_BENCH_INIT"))
        for method_name, fn in [
            ("hello", worker.hello),
            ("catalog", worker.catalog),
            ("diagnostics", worker.diagnostics),
            ("observe", worker.observe),
        ]:
            durations = []
            for _ in range(iterations):
                t0 = time.perf_counter()
                fn()
                durations.append((time.perf_counter() - t0) * 1000)
                self.record_latency("protocol", method_name, durations[-1])
            results[method_name] = compute_distribution(durations)
            print(f"  {method_name:12s} p50: {results[method_name]['p50']:.3f} ms | p95: {results[method_name]['p95']:.3f} ms | mean: {results[method_name]['mean']:.3f} ms")
        return results

    # ── Workload A: Resident Stepping ─────────────────────────────────────────
    def run_workload_a_resident_stepping(self, target_transitions: int = 10000, num_workers: int = 4) -> Dict[str, Any]:
        print(f"\n[Workload A] Resident Stepping ({target_transitions} transitions across {num_workers} workers)...")
        samples_by_kind: Dict[str, List[float]] = defaultdict(list)
        total_transitions = 0
        total_combats_completed = 0

        t_start = time.perf_counter()
        with NativeWorkerPool(num_workers) as pool:
            # Warm up
            scenarios = [create_scenario_payload(BENCHMARK_SCENARIOS[i % len(BENCHMARK_SCENARIOS)], f"WARMUP_{i}") for i in range(num_workers)]
            states = pool.map(lambda w, s: w.reset(s), scenarios)

            def worker_step_loop(worker_idx: int, trans_to_run: int) -> Dict[str, Any]:
                w = pool.workers[worker_idx]
                rng = random.Random(42 + worker_idx)
                local_samples = defaultdict(list)
                completed = 0
                steps = 0
                cur_state = states[worker_idx]

                while steps < trans_to_run:
                    if cur_state.get("terminated") or not cur_state.get("legal_actions"):
                        completed += 1
                        sc = create_scenario_payload(BENCHMARK_SCENARIOS[rng.randint(0, len(BENCHMARK_SCENARIOS) - 1)], f"A_STEP_{worker_idx}_{steps}")
                        t0 = time.perf_counter()
                        cur_state = w.reset(sc)
                        dt = (time.perf_counter() - t0) * 1000
                        local_samples["reset"].append(dt)
                        self.record_latency("workload_a", "reset", dt, worker_idx)
                        continue

                    act_id = select_action(cur_state, "heuristic", rng)
                    if not act_id:
                        sc = create_scenario_payload(BENCHMARK_SCENARIOS[rng.randint(0, len(BENCHMARK_SCENARIOS) - 1)], f"A_STEP_NOL_{worker_idx}_{steps}")
                        cur_state = w.reset(sc)
                        continue

                    # Classify action kind
                    if act_id == "end_turn":
                        kind = "end_turn"
                    elif "choose_cards" in act_id or "choose" in act_id:
                        kind = "choice_resolution"
                    elif "use_potion" in act_id or "discard_potion" in act_id:
                        kind = "potion_action"
                    else:
                        kind = "card_action"

                    t0 = time.perf_counter()
                    cur_state = w.step(act_id)
                    dt = (time.perf_counter() - t0) * 1000
                    local_samples[kind].append(dt)
                    self.record_latency("workload_a", kind, dt, worker_idx)
                    steps += 1

                return {"samples": local_samples, "steps": steps, "completed": completed}

            steps_per_worker = target_transitions // num_workers
            with ThreadPoolExecutor(max_workers=num_workers) as executor:
                futures = [executor.submit(worker_step_loop, i, steps_per_worker) for i in range(num_workers)]
                for f in as_completed(futures):
                    res = f.result()
                    total_transitions += res["steps"]
                    total_combats_completed += res["completed"]
                    for k, v in res["samples"].items():
                        samples_by_kind[k].extend(v)

        total_elapsed = time.perf_counter() - t_start
        throughput = total_transitions / total_elapsed

        summary = {
            "target_transitions": target_transitions,
            "actual_transitions": total_transitions,
            "elapsed_seconds": round(total_elapsed, 3),
            "transitions_per_second": round(throughput, 2),
            "combats_completed": total_combats_completed,
            "latency_by_action_kind": {k: compute_distribution(v) for k, v in samples_by_kind.items()}
        }

        print(f"  Completed {total_transitions} transitions in {total_elapsed:.2f}s ({throughput:.1f} trans/sec)")
        for k, dist in summary["latency_by_action_kind"].items():
            print(f"    {k:20s} (N={dist['count']}): p50={dist['p50']:.3f}ms | p95={dist['p95']:.3f}ms | p99={dist['p99']:.3f}ms | mean={dist['mean']:.3f}ms")

        return summary

    # ── Workload B: Terminal Combat Rollouts ───────────────────────────────────
    def run_workload_b_terminal_rollouts(self, target_combats: int = 1000, num_workers: int = 8) -> Dict[str, Any]:
        print(f"\n[Workload B] Terminal Combat Rollouts ({target_combats} episodes across {num_workers} workers)...")
        policies = ["greedy", "heuristic", "epsilon_random"]
        policy_counts = Counter()
        victories = Counter()
        defeats = Counter()
        total_decisions = 0
        total_turns = 0
        no_progress_count = 0
        zero_legal_non_terminal = 0
        errors: List[Dict[str, Any]] = []

        combat_durations = []
        decision_counts = []
        turn_counts = []

        t_start = time.perf_counter()
        with NativeWorkerPool(num_workers) as pool:
            def combat_worker_loop(worker_idx: int, combats_to_run: int) -> Dict[str, Any]:
                w = pool.workers[worker_idx]
                rng = random.Random(100 + worker_idx)
                local_results = []

                for c_idx in range(combats_to_run):
                    policy = policies[(c_idx + worker_idx) % len(policies)]
                    sc_def = BENCHMARK_SCENARIOS[rng.randint(0, len(BENCHMARK_SCENARIOS) - 1)]
                    seed = f"WORKLOAD_B_{worker_idx}_{c_idx}_{policy}"
                    payload = create_scenario_payload(sc_def, seed)

                    c_start = time.perf_counter()
                    t0_reset = time.perf_counter()
                    state = w.reset(payload)
                    dt_reset = (time.perf_counter() - t0_reset) * 1000
                    self.record_latency("workload_b", "reset", dt_reset, worker_idx)

                    steps = 0
                    turns = 1
                    err = None
                    initial_hp = state.get("observation", {}).get("player", {}).get("hp", 80)
                    last_hash = state.get("state_hash")
                    stalled_steps = 0

                    while not state.get("terminated") and steps < 200:
                        legals = state.get("legal_actions", [])
                        if not legals:
                            zero_legal_non_terminal_flag = True
                            self.log_event("zero_legal_non_terminal", {"worker": worker_idx, "state": state})
                            break

                        act_id = select_action(state, policy, rng)
                        t0_step = time.perf_counter()
                        try:
                            state = w.step(act_id)
                        except Exception as e:
                            err = str(e)
                            self.log_event("combat_error", {"worker": worker_idx, "error": str(e), "action": act_id})
                            break
                        dt_step = (time.perf_counter() - t0_step) * 1000
                        kind = "end_turn" if act_id == "end_turn" else ("choice" if "choose" in act_id else "card")
                        self.record_latency("workload_b", kind, dt_step, worker_idx)

                        steps += 1
                        cur_turn = state.get("observation", {}).get("combat", {}).get("turn", turns)
                        if cur_turn > turns:
                            turns = cur_turn

                        if state.get("state_hash") == last_hash:
                            stalled_steps += 1
                        else:
                            stalled_steps = 0
                        last_hash = state.get("state_hash")

                        if stalled_steps >= 50:
                            no_progress_flag = True
                            self.log_event("no_progress_transition", {"worker": worker_idx, "steps": steps})
                            break

                    c_elapsed = (time.perf_counter() - c_start) * 1000
                    is_victory = bool(state.get("victory", False))
                    is_term = bool(state.get("terminated", False))

                    local_results.append({
                        "policy": policy,
                        "scenario": sc_def["name"],
                        "victory": is_victory,
                        "terminated": is_term,
                        "steps": steps,
                        "turns": turns,
                        "duration_ms": c_elapsed,
                        "error": err
                    })

                return local_results

            combats_per_worker = target_combats // num_workers
            with ThreadPoolExecutor(max_workers=num_workers) as executor:
                futures = [executor.submit(combat_worker_loop, i, combats_per_worker) for i in range(num_workers)]
                for f in as_completed(futures):
                    for res in f.result():
                        pol = res["policy"]
                        policy_counts[pol] += 1
                        if res["victory"]:
                            victories[pol] += 1
                        elif res["terminated"]:
                            defeats[pol] += 1
                        if res["error"]:
                            errors.append(res)
                        total_decisions += res["steps"]
                        total_turns += res["turns"]
                        combat_durations.append(res["duration_ms"])
                        decision_counts.append(res["steps"])
                        turn_counts.append(res["turns"])

        total_elapsed = time.perf_counter() - t_start
        total_combats_run = sum(policy_counts.values())
        combats_per_minute = (total_combats_run / total_elapsed) * 60.0
        decisions_per_sec = total_decisions / total_elapsed

        summary = {
            "target_combats": target_combats,
            "actual_combats": total_combats_run,
            "elapsed_seconds": round(total_elapsed, 3),
            "combats_per_minute": round(combats_per_minute, 2),
            "decisions_per_second": round(decisions_per_sec, 2),
            "total_decisions": total_decisions,
            "total_turns": total_turns,
            "policy_breakdown": {
                p: {
                    "total": policy_counts[p],
                    "victories": victories[p],
                    "defeats": defeats[p],
                    "win_rate": round(victories[p] / max(1, policy_counts[p]), 4)
                } for p in policies
            },
            "combat_duration_ms": compute_distribution(combat_durations),
            "decisions_per_combat": compute_distribution(decision_counts),
            "turns_per_combat": compute_distribution(turn_counts),
            "errors_count": len(errors),
            "no_progress_transitions": no_progress_count,
            "zero_legal_non_terminal_count": zero_legal_non_terminal,
        }

        print(f"  Finished {total_combats_run} combats in {total_elapsed:.2f}s ({combats_per_minute:.1f} combats/min, {decisions_per_sec:.1f} decisions/sec)")
        print(f"  Victories: {sum(victories.values())} | Defeats: {sum(defeats.values())} | Errors: {len(errors)}")
        print(f"  Decisions/combat mean: {summary['decisions_per_combat']['mean']:.1f} | Duration mean: {summary['combat_duration_ms']['mean']:.1f}ms")

        return summary

    # ── Workload C: Reset Churn ───────────────────────────────────────────────
    def run_workload_c_reset_churn(self, target_resets: int = 2000, num_workers: int = 8) -> Dict[str, Any]:
        print(f"\n[Workload C] Reset Churn ({target_resets} resets across {num_workers} workers)...")
        reset_latencies = []
        first_step_latencies = []
        hash_check_divergences = 0
        state_leakage_detected = 0
        worker_poisonings = 0
        worker_replacements = 0

        t_start = time.perf_counter()
        with NativeWorkerPool(num_workers) as pool:
            # Check identical reset reproducibility
            test_sc = create_scenario_payload(BENCHMARK_SCENARIOS[0], "REPRO_FIXED_SEED")
            initial_check = pool.reset_all(test_sc)
            initial_hashes = [s["state_hash"] for s in initial_check]
            if len(set(initial_hashes)) != 1:
                hash_check_divergences += 1
                self.log_event("reset_hash_divergence_initial", {"hashes": initial_hashes})

            def reset_worker_loop(worker_idx: int, resets_to_run: int) -> Dict[str, Any]:
                w = pool.workers[worker_idx]
                rng = random.Random(200 + worker_idx)
                local_resets = []
                local_steps = []
                local_hash_mismatches = 0
                local_poisonings = 0

                for r_idx in range(resets_to_run):
                    sc_def = BENCHMARK_SCENARIOS[r_idx % len(BENCHMARK_SCENARIOS)]
                    seed = f"RESET_CHURN_{worker_idx}_{r_idx}"
                    payload = create_scenario_payload(sc_def, seed)

                    t0 = time.perf_counter()
                    try:
                        state = w.reset(payload)
                        dt_reset = (time.perf_counter() - t0) * 1000
                        local_resets.append(dt_reset)
                        self.record_latency("workload_c", "reset", dt_reset, worker_idx)
                    except Exception as e:
                        local_poisonings += 1
                        self.log_event("reset_poisoning", {"worker": worker_idx, "error": str(e)})
                        continue

                    # Verify deterministic reset hash by doing second identical reset on same worker
                    if r_idx % 20 == 0:
                        state_again = w.reset(payload)
                        if state_again["state_hash"] != state["state_hash"]:
                            local_hash_mismatches += 1
                            self.log_event("identical_reset_hash_mismatch", {
                                "worker": worker_idx, "seed": seed,
                                "hash1": state["state_hash"], "hash2": state_again["state_hash"]
                            })
                        state = state_again

                    # Take at least 1 native action after reset
                    if state.get("legal_actions"):
                        act = select_action(state, "greedy", rng)
                        t0_step = time.perf_counter()
                        try:
                            s1 = w.step(act)
                            dt_step = (time.perf_counter() - t0_step) * 1000
                            local_steps.append(dt_step)
                            self.record_latency("workload_c", "first_action", dt_step, worker_idx)
                        except Exception as e:
                            local_poisonings += 1
                            self.log_event("step_after_reset_error", {"worker": worker_idx, "error": str(e)})

                return {
                    "resets": local_resets,
                    "steps": local_steps,
                    "hash_mismatches": local_hash_mismatches,
                    "poisonings": local_poisonings
                }

            resets_per_worker = target_resets // num_workers
            with ThreadPoolExecutor(max_workers=num_workers) as executor:
                futures = [executor.submit(reset_worker_loop, i, resets_per_worker) for i in range(num_workers)]
                for f in as_completed(futures):
                    res = f.result()
                    reset_latencies.extend(res["resets"])
                    first_step_latencies.extend(res["steps"])
                    hash_check_divergences += res["hash_mismatches"]
                    worker_poisonings += res["poisonings"]

            # Final memory check on workers
            memory_per_worker = [w.memory_bytes for w in pool.workers]

        total_elapsed = time.perf_counter() - t_start
        resets_per_sec = len(reset_latencies) / total_elapsed

        summary = {
            "target_resets": target_resets,
            "actual_resets": len(reset_latencies),
            "elapsed_seconds": round(total_elapsed, 3),
            "resets_per_second": round(resets_per_sec, 2),
            "reset_latency_ms": compute_distribution(reset_latencies),
            "first_step_latency_ms": compute_distribution(first_step_latencies),
            "hash_divergences": hash_check_divergences,
            "state_leakage_detected": state_leakage_detected,
            "worker_poisoning_events": worker_poisonings,
            "worker_replacements": worker_replacements,
            "memory_bytes_per_worker_after_churn": memory_per_worker,
            "avg_memory_mb_per_worker": round(statistics.mean(memory_per_worker) / (1024 * 1024), 2)
        }

        print(f"  Completed {len(reset_latencies)} resets in {total_elapsed:.2f}s ({resets_per_sec:.1f} resets/sec)")
        print(f"  Reset latency p50={summary['reset_latency_ms']['p50']:.3f}ms | p95={summary['reset_latency_ms']['p95']:.3f}ms | mean={summary['reset_latency_ms']['mean']:.3f}ms")
        print(f"  First action latency p50={summary['first_step_latency_ms']['p50']:.3f}ms | p95={summary['first_step_latency_ms']['p95']:.3f}ms")
        print(f"  Memory after churn: {summary['avg_memory_mb_per_worker']:.2f} MB/worker | Poisoning events: {worker_poisonings} | Hash divergences: {hash_check_divergences}")

        return summary

    # ── Workload D: Branch / Fork / Restore ────────────────────────────────────
    def run_workload_d_branch_fork_restore(self, iterations: int = 50) -> Dict[str, Any]:
        print(f"\n[Workload D] Branch / Fork / Restore Micro-benchmarks ({iterations} iterations)...")
        fork_times = []
        resident_step_times = []
        resident_cont_times = []
        restore_depth_times: Dict[int, List[float]] = defaultdict(list)
        portable_cycle_times = []

        sc_def = BENCHMARK_SCENARIOS[0]
        payload = create_scenario_payload(sc_def, "WORKLOAD_D_BENCH")

        with NativeWorker() as w1, NativeWorker() as w2:
            # Warm up
            init_state = w1.reset(payload)

            for it in range(iterations):
                # 1. Measure fork
                t0 = time.perf_counter()
                fork_handle = w1.fork()
                dt_fork = (time.perf_counter() - t0) * 1000
                fork_times.append(dt_fork)
                self.record_latency("workload_d", "fork", dt_fork)

                # 2. Resident child step
                legals = w1.legal_actions()
                act = select_action({"legal_actions": legals}, "greedy")
                t0 = time.perf_counter()
                child_state = w1.step(act)
                dt_step = (time.perf_counter() - t0) * 1000
                resident_step_times.append(dt_step)
                self.record_latency("workload_d", "resident_child_step", dt_step)

                # 3. Resident restore
                t0 = time.perf_counter()
                restored = w1.restore(fork_handle)
                dt_rest = (time.perf_counter() - t0) * 1000
                resident_cont_times.append(dt_rest)
                self.record_latency("workload_d", "resident_restore", dt_rest)

                # 4. Portable export / replay cycle across workers
                portable = w1.export_branch()
                t0 = time.perf_counter()
                # Replay on w2
                req = portable.get("reset_request", {"method": "reset", "params": portable["reset"]})
                res_w2 = w2.request(req["method"], req["params"])
                w2._record_reset(req["method"], req["params"], req["params"].get("state", req["params"]), res_w2)
                for h_act in portable["history"]:
                    res_w2 = w2.step(h_act)
                dt_portable = (time.perf_counter() - t0) * 1000
                portable_cycle_times.append(dt_portable)
                self.record_latency("workload_d", "portable_restore_cycle", dt_portable)

            # Depth restore benchmarking (0, 8, 16, 32, 64)
            print("  Measuring restore latency across history depths (0, 8, 16, 32, 64)...")
            depths = [0, 8, 16, 32, 64]
            long_sc = {
                "game_build": {}, "seed": "DEPTH_BENCH_SEED", "rng_counters": {},
                "character": "SILENT", "ascension": 0, "encounter": "WATERFALL_GIANT_BOSS",
                "current_hp": 200, "max_hp": 200, "gold": 99,
                "deck": [{"instance_id": f"sil-{i}", "model_id": "DEFEND_SILENT"} for i in range(10)] + [{"instance_id": f"st-{i}", "model_id": "STRIKE_SILENT"} for i in range(5)],
                "initial_hand": [], "relics": [], "potions": [],
                "invoke_combat_entry_hooks": True
            }
            state = w1.reset(long_sc)
            depth_handles: Dict[int, str] = {0: state["state_handle"]}

            # Step up to 64 steps
            cur = state
            for step_i in range(1, 65):
                act = select_action(cur, "heuristic", random.Random(step_i))
                if not act:
                    act = "end_turn"
                cur = w1.step(act)
                if step_i in depths:
                    depth_handles[step_i] = cur["state_handle"]

            # Now benchmark restores for each depth
            for d in depths:
                if d in depth_handles:
                    h = depth_handles[d]
                    for _ in range(iterations):
                        t0 = time.perf_counter()
                        w1.restore(h)
                        dt_d = (time.perf_counter() - t0) * 1000
                        restore_depth_times[d].append(dt_d)
                        self.record_latency("workload_d", f"restore_depth_{d}", dt_d)

        summary = {
            "fork_latency_ms": compute_distribution(fork_times),
            "resident_child_step_ms": compute_distribution(resident_step_times),
            "resident_restore_ms": compute_distribution(resident_cont_times),
            "portable_restore_cycle_ms": compute_distribution(portable_cycle_times),
            "restore_by_history_depth_ms": {
                f"depth_{d}": compute_distribution(restore_depth_times[d]) for d in sorted(restore_depth_times.keys())
            }
        }

        print(f"  Fork p50: {summary['fork_latency_ms']['p50']:.3f}ms | p95: {summary['fork_latency_ms']['p95']:.3f}ms")
        print(f"  Resident Restore p50: {summary['resident_restore_ms']['p50']:.3f}ms | p95: {summary['resident_restore_ms']['p95']:.3f}ms")
        for d_str, dist in summary["restore_by_history_depth_ms"].items():
            print(f"    Restore {d_str:10s} p50: {dist['p50']:.3f}ms | p95: {dist['p95']:.3f}ms | mean: {dist['mean']:.3f}ms")
        print(f"  Portable Replay Cycle p50: {summary['portable_restore_cycle_ms']['p50']:.3f}ms | p95: {summary['portable_restore_cycle_ms']['p95']:.3f}ms")

        return summary

    # ── Workload E: Long-duration Stability ───────────────────────────────────
    def run_workload_e_stability(self, target_decisions: int = 50000, num_workers: int = 16) -> Dict[str, Any]:
        print(f"\n[Workload E] Long-duration Stability ({target_decisions} decisions across {num_workers} workers)...")
        crashes = 0
        timeouts = 0
        desyncs = 0
        poisonings = 0
        replay_divergences = 0
        unsafe_abandonments = 0
        pid_replacements = 0

        memory_samples = []
        cpu_samples = []
        stop_monitoring = threading.Event()

        def monitor_resources():
            while not stop_monitoring.is_set():
                mem = psutil.virtual_memory()
                cpu = psutil.cpu_percent(interval=None)
                memory_samples.append({
                    "time": time.time(),
                    "used_percent": mem.percent,
                    "available_gb": round(mem.available / (1024**3), 2),
                })
                cpu_samples.append(cpu)
                time.sleep(0.5)

        t_mon = threading.Thread(target=monitor_resources, daemon=True)
        t_mon.start()

        t_start = time.perf_counter()
        completed_decisions = 0
        combats_completed = 0

        with NativeWorkerPool(num_workers) as pool:
            initial_pids = [w.process.pid for w in pool.workers]

            # Measure memory after warmup
            initial_sc = [create_scenario_payload(BENCHMARK_SCENARIOS[i % len(BENCHMARK_SCENARIOS)], f"WARM_{i}") for i in range(num_workers)]
            states = pool.map(lambda w, s: w.reset(s), initial_sc)
            warmup_memory_per_worker = [w.memory_bytes for w in pool.workers]

            def worker_stability_loop(worker_idx: int, decisions_target: int) -> Dict[str, Any]:
                nonlocal crashes, timeouts, desyncs, poisonings, replay_divergences, unsafe_abandonments, pid_replacements
                w = pool.workers[worker_idx]
                rng = random.Random(500 + worker_idx)
                cur_state = states[worker_idx]
                done_steps = 0
                done_combats = 0

                while done_steps < decisions_target:
                    if cur_state.get("terminated") or not cur_state.get("legal_actions"):
                        done_combats += 1
                        sc_payload = create_scenario_payload(BENCHMARK_SCENARIOS[rng.randint(0, len(BENCHMARK_SCENARIOS) - 1)], f"STAB_{worker_idx}_{done_steps}")
                        try:
                            cur_state = w.reset(sc_payload)
                        except Exception as e:
                            self.log_event("stability_reset_error", {"worker": worker_idx, "error": str(e)})
                            # Check if worker dead
                            if w.process.poll() is not None:
                                crashes += 1
                                pid_replacements += 1
                                w = pool._replace_if_dead(worker_idx)
                            continue

                    act_id = select_action(cur_state, "greedy" if rng.random() > 0.3 else "heuristic", rng)
                    if not act_id:
                        cur_state = w.reset(create_scenario_payload(BENCHMARK_SCENARIOS[0], f"STAB_NOL_{worker_idx}_{done_steps}"))
                        continue

                    try:
                        cur_state = w.step(act_id)
                        done_steps += 1
                    except NativeSimError as e:
                        if e.code == "worker_crashed":
                            crashes += 1
                        elif e.code == "request_timeout":
                            timeouts += 1
                        elif e.code == "protocol_desync":
                            desyncs += 1
                        elif e.code == "worker_poisoned":
                            poisonings += 1
                        elif e.code == "replay_divergence":
                            replay_divergences += 1
                        elif e.code == "unsafe_transition_abandon":
                            unsafe_abandonments += 1
                        self.log_event("native_sim_error", {"worker": worker_idx, "code": e.code, "msg": str(e)})
                        if w.process.poll() is not None:
                            pid_replacements += 1
                            w = pool._replace_if_dead(worker_idx)
                        cur_state = w.reset(create_scenario_payload(BENCHMARK_SCENARIOS[0], f"STAB_RECOVER_{worker_idx}_{done_steps}"))
                    except Exception as e:
                        self.log_event("unexpected_worker_error", {"worker": worker_idx, "error": str(e)})
                        if w.process.poll() is not None:
                            crashes += 1
                            pid_replacements += 1
                            w = pool._replace_if_dead(worker_idx)
                        cur_state = w.reset(create_scenario_payload(BENCHMARK_SCENARIOS[0], f"STAB_RECOVER_{worker_idx}_{done_steps}"))

                return {"steps": done_steps, "combats": done_combats}

            steps_per_w = target_decisions // num_workers
            with ThreadPoolExecutor(max_workers=num_workers) as executor:
                futures = [executor.submit(worker_stability_loop, i, steps_per_w) for i in range(num_workers)]
                for f in as_completed(futures):
                    res = f.result()
                    completed_decisions += res["steps"]
                    combats_completed += res["combats"]

            final_pids = [w.process.pid for w in pool.workers]
            final_memory_per_worker = [w.memory_bytes for w in pool.workers]

        stop_monitoring.set()
        t_mon.join(timeout=2)

        total_elapsed = time.perf_counter() - t_start
        throughput = completed_decisions / total_elapsed

        summary = {
            "target_decisions": target_decisions,
            "actual_decisions": completed_decisions,
            "combats_completed": combats_completed,
            "elapsed_seconds": round(total_elapsed, 3),
            "decisions_per_second": round(throughput, 2),
            "worker_crashes": crashes,
            "request_timeouts": timeouts,
            "protocol_desyncs": desyncs,
            "worker_poisonings": poisonings,
            "replay_divergences": replay_divergences,
            "unsafe_abandonments": unsafe_abandonments,
            "pid_replacements": pid_replacements,
            "initial_pids": initial_pids,
            "final_pids": final_pids,
            "warmup_memory_per_worker_mb": [round(m / (1024*1024), 2) for m in warmup_memory_per_worker],
            "final_memory_per_worker_mb": [round(m / (1024*1024), 2) for m in final_memory_per_worker],
            "avg_memory_growth_mb": round((statistics.mean(final_memory_per_worker) - statistics.mean(warmup_memory_per_worker)) / (1024*1024), 3),
            "system_cpu_utilization_mean": round(statistics.mean(cpu_samples), 2) if cpu_samples else 0.0,
            "system_ram_used_percent_peak": max([s["used_percent"] for s in memory_samples]) if memory_samples else 0.0,
        }

        print(f"  Completed {completed_decisions} decisions in {total_elapsed:.2f}s ({throughput:.1f} dec/sec)")
        print(f"  Crashes: {crashes} | Timeouts: {timeouts} | Desyncs: {desyncs} | Poisonings: {poisonings} | PID replacements: {pid_replacements}")
        print(f"  Warmup RAM: {statistics.mean(summary['warmup_memory_per_worker_mb']):.2f} MB/worker -> Final: {statistics.mean(summary['final_memory_per_worker_mb']):.2f} MB/worker (growth: {summary['avg_memory_growth_mb']} MB)")
        print(f"  System CPU avg: {summary['system_cpu_utilization_mean']}% | RAM Peak: {summary['system_ram_used_percent_peak']}%")

        return summary

    # ── Determinism Check ─────────────────────────────────────────────────────
    def run_determinism_check(self, trajectories: int = 100, max_steps: int = 25) -> Dict[str, Any]:
        print(f"\n[Determinism Check] Validating exact bit-for-bit trajectory reproduction ({trajectories} trajectories)...")
        mismatches = []
        verified_trajectories = 0
        total_steps_checked = 0

        with NativeWorker() as w1, NativeWorker() as w2:
            for t_idx in range(trajectories):
                sc_def = BENCHMARK_SCENARIOS[t_idx % len(BENCHMARK_SCENARIOS)]
                seed = f"DETERMINISM_TEST_{t_idx}_{sc_def['character']}"
                payload = create_scenario_payload(sc_def, seed)

                # Reset both workers
                s1 = w1.reset(payload)
                s2 = w2.reset(payload)

                if s1["state_hash"] != s2["state_hash"]:
                    mismatches.append({
                        "trajectory": t_idx,
                        "step": 0,
                        "scenario": sc_def["name"],
                        "seed": seed,
                        "hash_w1": s1["state_hash"],
                        "hash_w2": s2["state_hash"],
                        "reason": "initial_reset_mismatch"
                    })
                    continue

                # Run sequence of identical actions
                rng = random.Random(999 + t_idx)
                trajectory_ok = True

                for step_idx in range(1, max_steps + 1):
                    if s1.get("terminated") or not s1.get("legal_actions"):
                        break

                    act = select_action(s1, "epsilon_random", rng)
                    s1 = w1.step(act)
                    s2 = w2.step(act)
                    total_steps_checked += 1

                    if s1["state_hash"] != s2["state_hash"]:
                        trajectory_ok = False
                        mismatches.append({
                            "trajectory": t_idx,
                            "step": step_idx,
                            "action": act,
                            "scenario": sc_def["name"],
                            "seed": seed,
                            "hash_w1": s1["state_hash"],
                            "hash_w2": s2["state_hash"],
                            "reason": "step_hash_mismatch"
                        })
                        break

                if trajectory_ok:
                    verified_trajectories += 1

        summary = {
            "trajectories_evaluated": trajectories,
            "trajectories_verified": verified_trajectories,
            "total_steps_checked": total_steps_checked,
            "mismatch_count": len(mismatches),
            "mismatches": mismatches,
            "pass_rate": round(verified_trajectories / max(1, trajectories), 4),
            "is_100_percent_deterministic": (len(mismatches) == 0 and verified_trajectories == trajectories)
        }

        print(f"  Verified {verified_trajectories}/{trajectories} trajectories ({total_steps_checked} steps checked) -> 100% Deterministic: {summary['is_100_percent_deterministic']}")
        if mismatches:
            print(f"  WARNING: {len(mismatches)} mismatches detected!")

        return summary

    # ── Worker Scaling Matrix ─────────────────────────────────────────────────
    def run_worker_scaling_matrix(self, worker_counts: List[int]) -> List[Dict[str, Any]]:
        print("\n" + "="*80)
        print(f"RUNNING WORKER SCALING MATRIX: {worker_counts}")
        print("="*80)

        scaling_results = []

        for count in worker_counts:
            # Memory safety guard
            mem = psutil.virtual_memory()
            if mem.percent > 80.0:
                print(f"  [SAFETY GUARD] System RAM utilization ({mem.percent}%) exceeds 80% threshold. Halting scaling matrix expansion.")
                break

            print(f"\nEvaluating scaling with {count} worker(s)...")
            t_startup_0 = time.perf_counter()
            with NativeWorkerPool(count) as pool:
                startup_wall_time_ms = (time.perf_counter() - t_startup_0) * 1000

                # Warmup reset
                warmup_sc = [create_scenario_payload(BENCHMARK_SCENARIOS[i % len(BENCHMARK_SCENARIOS)], f"SCALE_WARM_{count}_{i}") for i in range(count)]
                t_reset_0 = time.perf_counter()
                states = pool.map(lambda w, s: w.reset(s), warmup_sc)
                reset_wall_time_ms = (time.perf_counter() - t_reset_0) * 1000

                # Memory per worker
                mem_per_worker_bytes = [w.memory_bytes for w in pool.workers]
                total_ws_mb = sum(mem_per_worker_bytes) / (1024 * 1024)
                ws_per_worker_mb = (total_ws_mb / count) if count > 0 else 0

                # Measure throughput and latencies over a timed benchmark burst
                action_latencies = []
                reset_latencies = []
                end_turn_latencies = []
                combats_completed = 0
                total_steps = 0

                cpu_samples = []
                stop_cpu = threading.Event()

                def sample_cpu():
                    while not stop_cpu.is_set():
                        cpu_samples.append(psutil.cpu_percent(interval=None))
                        time.sleep(0.2)

                t_cpu = threading.Thread(target=sample_cpu, daemon=True)
                t_cpu.start()

                t_burst_start = time.perf_counter()
                target_steps_per_worker = max(200, 4000 // count)

                def scaling_worker_burst(worker_idx: int, steps_target: int) -> Dict[str, Any]:
                    w = pool.workers[worker_idx]
                    rng = random.Random(300 + worker_idx)
                    cur_st = states[worker_idx]
                    loc_act_lat = []
                    loc_rst_lat = []
                    loc_et_lat = []
                    loc_combats = 0
                    loc_steps = 0

                    while loc_steps < steps_target:
                        if cur_st.get("terminated") or not cur_st.get("legal_actions"):
                            loc_combats += 1
                            sc = create_scenario_payload(BENCHMARK_SCENARIOS[rng.randint(0, len(BENCHMARK_SCENARIOS)-1)], f"SCALE_{count}_{worker_idx}_{loc_steps}")
                            t0 = time.perf_counter()
                            cur_st = w.reset(sc)
                            loc_rst_lat.append((time.perf_counter() - t0) * 1000)
                            continue

                        act = select_action(cur_st, "greedy", rng)
                        t0 = time.perf_counter()
                        cur_st = w.step(act)
                        dt = (time.perf_counter() - t0) * 1000

                        if act == "end_turn":
                            loc_et_lat.append(dt)
                        else:
                            loc_act_lat.append(dt)

                        loc_steps += 1

                    return {
                        "steps": loc_steps, "combats": loc_combats,
                        "act_lat": loc_act_lat, "rst_lat": loc_rst_lat, "et_lat": loc_et_lat
                    }

                with ThreadPoolExecutor(max_workers=count) as executor:
                    futures = [executor.submit(scaling_worker_burst, i, target_steps_per_worker) for i in range(count)]
                    for f in as_completed(futures):
                        res = f.result()
                        total_steps += res["steps"]
                        combats_completed += res["combats"]
                        action_latencies.extend(res["act_lat"])
                        reset_latencies.extend(res["rst_lat"])
                        end_turn_latencies.extend(res["et_lat"])

                burst_elapsed = time.perf_counter() - t_burst_start
                stop_cpu.set()
                t_cpu.join(timeout=2)

                transitions_per_sec = total_steps / burst_elapsed
                combats_per_min = (combats_completed / burst_elapsed) * 60.0
                cpu_util_mean = statistics.mean(cpu_samples) if cpu_samples else 0.0

                act_dist = compute_distribution(action_latencies)
                rst_dist = compute_distribution(reset_latencies)
                et_dist = compute_distribution(end_turn_latencies)

                rec = {
                    "worker_count": count,
                    "startup_wall_time_ms": round(startup_wall_time_ms, 2),
                    "total_working_set_mb": round(total_ws_mb, 2),
                    "working_set_per_worker_mb": round(ws_per_worker_mb, 2),
                    "cpu_utilization_percent": round(cpu_util_mean, 2),
                    "transitions_per_sec": round(transitions_per_sec, 2),
                    "combats_per_minute": round(combats_per_min, 2),
                    "terminal_episodes_per_minute": round(combats_per_min, 2),
                    "p50_action_latency_ms": act_dist["p50"],
                    "p95_action_latency_ms": act_dist["p95"],
                    "p99_action_latency_ms": act_dist["p99"],
                    "p50_reset_latency_ms": rst_dist["p50"],
                    "p95_reset_latency_ms": rst_dist["p95"],
                    "p50_end_turn_latency_ms": et_dist["p50"],
                    "p95_end_turn_latency_ms": et_dist["p95"],
                }
                scaling_results.append(rec)

                print(f"  Workers: {count:2d} | Throughput: {transitions_per_sec:7.1f} trans/sec | Combats: {combats_per_min:6.1f}/min | CPU: {cpu_util_mean:4.1f}% | RAM: {total_ws_mb:6.1f}MB ({ws_per_worker_mb:4.1f}MB/w)")
                print(f"           Action p50={act_dist['p50']:.2f}ms p95={act_dist['p95']:.2f}ms | Reset p50={rst_dist['p50']:.2f}ms | EndTurn p50={et_dist['p50']:.2f}ms")

        return scaling_results

    # ── Master Benchmark Runner ───────────────────────────────────────────────
    def run_all(self, scaling_counts: List[int] = [1, 2, 4, 8, 12, 16, 20]) -> Dict[str, Any]:
        master_t0 = time.perf_counter()
        preflight = get_preflight_info()
        print("="*80)
        print("DIVINE-STS2 REINFORCEMENT LEARNING ROLLOUT ENGINE BENCHMARK SUITE")
        print("="*80)
        print(f"Host: {preflight['cpu_model']} ({preflight['cpu_physical_cores']}C / {preflight['cpu_logical_cores']}T)")
        print(f"RAM: {preflight['physical_ram_total_gb']} GB (Available: {preflight['physical_ram_available_gb']} GB)")
        print(f"OS: {preflight['operating_system']} | GPU: {preflight['gpu_info']}")
        print(f"Git Commit: {preflight['git_commit_sha']} (Clean: {preflight['working_tree_clean']})")
        print(f".NET: {preflight['dotnet_version']} | Python: {preflight['python_version'].split()[0]}")
        print(f"Assembly SHA-256: {preflight['assembly_sha256']}")
        print(f"PCK SHA-256:      {preflight['pck_sha256']}")
        print("="*80)

        # 1. Micro-benchmark protocol overhead with single worker
        with NativeWorker() as single_worker:
            protocol_overhead = self.benchmark_protocol_overhead(single_worker, iterations=100)

        # 2. Workload A: Resident stepping
        workload_a = self.run_workload_a_resident_stepping(target_transitions=10000, num_workers=8)

        # 3. Workload B: Terminal combat rollouts
        workload_b = self.run_workload_b_terminal_rollouts(target_combats=1000, num_workers=8)

        # 4. Workload C: Reset churn
        workload_c = self.run_workload_c_reset_churn(target_resets=2000, num_workers=8)

        # 5. Workload D: Branch / fork / restore
        workload_d = self.run_workload_d_branch_fork_restore(iterations=50)

        # 6. Workload E: Long-duration stability
        workload_e = self.run_workload_e_stability(target_decisions=50000, num_workers=16)

        # 7. Determinism check
        determinism = self.run_determinism_check(trajectories=100, max_steps=25)

        # 8. Worker scaling matrix
        scaling_matrix = self.run_worker_scaling_matrix(scaling_counts)

        master_elapsed = time.perf_counter() - master_t0

        # Save all artifacts
        full_summary = {
            "benchmark_execution_time_seconds": round(master_elapsed, 2),
            "preflight": preflight,
            "protocol_overhead": protocol_overhead,
            "workload_a_resident_stepping": workload_a,
            "workload_b_terminal_combat_rollouts": workload_b,
            "workload_c_reset_churn": workload_c,
            "workload_d_branch_fork_restore": workload_d,
            "workload_e_long_duration_stability": workload_e,
            "determinism_validation": determinism,
            "worker_scaling_matrix": scaling_matrix,
        }

        # Write JSON throughput summary
        with open(self.throughput_summary_json, "w", encoding="utf-8") as f:
            json.dump(full_summary, f, indent=2)

        # Write JSON determinism report
        with open(self.determinism_report_json, "w", encoding="utf-8") as f:
            json.dump(determinism, f, indent=2)

        # Write CSV worker scaling
        if scaling_matrix:
            keys = list(scaling_matrix[0].keys())
            with open(self.worker_scaling_csv, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=keys)
                writer.writeheader()
                writer.writerows(scaling_matrix)

        # Write CSV latency samples
        if self.latency_records:
            keys = ["phase", "action_kind", "duration_ms", "worker_id"]
            with open(self.latency_samples_csv, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=keys)
                writer.writeheader()
                writer.writerows(self.latency_records)

        print("\n" + "="*80)
        print("BENCHMARK SUITE COMPLETED SUCCESSFULLY!")
        print(f"Total benchmark runtime: {master_elapsed:.2f} seconds")
        print(f"Artifacts generated under: {self.output_dir.resolve()}")
        print(f"  - {self.throughput_summary_json.name}")
        print(f"  - {self.latency_samples_csv.name} ({len(self.latency_records)} samples)")
        print(f"  - {self.worker_scaling_csv.name}")
        print(f"  - {self.stability_events_file.name}")
        print(f"  - {self.determinism_report_json.name}")
        print("="*80)

        return full_summary


def main():
    parser = argparse.ArgumentParser(description="RL Rollout Engine Benchmark Harness")
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "artifacts" / "benchmark")
    parser.add_argument("--scaling-workers", type=int, nargs="+", default=[1, 2, 4, 8, 12, 16, 20])
    args = parser.parse_args()

    engine = RLRolloutBenchmarkEngine(args.output_dir)
    engine.run_all(scaling_counts=args.scaling_workers)

if __name__ == "__main__":
    main()
