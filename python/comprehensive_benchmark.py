"""
Comprehensive Native .NET 9 Simulation Benchmark Harness.
Runs all benchmark phases in a single script and produces a structured JSON report.

Phases:
  1. Latency micro-benchmark (single worker)
  2. Scaling benchmark (1/2/4/8/16/20 workers)
  3. Sustained soak (20 workers, configurable duration)
  4. Stress test (memory leak detection)
  5. Full-run rollout (configurable episodes)

Usage:
  python comprehensive_benchmark.py [--duration 100] [--episodes 300] [--workers 20] [--output-dir artifacts/benchmark]
"""

from __future__ import annotations
import argparse
import copy
import hashlib
import json
import os
import queue
import random
import statistics
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from sts2_native_sim import NativeSimError, NativeWorker, NativeWorkerPool

# ─── Shared fixtures ───────────────────────────────────────────────────────────

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
    ],
}

ACCEPTANCE_SCENARIO = {
    "game_build": {}, "seed": "BENCHMARK-BASELINE", "rng_counters": {},
    "character": "IRONCLAD", "ascension": 0, "encounter": "first",
    "current_hp": 80, "max_hp": 80, "gold": 99,
    "deck": DECK_TEMPLATES["IRONCLAD"], "initial_hand": ["strike-0"],
    "relics": [], "potions": []
}


def _percentiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    s = sorted(values)
    n = len(s)
    return {
        "min": round(s[0], 4), "p25": round(s[int(n * 0.25)], 4),
        "p50": round(s[int(n * 0.50)], 4), "p75": round(s[int(n * 0.75)], 4),
        "p90": round(s[int(n * 0.90)], 4), "p95": round(s[int(n * 0.95)], 4),
        "p99": round(s[min(int(n * 0.99), n - 1)], 4), "max": round(s[-1], 4),
        "mean": round(statistics.mean(values), 4),
        "stdev": round(statistics.stdev(values), 4) if n > 1 else 0.0,
        "count": n,
    }


def _timed(operation, samples=20) -> dict[str, float]:
    values = []
    for _ in range(samples):
        t0 = time.perf_counter()
        operation()
        values.append((time.perf_counter() - t0) * 1000)
    return _percentiles(values)


def _timed_after(setup, operation, samples=20) -> dict[str, float]:
    values = []
    for _ in range(samples):
        setup()
        t0 = time.perf_counter()
        operation()
        values.append((time.perf_counter() - t0) * 1000)
    return _percentiles(values)


# ─── Phase 1: Latency Micro-Benchmark ──────────────────────────────────────────

def run_latency_benchmark() -> dict[str, Any]:
    print("\n[Phase 1] Latency micro-benchmark (single worker)...")
    t0 = time.perf_counter()
    with NativeWorker() as worker:
        startup_ms = (time.perf_counter() - t0) * 1000
        initial = worker.reset(ACCEPTANCE_SCENARIO)
        h0 = initial["state_handle"]
        play = next(a["action_id"] for a in initial["legal_actions"] if a["kind"] == "play_card")
        after_play = worker.step(play)
        h1 = after_play["state_handle"]
        after_turn = worker.step("end_turn")
        h2 = after_turn["state_handle"]

        def search_cycle():
            branch = worker.fork()
            worker.step(play)
            worker.restore(branch)
            worker.step("end_turn")

        result = {
            "worker_startup_ms": round(startup_ms, 1),
            "resident": {
                "play_card_step_plus_obs_ms": _timed_after(
                    lambda: worker.restore(h0), lambda: worker.step(play), 30),
                "end_turn_plus_obs_ms": _timed_after(
                    lambda: worker.restore(h1), lambda: worker.step("end_turn"), 30),
            },
            "reset_ms": _timed(lambda: worker.reset(ACCEPTANCE_SCENARIO), 10),
            "restore_ms": {
                "depth_0": _timed_after(lambda: worker.restore(h1), lambda: worker.restore(h0), 10),
                "depth_1": _timed_after(lambda: worker.restore(h0), lambda: worker.restore(h1), 10),
                "depth_2": _timed_after(lambda: worker.restore(h0), lambda: worker.restore(h2), 10),
            },
            "search_fork_play_restore_end_ms": _timed_after(
                lambda: worker.restore(h0), search_cycle, 10),
            "protocol_ms": {
                "diagnostics": _timed(worker.diagnostics, 100),
                "observe": _timed(worker.observe, 100),
            },
            "memory_bytes": worker.memory_bytes,
            "deterministic_hashes": {
                "initial": initial["state_hash"],
                "after_play": after_play["state_hash"],
                "after_turn": after_turn["state_hash"],
            },
        }
    print(f"  ✓ Complete ({time.perf_counter() - t0:.1f}s)")
    return result


# ─── Phase 2: Scaling Benchmark ────────────────────────────────────────────────

def run_scaling_benchmark(max_workers: int = 20, iterations: int = 20) -> dict[str, Any]:
    print(f"\n[Phase 2] Scaling benchmark (up to {max_workers} workers)...")
    t0 = time.perf_counter()
    worker_counts = [w for w in [1, 2, 4, 8, 16, 20] if w <= max_workers]
    results = []
    for count in worker_counts:
        print(f"  {count} workers...", end=" ", flush=True)
        with NativeWorkerPool(count) as pool:
            states = pool.reset_all(ACCEPTANCE_SCENARIO)
            handles = [s["state_handle"] for s in states]
            actions = [next(a["action_id"] for a in s["legal_actions"] if a["kind"] == "play_card") for s in states]
            hashes = []
            started = time.perf_counter()
            for _ in range(iterations):
                pool.map(lambda w, h: w.restore(h), handles)
                step_results = pool.map(lambda w, a: w.step(a), actions)
                hashes.extend(r["state_hash"] for r in step_results)
            elapsed = time.perf_counter() - started
            total = count * iterations
            r = {"workers": count, "transitions": total, "seconds": round(elapsed, 3),
                 "trans_per_sec": round(total / elapsed, 1),
                 "deterministic": len(set(hashes)) == 1,
                 "memory_per_worker": [w.memory_bytes for w in pool.workers]}
            results.append(r)
            print(f"{r['trans_per_sec']} trans/s ({'✓' if r['deterministic'] else '✗'})")
    print(f"  ✓ Complete ({time.perf_counter() - t0:.1f}s)")
    return {"iterations": iterations, "results": results}


# ─── Phase 3: 20-Worker Sustained Soak ─────────────────────────────────────────

def run_soak_test(num_workers: int = 20, duration: float = 100.0) -> dict[str, Any]:
    print(f"\n[Phase 3] {num_workers}-worker {duration}s soak...")
    stop = threading.Event()
    stats: list[dict[str, Any]] = [{} for _ in range(num_workers)]
    snapshots: list[dict[str, Any]] = []

    def run_worker(worker: NativeWorker, wid: int):
        rng = random.Random(42000 + wid)
        local = {"combats": 0, "actions": 0, "turns": 0, "errors": 0, "ep_durations": []}
        t_start = time.perf_counter()
        while not stop.is_set() and (time.perf_counter() - t_start < duration):
            char = rng.choice(CHARACTERS)
            scenario = {
                "game_build": {}, "seed": f"SOAK_{wid}_{local['combats']}_{rng.randint(1000,999999)}",
                "rng_counters": {}, "character": char, "ascension": rng.choice([0,1,5,10]),
                "encounter": "first", "current_hp": 80, "max_hp": 80, "gold": 99,
                "deck": DECK_TEMPLATES[char], "initial_hand": [], "relics": [], "potions": []
            }
            ep_t = time.perf_counter()
            try:
                state = worker.reset(scenario)
                local["combats"] += 1
                for _ in range(50):
                    if stop.is_set(): break
                    legal = state.get("legal_actions", [])
                    if not legal: break
                    cards = [a["action_id"] for a in legal if a.get("kind") == "play_card"]
                    if cards:
                        action = rng.choice(cards)
                    else:
                        ends = [a["action_id"] for a in legal if a.get("action_id") == "end_turn"]
                        if ends: action = ends[0]; local["turns"] += 1
                        else: action = legal[0]["action_id"]
                    state = worker.step(action)
                    local["actions"] += 1
                    obs = state.get("observation", {})
                    creatures = obs.get("combat", {}).get("creatures", [])
                    if not [c for c in creatures if c.get("side") == "Enemy" and c.get("alive")]:
                        break
                    if not any(c for c in creatures if c.get("side") == "Player" and c.get("alive")):
                        break
                local["ep_durations"].append(time.perf_counter() - ep_t)
            except Exception as ex:
                local["errors"] += 1
                time.sleep(0.05)
        local["memory_bytes"] = worker.memory_bytes
        stats[wid] = local

    t0 = time.perf_counter()
    with NativeWorkerPool(num_workers) as pool:
        startup_s = time.perf_counter() - t0
        print(f"  Spawned in {startup_s:.1f}s")
        threads = [threading.Thread(target=run_worker, args=(w, i), daemon=True) for i, w in enumerate(pool.workers)]
        for t in threads: t.start()
        start = time.perf_counter()
        while time.perf_counter() - start < duration:
            time.sleep(10.0)
            el = time.perf_counter() - start
            acts = sum(s.get("actions", 0) for s in stats)
            cmb = sum(s.get("combats", 0) for s in stats)
            rate = acts / max(0.1, el)
            snapshots.append({"t": round(el, 1), "actions": acts, "combats": cmb, "dec_s": round(rate, 1)})
            print(f"  [{el:.0f}s] {cmb} combats, {acts} decisions ({rate:.1f} dec/s)")
        stop.set()
        for t in threads: t.join(timeout=15.0)

    elapsed = time.perf_counter() - start
    total_a = sum(s.get("actions", 0) for s in stats)
    total_c = sum(s.get("combats", 0) for s in stats)
    total_e = sum(s.get("errors", 0) for s in stats)
    all_ep = [d for s in stats for d in s.get("ep_durations", [])]
    total_mem = sum(s.get("memory_bytes", 0) for s in stats) / (1024*1024)
    result = {
        "workers": num_workers, "target_s": duration, "actual_s": round(elapsed, 2),
        "startup_s": round(startup_s, 2), "combats": total_c, "actions": total_a,
        "dec_per_sec": round(total_a / max(0.1, elapsed), 1),
        "combats_per_min": round(total_c / max(0.1, elapsed) * 60, 1),
        "errors": total_e, "memory_mb": round(total_mem, 1),
        "avg_mb_per_worker": round(total_mem / max(1, num_workers), 1),
        "episode_duration_pct_s": _percentiles(all_ep),
        "throughput_timeline": snapshots,
        "passed": total_e == 0 and total_a > 500,
    }
    print(f"  {'PASS ✓' if result['passed'] else 'FAIL ✗'}: {result['dec_per_sec']} dec/s, {result['avg_mb_per_worker']} MB/w")
    return result


# ─── Phase 4: Stress Test ──────────────────────────────────────────────────────

def run_stress_test() -> dict[str, Any]:
    print("\n[Phase 4] Stress test...")
    t0 = time.perf_counter()
    with NativeWorker() as worker:
        worker.reset(ACCEPTANCE_SCENARIO)
        mem0 = worker.memory_bytes
        for _ in range(1000): worker.observe()
        mem1 = worker.memory_bytes
        for i in range(50):
            s = copy.deepcopy(ACCEPTANCE_SCENARIO); s["seed"] = f"W-{i}"; worker.reset(s)
        mem2 = worker.memory_bytes
        for i in range(300):
            s = copy.deepcopy(ACCEPTANCE_SCENARIO); s["seed"] = f"S-{i}"; worker.reset(s)
            a = next(x["action_id"] for x in worker.legal_actions() if x["kind"] == "play_card")
            worker.step(a)
        time.sleep(1)
        diag = worker.diagnostics()
        mem3 = worker.memory_bytes
        growth = mem3 - mem2
    passed = growth < 256 * 1024 * 1024
    result = {"resets": 350, "growth_mb": round(growth/(1024*1024), 1),
              "branches": diag["branch_count"], "cap": diag["branch_capacity"],
              "mem_final_mb": round(mem3/(1024*1024), 1), "passed": passed,
              "elapsed_s": round(time.perf_counter() - t0, 1)}
    print(f"  {'PASS ✓' if passed else 'FAIL ✗'}: growth={result['growth_mb']} MB")
    return result


# ─── Phase 5: Full-Run Rollout Farm ────────────────────────────────────────────

def run_rollout_farm(num_workers: int, episodes: int, ascension: int = 1) -> dict[str, Any]:
    print(f"\n[Phase 5] {episodes}-episode rollout farm ({num_workers} workers, A{ascension})...")
    all_chars = ["IRONCLAD", "SILENT", "DEFECT", "NECROBINDER", "REGENT"]
    spec_q: queue.Queue = queue.Queue()
    for i in range(episodes):
        spec_q.put({"idx": i, "seed": f"BENCH-{i:06d}", "char": all_chars[i%len(all_chars)], "asc": ascension})

    lock = threading.Lock()
    m = {"done": 0, "valid": 0, "wins": 0, "steps": 0, "errors": 0, "caps": 0,
         "restarts": 0, "ep_dur": [], "ep_steps": [],
         "char": Counter(), "outcome": Counter(), "decision": Counter(), "act": Counter(),
         "startup": {}, "mem": {}}

    def drive(worker, spec):
        scenario = {
            "game_build": {}, "seed": spec["seed"], "rng_counters": {},
            "character": spec["char"], "ascension": spec["asc"], "encounter": "first",
            "current_hp": 1, "max_hp": 1, "gold": 0, "deck": [], "initial_hand": [],
            "relics": [], "potions": [], "use_character_starting_loadout": True,
            "capture_orbs": spec["char"] == "DEFECT",
        }
        t = time.perf_counter()
        state = worker.run_reset(scenario)
        decisions = Counter()
        max_act = 0
        for step in range(2001):
            obs = state.get("observation") or {}
            feat = state.get("scoring_features") or {}
            max_act = max(max_act, int(feat.get("act_index", 0)))
            if state.get("terminated") or obs.get("terminal"):
                v = bool(state.get("victory") or obs.get("victory"))
                return {"v": True, "w": v, "s": step, "t": time.perf_counter()-t,
                        "a": max_act, "d": dict(decisions), "o": "victory" if v else "death"}
            if step == 2000:
                return {"v": False, "w": False, "s": step, "t": time.perf_counter()-t,
                        "a": max_act, "d": dict(decisions), "o": "step_cap"}
            legal = state.get("legal_actions") or []
            if not legal:
                return {"v": False, "w": False, "s": step, "t": time.perf_counter()-t,
                        "a": max_act, "d": dict(decisions), "o": "no_actions"}
            digest = hashlib.sha256(str(state.get("state_hash", "")).encode()).digest()
            action = legal[int.from_bytes(digest[:8], "big") % len(legal)]
            dk = (obs.get("decision") or {}).get("kind", "?")
            decisions[dk] += 1
            state = worker.run_step(action["action_id"])

    def worker_loop(wid):
        worker = None
        try:
            t = time.perf_counter()
            worker = NativeWorker()
            with lock: m["startup"][wid] = time.perf_counter() - t
            while True:
                try: spec = spec_q.get_nowait()
                except queue.Empty: break
                try:
                    r = drive(worker, spec)
                    with lock:
                        m["done"] += 1; m["steps"] += r["s"]; m["valid"] += int(r["v"])
                        m["wins"] += int(r["w"]); m["caps"] += int(r["o"] == "step_cap")
                        m["ep_dur"].append(r["t"]); m["ep_steps"].append(float(r["s"]))
                        m["char"][spec["char"]] += 1; m["outcome"][r["o"]] += 1
                        m["act"][r["a"]] += 1
                        for k, v in r["d"].items(): m["decision"][k] += v
                except Exception as e:
                    with lock:
                        m["done"] += 1; m["errors"] += 1; m["char"][spec["char"]] += 1
                        m["outcome"]["error"] += 1
                    if isinstance(e, NativeSimError) and e.code == "worker_crashed":
                        try: worker.close()
                        except: pass
                        worker = NativeWorker()
                        with lock: m["restarts"] += 1
                finally: spec_q.task_done()
        finally:
            if worker:
                with lock: m["mem"][wid] = worker.memory_bytes
                try: worker.close()
                except: pass

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=num_workers) as ex:
        futs = [ex.submit(worker_loop, i) for i in range(num_workers)]
        while not all(f.done() for f in futs):
            time.sleep(5.0)
            with lock:
                d = m["done"]; el = time.perf_counter() - t0; rate = m["steps"] / max(0.1, el)
            if d > 0:
                print(f"  [{d}/{episodes}] {rate:.1f} dec/s, {d/el*3600:.0f} ep/hr")
        for f in futs: f.result()

    elapsed = time.perf_counter() - t0
    result = {
        "episodes": episodes, "workers": num_workers, "ascension": ascension,
        "elapsed_s": round(elapsed, 2),
        "completed": m["done"], "valid_terminal": m["valid"], "victories": m["wins"],
        "errors": m["errors"], "step_caps": m["caps"], "restarts": m["restarts"],
        "total_decisions": m["steps"],
        "dec_per_sec": round(m["steps"] / max(0.1, elapsed), 1),
        "ep_per_hour": round(m["done"] / elapsed * 3600, 1),
        "valid_frac": round(m["valid"] / max(1, m["done"]), 4),
        "ep_duration_pct_s": _percentiles(m["ep_dur"]),
        "ep_steps_pct": _percentiles(m["ep_steps"]),
        "by_char": dict(m["char"]), "by_outcome": dict(m["outcome"]),
        "by_decision": dict(m["decision"]),
        "max_act": {str(k): v for k, v in sorted(m["act"].items())},
        "startup_s": m["startup"], "mem_bytes": m["mem"],
    }
    print(f"  ✓ {result['dec_per_sec']} dec/s, {result['ep_per_hour']:.0f} ep/hr, "
          f"valid={result['valid_frac']}, errors={result['errors']}")
    return result


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Comprehensive Native .NET 9 STS2 Benchmark")
    p.add_argument("--workers", type=int, default=20)
    p.add_argument("--duration", type=float, default=100.0)
    p.add_argument("--episodes", type=int, default=300)
    p.add_argument("--ascension", type=int, default=1)
    p.add_argument("--output-dir", default="artifacts/comprehensive-benchmark")
    p.add_argument("--skip-latency", action="store_true")
    p.add_argument("--skip-scaling", action="store_true")
    p.add_argument("--skip-soak", action="store_true")
    p.add_argument("--skip-stress", action="store_true")
    p.add_argument("--skip-farm", action="store_true")
    p.add_argument("--label", default="baseline")
    args = p.parse_args()

    out = Path(args.output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "label": args.label, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "workers": args.workers,
        "platform": {"python": sys.version.split()[0], "os": os.name, "cpus": os.cpu_count()},
    }
    t0 = time.perf_counter()

    if not args.skip_latency: report["latency"] = run_latency_benchmark()
    if not args.skip_scaling: report["scaling"] = run_scaling_benchmark(max_workers=args.workers)
    if not args.skip_soak: report["soak"] = run_soak_test(num_workers=args.workers, duration=args.duration)
    if not args.skip_stress: report["stress"] = run_stress_test()
    if not args.skip_farm: report["farm"] = run_rollout_farm(num_workers=args.workers, episodes=args.episodes, ascension=args.ascension)

    report["total_s"] = round(time.perf_counter() - t0, 1)
    f = out / f"benchmark-{args.label}.json"
    f.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\n{'='*80}\nDONE in {report['total_s']:.0f}s → {f}\n{'='*80}")


if __name__ == "__main__":
    main()
