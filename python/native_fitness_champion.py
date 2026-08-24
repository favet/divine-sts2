"""Find an A1 Act-2 clear using shipped-native rollout fitness.

No card table, route gate, HP threshold, or approximate transition is used.
At each combat root, stochastic complete-combat continuations are evaluated by
actual native outcome.  The accepted continuation remains live on the primary
worker, so selected actions are never substituted by an approximate simulator.
"""
from __future__ import annotations

import argparse
import gzip
import json
import random
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from native_rollout_farm import CHARACTERS, EpisodeSpec, starting_scenario
from native_rollout_policy import NativeLearnedPolicy
from sts2_native_sim import NativeWorker, NativeWorkerPool


def in_combat(state: dict[str, Any]) -> bool:
    return not state.get("terminated") and (state.get("scoring_features") or {}).get("combat") is not None


def combat_fitness(state: dict[str, Any], steps: int) -> float:
    features = state.get("scoring_features") or {}
    hp_ratio = float(features.get("current_hp", 0)) / max(1.0, float(features.get("max_hp", 1)))
    if not in_combat(state) and float(features.get("current_hp", 0)) > 0:
        return 1_000_000.0 + hp_ratio * 10_000.0 - steps
    combat = features.get("combat") or {}
    enemies = [c for c in combat.get("creatures") or [] if str(c.get("side")) == "Enemy"]
    remaining = sum(max(0.0, float(c.get("hp", 0))) for c in enemies)
    maximum = max(1.0, sum(max(0.0, float(c.get("max_hp", 0))) for c in enemies))
    return -1_000_000.0 + (1.0 - remaining / maximum) * 1_000.0 + hp_ratio * 100.0 - steps


def restore_on(worker: NativeWorker, branch: dict[str, Any]) -> dict[str, Any]:
    request = branch.get("reset_request", {"method": "reset", "params": branch["reset"]})
    result = worker.request(request["method"], request["params"])
    state = request["params"].get("state", request["params"])
    worker._record_reset(request["method"], request["params"], state, result)
    for action_id in branch["history"]:
        result = worker.step(action_id)
    if result["state_hash"] != branch["expected_hash"]:
        raise RuntimeError("portable native root diverged")
    return result


def rollout_combat(worker: NativeWorker, task: dict[str, Any], policy: NativeLearnedPolicy) -> dict[str, Any]:
    state = restore_on(worker, task["branch"])
    rng = random.Random(task["seed"])
    actions: list[str] = []
    for step in range(task["step_limit"]):
        if not in_combat(state):
            break
        legal = state.get("legal_actions") or []
        if not legal:
            break
        if rng.random() < task["epsilon"]:
            action_id = rng.choice(legal)["action_id"]
        else:
            action_id = policy.select(state).action_id
        actions.append(action_id)
        state = worker.step(action_id)
    return {
        "rollout": task["rollout"], "epsilon": task["epsilon"],
        "actions": actions, "fitness": combat_fitness(state, len(actions)),
        "won": not in_combat(state) and float((state.get("scoring_features") or {}).get("current_hp", 0)) > 0,
        "final_hash": state.get("state_hash"),
        "hp": (state.get("scoring_features") or {}).get("current_hp"),
        "max_hp": (state.get("scoring_features") or {}).get("max_hp"),
    }


def plan_combat(pool: NativeWorkerPool, policy: NativeLearnedPolicy, branch: dict[str, Any],
                seed: str, rollouts: int, max_rollouts: int, step_limit: int) -> tuple[dict[str, Any], int]:
    completed: list[dict[str, Any]] = []
    attempted = 0
    epsilon_schedule = (0.05, 0.12, 0.22, 0.35, 0.50, 0.70)
    while attempted < max_rollouts:
        wave = min(rollouts, max_rollouts - attempted)
        for offset in range(0, wave, len(pool.workers)):
            batch_size = min(len(pool.workers), wave - offset)
            tasks = []
            for local in range(batch_size):
                ordinal = attempted + offset + local
                tasks.append({
                    "branch": branch, "rollout": ordinal,
                    "seed": f"{seed}:combat-rollout:{ordinal}",
                    "epsilon": epsilon_schedule[ordinal % len(epsilon_schedule)],
                    "step_limit": step_limit,
                })
            completed.extend(pool.map(lambda worker, task: rollout_combat(worker, task, policy), tasks))
        attempted += wave
        winners = [row for row in completed if row["won"]]
        if winners:
            return max(winners, key=lambda row: row["fitness"]), attempted
    return max(completed, key=lambda row: row["fitness"]), attempted


def plan_combat_local(worker: NativeWorker, policy: NativeLearnedPolicy, seed: str,
                      rollouts: int, max_rollouts: int, step_limit: int,
                      winner_target: int) -> tuple[dict[str, Any], int, str]:
    root_handle = worker.fork()
    root_hash = worker.observe()["state_hash"]
    completed = []
    winner_handles: dict[int, str] = {}
    epsilon_schedule = (0.05, 0.12, 0.22, 0.35, 0.50, 0.70)
    for ordinal in range(max_rollouts):
        restored = worker.observe() if ordinal == 0 else worker.restore(root_handle)
        if restored["state_hash"] != root_hash:
            raise RuntimeError("same-worker native combat root diverged")
        rng = random.Random(f"{seed}:combat-rollout:{ordinal}")
        state = restored
        actions = []
        epsilon = epsilon_schedule[ordinal % len(epsilon_schedule)]
        for _ in range(step_limit):
            if not in_combat(state):
                break
            legal = state.get("legal_actions") or []
            if not legal:
                break
            action_id = rng.choice(legal)["action_id"] if rng.random() < epsilon else policy.select(state).action_id
            actions.append(action_id)
            state = worker.step(action_id)
        completed.append({
            "rollout": ordinal, "epsilon": epsilon, "actions": actions,
            "fitness": combat_fitness(state, len(actions)),
            "won": not in_combat(state) and float((state.get("scoring_features") or {}).get("current_hp", 0)) > 0,
            "final_hash": state.get("state_hash"), "hp": (state.get("scoring_features") or {}).get("current_hp"),
            "max_hp": (state.get("scoring_features") or {}).get("max_hp"),
        })
        if completed[-1]["won"]:
            winner_handles[ordinal] = worker.fork()
        winners = [row for row in completed if row["won"]]
        if len(winners) >= max(1, winner_target):
            break

    winners = [row for row in completed if row["won"]]
    best = max(winners or completed, key=lambda row: row["fitness"])
    if best["won"] and worker.observe()["state_hash"] != best["final_hash"]:
        restored = worker.restore(winner_handles[best["rollout"]])
        if restored["state_hash"] != best["final_hash"]:
            raise RuntimeError("selected native winner diverged during restore")
    return best, len(completed), root_handle


def plan_combat_beam(worker: NativeWorker, policy: NativeLearnedPolicy,
                     beam_width: int, step_limit: int) -> tuple[dict[str, Any], int, str]:
    """Search exact native transitions while pruning by continuous combat fitness."""
    root_handle = worker.fork()
    root = worker.observe()
    beam = [{"handle": root_handle, "state": root, "actions": [], "fitness": combat_fitness(root, 0)}]
    expanded = 0
    winners: list[dict[str, Any]] = []
    for depth in range(step_limit):
        children: dict[str, dict[str, Any]] = {}
        for node in beam:
            restored = worker.restore(node["handle"])
            if restored.get("state_hash") != node["state"].get("state_hash"):
                raise RuntimeError("beam-search native handle diverged")
            for action in restored.get("legal_actions") or []:
                worker.restore(node["handle"])
                state = worker.step(action["action_id"])
                expanded += 1
                actions = node["actions"] + [action["action_id"]]
                row = {
                    "handle": worker.fork(), "state": state, "actions": actions,
                    "fitness": combat_fitness(state, len(actions)),
                    "won": not in_combat(state) and float((state.get("scoring_features") or {}).get("current_hp", 0)) > 0,
                    "final_hash": state.get("state_hash"),
                    "hp": (state.get("scoring_features") or {}).get("current_hp"),
                    "max_hp": (state.get("scoring_features") or {}).get("max_hp"),
                }
                if row["won"]:
                    winners.append(row)
                elif in_combat(state):
                    prior = children.get(str(state.get("state_hash")))
                    if prior is None or row["fitness"] > prior["fitness"]:
                        children[str(state.get("state_hash"))] = row
        if winners:
            best = max(winners, key=lambda row: row["fitness"])
            restored = worker.restore(best["handle"])
            if restored.get("state_hash") != best["final_hash"]:
                raise RuntimeError("beam winner diverged during restore")
            return best, expanded, root_handle
        if not children:
            break
        beam = sorted(children.values(), key=lambda row: row["fitness"], reverse=True)[:beam_width]
    best = max(beam, key=lambda row: row["fitness"])
    return {**best, "won": False, "hp": (best["state"].get("scoring_features") or {}).get("current_hp")}, expanded, root_handle


def run_seed(pool: NativeWorkerPool, policy: NativeLearnedPolicy, seed: str, character: str,
             rollouts: int, max_rollouts: int, combat_step_limit: int, run_step_limit: int,
             winner_target: int, progress=None,
             prefix: list[dict[str, Any]] | None = None,
             first_winner_target: int | None = None,
             winner_targets: list[int] | None = None, planner: str = "shooting",
             beam_width: int = 12) -> dict[str, Any]:
    primary = pool.workers[0]
    spec = EpisodeSpec(0, seed, seed, character, 1)
    state = primary.run_reset(starting_scenario(spec))
    trace: list[str] = []
    combats = []
    started = time.perf_counter()
    for row in prefix or []:
        if state.get("state_hash") != row.get("state_hash"):
            raise RuntimeError(
                f"recorded native prefix diverged before step {row.get('step')}: "
                f"expected {row.get('state_hash')}, obtained {state.get('state_hash')}"
            )
        trace.append(row["action"])
        state = primary.step(row["action"])
    prefix_steps = len(prefix or [])
    for local_step in range(run_step_limit):
        step = prefix_steps + local_step
        features = state.get("scoring_features") or {}
        act_index = int(features.get("act_index", 0))
        if act_index >= 2:
            return {
                "success": True, "seed": seed, "character": character,
                "outcome": "act_2_cleared", "steps": step, "elapsed_seconds": time.perf_counter() - started,
                "final_hash": state.get("state_hash"), "trace": trace, "combats": combats,
                "final_features": features,
            }
        if state.get("terminated") or (state.get("observation") or {}).get("terminal"):
            return {
                "success": False, "seed": seed, "character": character,
                "outcome": "victory" if state.get("victory") else "death", "steps": step,
                "elapsed_seconds": time.perf_counter() - started, "final_hash": state.get("state_hash"),
                "trace": trace, "combats": combats, "final_features": features,
            }
        if in_combat(state) and (state.get("observation") or {}).get("decision", {}).get("kind") == "combat_action":
            before = {"act": act_index, "floor": int(features.get("act_floor", 0)), "hp": features.get("current_hp")}
            target = (
                winner_targets[len(combats)]
                if winner_targets is not None and len(combats) < len(winner_targets)
                else first_winner_target
                if not combats and first_winner_target is not None
                else winner_target
            )
            if planner == "beam":
                best, attempted, root_handle = plan_combat_beam(primary, policy, beam_width, combat_step_limit)
            else:
                best, attempted, root_handle = plan_combat_local(primary, policy, f"{seed}:{len(combats)}", rollouts, max_rollouts, combat_step_limit, target)
            if not best["won"]:
                return {
                    "success": False, "seed": seed, "character": character, "outcome": "no_winning_combat_rollout",
                    "steps": step, "elapsed_seconds": time.perf_counter() - started, "trace": trace,
                    "combats": combats + [{**before, "rollouts": attempted, "best": best}], "final_features": features,
                }
            state = primary.observe()
            trace.extend(best["actions"])
            if state.get("state_hash") != best["final_hash"]:
                raise RuntimeError("selected live combat continuation lost its native final state")
            combats.append({**before, "rollouts": attempted, "actions": len(best["actions"]), "ending_hp": best["hp"], "fitness": best["fitness"]})
            print(json.dumps({"seed": seed, "combat": len(combats), **combats[-1]}), flush=True)
            if progress is not None:
                progress({
                    "success": False, "in_progress": True, "seed": seed,
                    "character": character, "steps": step, "trace": trace,
                    "combats": combats, "current_features": state.get("scoring_features") or {},
                    "current_hash": state.get("state_hash"),
                })
            continue
        selected = policy.select(state)
        trace.append(selected.action_id)
        state = primary.step(selected.action_id)
    return {"success": False, "seed": seed, "character": character, "outcome": "run_step_cap", "trace": trace, "combats": combats}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds", type=int, default=20)
    parser.add_argument("--seed", action="append", dest="explicit_seeds",
                        help="exact native seed; may be repeated")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--rollouts", type=int, default=24)
    parser.add_argument("--max-rollouts", type=int, default=144)
    parser.add_argument("--combat-step-limit", type=int, default=300)
    parser.add_argument("--winner-target", type=int, default=6)
    parser.add_argument("--planner", choices=("shooting", "beam"), default="shooting")
    parser.add_argument("--beam-width", type=int, default=12)
    parser.add_argument("--first-winner-target", type=int,
                        help="winner population for the first searched combat only")
    parser.add_argument("--winner-targets",
                        help="comma-separated winner populations by searched-combat ordinal")
    parser.add_argument("--run-step-limit", type=int, default=2500)
    parser.add_argument("--character", default="REGENT", choices=CHARACTERS)
    parser.add_argument("--combat-checkpoint", type=Path,
                        help="combat proposal policy used inside shipped-native outcome search")
    parser.add_argument("--native-macro-corpus", type=Path,
                        help="optional shipped-native outcome macro corpus")
    parser.add_argument("--trajectory", type=Path,
                        help="recorded native JSONL(.gz) trajectory to replay before fitness search")
    parser.add_argument("--resume-before-step", type=int,
                        help="replay trajectory actions with step lower than this value")
    parser.add_argument("--output", type=Path, default=Path("artifacts/fitness/a1-act2-champion.json"))
    args = parser.parse_args()
    policy = NativeLearnedPolicy(
        exploration=0.0,
        combat_checkpoint=args.combat_checkpoint,
        native_macro_corpus=args.native_macro_corpus,
    )
    reports = []
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def checkpoint(report: dict[str, Any]) -> None:
        args.output.write_text(json.dumps({"reports": reports, "active": report}, indent=2), encoding="utf-8")

    seeds = args.explicit_seeds or [
        f"NATIVE-FITNESS-A1-{index:08d}"
        for index in range(args.start, args.start + args.seeds)
    ]
    prefix = None
    winner_targets = [int(value) for value in args.winner_targets.split(",")] if args.winner_targets else None
    if args.trajectory is not None:
        if len(seeds) != 1:
            raise ValueError("trajectory replay requires exactly one explicit seed")
        replay_seed = seeds[0]
        opener = gzip.open if args.trajectory.suffix == ".gz" else open
        with opener(args.trajectory, "rt", encoding="utf-8") as handle:
            prefix = [
                row for row in map(json.loads, handle)
                if row.get("record_type") == "transition"
                and row.get("seed") == replay_seed
                and (args.resume_before_step is None or int(row.get("step", -1)) < args.resume_before_step)
            ]
        if not prefix:
            raise ValueError(f"trajectory has no transitions for seed {replay_seed}")
        prefix.sort(key=lambda row: int(row["step"]))
    with NativeWorkerPool(args.workers) as pool:
        for seed in seeds:
            report = run_seed(pool, policy, seed, args.character, args.rollouts, args.max_rollouts, args.combat_step_limit, args.run_step_limit, args.winner_target, checkpoint, prefix, args.first_winner_target, winner_targets, args.planner, args.beam_width)
            reports.append(report)
            args.output.write_text(json.dumps({"reports": reports}, indent=2), encoding="utf-8")
            print(json.dumps({k: report.get(k) for k in ("success", "seed", "character", "outcome", "steps", "elapsed_seconds")}), flush=True)
            if report["success"]:
                print(json.dumps(report, indent=2))
                return
    raise SystemExit("no Act 2 clear found within seed budget")


if __name__ == "__main__":
    main()
