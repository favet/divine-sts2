"""Persistent, multi-worker shipped-native episode farm.

Workers are started once, pull episode specs from a shared queue, and reset in
place after every terminal state. Full-app workers are deliberately absent.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import itertools
import json
import queue
import threading
import time
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sts2_native_sim import NativeSimError, NativeWorker


CHARACTERS = ("IRONCLAD", "SILENT", "DEFECT", "NECROBINDER", "REGENT")


@dataclass(frozen=True)
class EpisodeSpec:
    episode_index: int
    episode_id: str
    seed: str
    character: str
    ascension: int


@dataclass(frozen=True)
class PolicyDecision:
    action_id: str
    source: str


class DeterministicLegalPolicy:
    """Dependency-free throughput baseline; it makes no gameplay-strength claim."""

    def select(self, state: dict[str, Any]) -> PolicyDecision:
        actions = state.get("legal_actions") or []
        if not actions:
            raise RuntimeError("nonterminal state has no legal actions")
        digest = hashlib.sha256(str(state.get("state_hash", "")).encode("utf-8")).digest()
        selected = actions[int.from_bytes(digest[:8], "big") % len(actions)]
        return PolicyDecision(selected["action_id"], "deterministic_uniform_legal")


def starting_scenario(spec: EpisodeSpec) -> dict[str, Any]:
    return {
        "game_build": {},
        "seed": spec.seed,
        "rng_counters": {},
        "character": spec.character,
        "ascension": spec.ascension,
        "encounter": "first",
        # Ignored when native starting loadout is requested, but retained as
        # positive schema sentinels so older workers fail loudly.
        "current_hp": 1,
        "max_hp": 1,
        "gold": 0,
        "deck": [],
        "initial_hand": [],
        "relics": [],
        "potions": [],
        "use_character_starting_loadout": True,
        "capture_orbs": spec.character == "DEFECT",
    }


class FarmMetrics:
    def __init__(self, requested: int):
        self.lock = threading.Lock()
        self.requested = requested
        self.started = time.perf_counter()
        self.completed = 0
        self.valid_terminal = 0
        self.victories = 0
        self.steps = 0
        self.errors = 0
        self.step_caps = 0
        self.worker_restarts = 0
        self.worker_shutdown_errors = 0
        self.by_character: Counter[str] = Counter()
        self.by_outcome: Counter[str] = Counter()
        self.by_policy_source: Counter[str] = Counter()
        self.by_decision: Counter[str] = Counter()
        self.max_act_reached: Counter[int] = Counter()
        self.worker_startup_seconds: dict[int, float] = {}
        self.worker_memory_bytes: dict[int, int] = {}

    def snapshot(self) -> dict[str, Any]:
        elapsed = max(1e-9, time.perf_counter() - self.started)
        return {
            "requested_episodes": self.requested,
            "completed_episodes": self.completed,
            "valid_terminal_episodes": self.valid_terminal,
            "victories": self.victories,
            "errors": self.errors,
            "step_caps": self.step_caps,
            "worker_restarts": self.worker_restarts,
            "worker_shutdown_errors": self.worker_shutdown_errors,
            "native_decisions": self.steps,
            "elapsed_seconds": elapsed,
            "episodes_per_hour": self.completed / elapsed * 3600.0,
            "valid_episodes_per_hour": self.valid_terminal / elapsed * 3600.0,
            "decisions_per_second": self.steps / elapsed,
            "valid_fraction": self.valid_terminal / max(1, self.completed),
            "by_character": dict(self.by_character),
            "by_outcome": dict(self.by_outcome),
            "by_policy_source": dict(self.by_policy_source),
            "by_decision": dict(self.by_decision),
            "max_act_reached": {str(key): value for key, value in sorted(self.max_act_reached.items())},
            "worker_startup_seconds": self.worker_startup_seconds,
            "worker_memory_bytes": self.worker_memory_bytes,
        }


def compact_state_record(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "state_hash": state.get("state_hash"),
        "observation": state.get("observation"),
        "scoring_features": state.get("scoring_features"),
        "legal_actions": state.get("legal_actions"),
        "transition": state.get("transition"),
    }


def drive_episode(
    worker: NativeWorker,
    policy: Any,
    spec: EpisodeSpec,
    handle,
    step_limit: int,
    record_states: bool,
) -> dict[str, Any]:
    started = time.perf_counter()
    state = worker.run_reset(starting_scenario(spec))
    decisions: Counter[str] = Counter()
    policy_sources: Counter[str] = Counter()
    max_act_index = int((state.get("scoring_features") or {}).get("act_index", 0))
    max_floor = 0
    for step in range(step_limit + 1):
        observation = state.get("observation") or {}
        decision_kind = (observation.get("decision") or {}).get("kind", "unknown")
        features = state.get("scoring_features") or {}
        max_act_index = max(max_act_index, int(features.get("act_index", 0)))
        max_floor = max(max_floor, max_act_index * 16 + int(features.get("act_floor", 0)))
        if state.get("terminated") or observation.get("terminal"):
            victory = bool(state.get("victory") or observation.get("victory"))
            outcome = "victory" if victory else "death"
            summary = {
                "record_type": "episode_summary",
                **asdict(spec),
                "outcome": outcome,
                "valid_terminal": True,
                "victory": victory,
                "steps": step,
                "elapsed_seconds": time.perf_counter() - started,
                "max_act_index": max_act_index,
                "max_floor": max_floor,
                "final_state_hash": state.get("state_hash"),
                "decisions": dict(decisions),
                "policy_sources": dict(policy_sources),
            }
            handle.write(json.dumps(summary, separators=(",", ":")) + "\n")
            return summary
        if step == step_limit:
            summary = {
                "record_type": "episode_summary",
                **asdict(spec),
                "outcome": "step_cap",
                "valid_terminal": False,
                "victory": False,
                "steps": step,
                "elapsed_seconds": time.perf_counter() - started,
                "max_act_index": max_act_index,
                "max_floor": max_floor,
                "final_state_hash": state.get("state_hash"),
                "decisions": dict(decisions),
                "policy_sources": dict(policy_sources),
            }
            handle.write(json.dumps(summary, separators=(",", ":")) + "\n")
            return summary

        selected = policy.select(state)
        decisions[decision_kind] += 1
        policy_sources[selected.source] += 1
        if record_states:
            record = {
                "record_type": "transition",
                **asdict(spec),
                "step": step,
                "decision_kind": decision_kind,
                "action": selected.action_id,
                "policy_source": selected.source,
                **compact_state_record(state),
            }
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        state = worker.run_step(selected.action_id)
    raise AssertionError("unreachable episode loop")


def run_farm(args: argparse.Namespace) -> dict[str, Any]:
    characters = tuple(value.strip().upper() for value in args.characters.split(",") if value.strip())
    unknown = sorted(set(characters) - set(CHARACTERS))
    if unknown:
        raise ValueError(f"unknown characters: {unknown}")
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    episode_indices = (
        [int(value.strip()) for value in args.indices.split(",") if value.strip()]
        if args.indices else list(range(args.start_index, args.start_index + args.episodes))
    )
    if not episode_indices or len(set(episode_indices)) != len(episode_indices) or min(episode_indices) < 0:
        raise ValueError("episode indices must be unique nonnegative integers")
    args.episodes = len(episode_indices)
    specs: queue.Queue[EpisodeSpec] = queue.Queue()
    for index in episode_indices:
        character = characters[index % len(characters)]
        specs.put(EpisodeSpec(
            episode_index=index,
            episode_id=f"{args.seed_prefix}-{index:08d}",
            seed=f"{args.seed_prefix}-{index:08d}",
            character=character,
            ascension=args.ascension,
        ))

    metrics = FarmMetrics(args.episodes if not args.duration else 0)
    duration_s = getattr(args, "duration", None)
    stop_time: float | None = None
    episode_counter = itertools.count(args.start_index)

    if args.policy == "learned":
        from native_rollout_policy import NativeLearnedPolicy

        policy = NativeLearnedPolicy(
            exploration=args.exploration,
            combat_checkpoint=args.combat_checkpoint,
            native_macro_corpus=args.native_macro_corpus,
            card_database=args.card_database,
        )
    else:
        policy = DeterministicLegalPolicy()

    def worker_loop(worker_id: int) -> None:
        nonlocal stop_time
        shard = output_dir / f"worker-{worker_id:02d}.jsonl.gz"
        with gzip.open(shard, "at", encoding="utf-8", compresslevel=args.compression) as handle:
            worker: NativeWorker | None = None
            try:
                startup = time.perf_counter()
                worker = NativeWorker()
                with metrics.lock:
                    metrics.worker_startup_seconds[worker_id] = time.perf_counter() - startup
                    if stop_time is None and duration_s is not None:
                        stop_time = time.perf_counter() + duration_s
                while True:
                    if stop_time is not None and time.perf_counter() >= stop_time:
                        break
                    if stop_time is not None:
                        with metrics.lock:
                            idx = next(episode_counter)
                            char = characters[idx % len(characters)]
                            spec = EpisodeSpec(
                                episode_index=idx,
                                episode_id=f"{args.seed_prefix}-{idx:08d}",
                                seed=f"{args.seed_prefix}-{idx:08d}",
                                character=char,
                                ascension=args.ascension,
                            )
                    else:
                        try:
                            spec = specs.get_nowait()
                        except queue.Empty:
                            break
                    try:
                        summary = drive_episode(
                            worker, policy, spec, handle, args.step_limit, not args.summary_only
                        )
                        with metrics.lock:
                            metrics.completed += 1
                            metrics.steps += int(summary["steps"])
                            metrics.valid_terminal += int(summary["valid_terminal"])
                            metrics.victories += int(summary["victory"])
                            metrics.step_caps += int(summary["outcome"] == "step_cap")
                            metrics.by_character[spec.character] += 1
                            metrics.by_outcome[summary["outcome"]] += 1
                            metrics.max_act_reached[int(summary["max_act_index"])] += 1
                            metrics.by_policy_source.update(summary["policy_sources"])
                            metrics.by_decision.update(summary["decisions"])
                            if int(summary["max_act_index"]) >= 1:
                                print(json.dumps({
                                    "candidate": {
                                        "episode_index": spec.episode_index,
                                        "seed": spec.seed,
                                        "character": spec.character,
                                        "max_act_index": summary["max_act_index"],
                                        "max_floor": summary["max_floor"],
                                        "outcome": summary["outcome"],
                                        "steps": summary["steps"],
                                    }
                                }), flush=True)
                            if args.progress_every and metrics.completed % args.progress_every == 0:
                                snap = metrics.snapshot()
                                print(json.dumps({
                                    "progress": f"{metrics.completed}/{args.episodes}",
                                    "episodes_per_hour": round(snap["episodes_per_hour"], 1),
                                    "valid_fraction": round(snap["valid_fraction"], 4),
                                    "max_act_reached": snap["max_act_reached"],
                                }), flush=True)
                    except Exception as error:
                        failure = {
                            "record_type": "episode_summary",
                            **asdict(spec),
                            "outcome": "error",
                            "valid_terminal": False,
                            "victory": False,
                            "steps": 0,
                            "error": str(error),
                            "error_details": error.details if isinstance(error, NativeSimError) else None,
                            "traceback": traceback.format_exc(),
                        }
                        handle.write(json.dumps(failure, separators=(",", ":")) + "\n")
                        handle.flush()
                        with metrics.lock:
                            metrics.completed += 1
                            metrics.errors += 1
                            metrics.by_character[spec.character] += 1
                            metrics.by_outcome["error"] += 1
                        if isinstance(error, NativeSimError) and error.code == "worker_crashed":
                            try:
                                worker.close()
                            except Exception:
                                pass
                            worker = NativeWorker()
                            with metrics.lock:
                                metrics.worker_restarts += 1
                    finally:
                        specs.task_done()
            finally:
                if worker is not None:
                    with metrics.lock:
                        metrics.worker_memory_bytes[worker_id] = worker.memory_bytes
                    try:
                        worker.close()
                    except NativeSimError as error:
                        with metrics.lock:
                            metrics.worker_shutdown_errors += 1
                        print(json.dumps({
                            "worker": worker_id,
                            "shutdown_error": str(error),
                            "details": error.details,
                        }), flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(worker_loop, worker_id) for worker_id in range(args.workers)]
        for future in futures:
            future.result()

    summary = metrics.snapshot()
    summary.update({
        "workers": args.workers,
        "ascension": args.ascension,
        "characters": list(characters),
        "exploration": args.exploration,
        "step_limit": args.step_limit,
        "summary_only": args.summary_only,
        "output_dir": str(output_dir),
        "mechanics_source": "shipped_sts2_dll",
        "policy": args.policy,
        "combat_checkpoint": str(Path(args.combat_checkpoint).resolve()) if args.combat_checkpoint else None,
        "native_macro_corpus": str(Path(args.native_macro_corpus).resolve()) if args.native_macro_corpus else None,
    })
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--episodes", type=int, default=1000)
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--indices", help="comma-separated exact episode indices; overrides episodes/start-index")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--ascension", type=int, default=1, choices=range(0, 11))
    parser.add_argument("--characters", default=",".join(CHARACTERS))
    parser.add_argument("--seed-prefix", default="NATIVE-TRAIN-A1")
    parser.add_argument("--output-dir", default="artifacts/native_rollouts/latest")
    parser.add_argument("--step-limit", type=int, default=2000)
    parser.add_argument("--exploration", type=float, default=0.05)
    parser.add_argument("--policy", choices=("random", "learned"), default="random", help="random is a deterministic legal-action throughput baseline")
    parser.add_argument("--combat-checkpoint", help="candidate combat checkpoint; required with --policy learned")
    parser.add_argument("--native-macro-corpus", help="optional outcome-weighted native macro training corpus")
    parser.add_argument("--card-database", help="optional compiled card metadata used to fill missing numeric features")
    parser.add_argument("--compression", type=int, default=3, choices=range(0, 10))
    parser.add_argument("--progress-every", type=int, default=100)
    parser.add_argument("--summary-only", action="store_true", help="benchmark without transition records")
    parser.add_argument("--duration", type=float, default=None, help="duration in seconds to run continuously; overrides fixed episode count")
    args = parser.parse_args()
    if not args.duration and (args.episodes < 1 or args.workers < 1 or args.step_limit < 1):
        parser.error("episodes, workers, and step-limit must be positive")
    if args.policy == "learned" and not args.combat_checkpoint:
        parser.error("--policy learned requires --combat-checkpoint")
    return args


if __name__ == "__main__":
    run_farm(parse_args())
