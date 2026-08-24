"""Locked search-level lift evaluation on fresh native encounters and seeds."""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from sts2_native_sim import NativeSearchCoordinator, NativeTorchValueScorer, NativeWorkerPool
from train_native_value_matrix import (
    ARCHETYPES,
    DECK_SIZES,
    HP_RATIOS,
    choose_action,
    profile_spec,
    stable_int,
    terminal_return,
    validate_archetypes,
)


POLICIES = ("value_search", "greedy", "heuristic")
TIER_NAMES = {"Monster": "hallway", "Elite": "elite", "Boss": "boss"}


def fresh_encounters(catalog: dict[str, Any], metadata: dict[str, Any], per_stratum: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    matrix = metadata.get("distribution_matrix") or {}
    used = {
        model_id
        for key in ("train_encounters", "validation_encounters", "promotion_test_encounters")
        for model_id in matrix.get(key, [])
    }
    strata: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for encounter in catalog["encounters"]:
        model_id = encounter["model_id"]
        tier = TIER_NAMES.get(encounter.get("room_type"))
        if model_id in used or tier is None or "TEST" in model_id or "DEBUG" in model_id:
            continue
        for act in encounter.get("act_indices", []):
            if act in (1, 2, 3):
                strata[(act, tier)].append(encounter)
    missing = [{"act": act, "tier": tier} for act in (1, 2, 3) for tier in ("hallway", "elite", "boss") if not strata[(act, tier)]]
    selected = []
    for (act, tier), values in sorted(strata.items()):
        ordered = sorted(values, key=lambda item: item["model_id"])
        take = min(per_stratum, len(ordered))
        for encounter in ordered[:take]:
            selected.append({"act": act, "tier": tier, "encounter": encounter})
    if not selected:
        raise RuntimeError("no encounter IDs remain outside all critic corpus splits")
    return selected, missing


def run_episode(
    pool: NativeWorkerPool,
    coordinator: NativeSearchCoordinator,
    scorer: NativeTorchValueScorer,
    spec: dict[str, Any],
    policy: str,
    policy_seed: str,
    max_decisions: int,
    search_depth: int,
    node_budget: int,
    beam_width: int,
) -> dict[str, Any]:
    worker = pool.workers[0]
    state = worker.reset(spec)
    rng = random.Random(stable_int(policy_seed))
    search_nodes = 0
    attempted_at_state: set[tuple[str, str]] = set()
    for decision in range(max_decisions):
        if state["terminated"]:
            features = state["scoring_features"]
            hp_ratio = features["current_hp"] / max(1, features["max_hp"])
            return {
                "completed": True,
                "victory": bool(state["victory"]),
                "return": terminal_return(state),
                "survival": hp_ratio if state["victory"] else 0.0,
                "decisions": decision,
                "search_nodes": search_nodes,
                "terminal_hash": state["state_hash"],
            }
        if policy == "value_search":
            result = coordinator.search(
                scorer,
                max_depth=search_depth,
                node_budget=node_budget,
                beam_width=beam_width,
            )
            if not result["best_path"]:
                raise RuntimeError(f"search returned no action for nonterminal root {state['state_hash']}")
            action_id = result["best_path"][0]
            search_nodes += result["nodes_evaluated"]
        else:
            action_id = choose_action(state, policy, rng)
        cycle_key = (state["state_hash"], action_id)
        if cycle_key in attempted_at_state:
            alternatives = [action["action_id"] for action in state["legal_actions"] if (state["state_hash"], action["action_id"]) not in attempted_at_state]
            action_id = next((candidate for candidate in alternatives if candidate == "end_turn"), alternatives[0] if alternatives else action_id)
            cycle_key = (state["state_hash"], action_id)
        attempted_at_state.add(cycle_key)
        state = worker.step(action_id)
    return {
        "completed": False,
        "victory": False,
        "return": 0.0,
        "survival": 0.0,
        "decisions": max_decisions,
        "search_nodes": search_nodes,
        "terminal_hash": None,
        "last_state_hash": state["state_hash"],
        "last_decision": state["observation"]["decision"]["kind"],
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(rows)
    return {
        "episodes": count,
        "completed": sum(row["completed"] for row in rows),
        "budget_exhausted": sum(not row["completed"] for row in rows),
        "victories": sum(row["victory"] for row in rows),
        "win_rate": sum(row["victory"] for row in rows) / count,
        "mean_return": sum(row["return"] for row in rows) / count,
        "mean_survival": sum(row["survival"] for row in rows) / count,
        "mean_decisions": sum(row["decisions"] for row in rows) / count,
        "mean_realized_regret": sum(row["realized_regret"] for row in rows) / count,
        "search_nodes": sum(row["search_nodes"] for row in rows),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", type=Path, default=Path("artifacts/native-value-matrix.pt"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/native-value-search-evaluation.json"))
    parser.add_argument("--encounters-per-stratum", type=int, default=1)
    parser.add_argument("--max-decisions", type=int, default=250)
    parser.add_argument("--search-depth", type=int, default=2)
    parser.add_argument("--node-budget", type=int, default=12)
    parser.add_argument("--beam-width", type=int, default=4)
    parser.add_argument("--minimum-return-lift", type=float, default=0.02)
    parser.add_argument("--seed-namespace", default="fresh-v1")
    args = parser.parse_args()
    if min(args.encounters_per_stratum, args.max_decisions, args.search_depth, args.node_budget, args.beam_width) < 1:
        raise ValueError("all evaluation counts and search budgets must be positive")

    with NativeWorkerPool(4) as pool:
        catalog = pool.workers[0].catalog()
        card_types = validate_archetypes(catalog["cards"])
        scorer = NativeTorchValueScorer.load(args.checkpoint, pool.workers[0].build, allow_unpromoted=True)
        if not (scorer.metadata.get("promotion") or {}).get("promoted"):
            raise ValueError("search-lift evaluation requires a checkpoint that first passed its sibling-state gate")
        selected, coverage_gaps = fresh_encounters(catalog, scorer.metadata, args.encounters_per_stratum)
        coordinator = NativeSearchCoordinator(pool)
        rows = []
        archetypes = list(ARCHETYPES)
        for ordinal, item in enumerate(selected):
            archetype = archetypes[ordinal % len(archetypes)]
            deck_size = DECK_SIZES[ordinal % len(DECK_SIZES)]
            hp_ratio = HP_RATIOS[ordinal % len(HP_RATIOS)]
            scenario_id = f"{args.seed_namespace}:act{item['act']}:{item['tier']}:{item['encounter']['model_id']}:{archetype}:{deck_size}:{hp_ratio}"
            spec = profile_spec(item["encounter"]["model_id"], archetype, deck_size, hp_ratio, f"NATIVE-SEARCH-LIFT-{scenario_id}", card_types)
            scenario_rows = []
            for policy in POLICIES:
                result = run_episode(pool, coordinator, scorer, spec, policy, f"{scenario_id}:{policy}", args.max_decisions, args.search_depth, args.node_budget, args.beam_width)
                result.update({
                    "scenario_id": scenario_id,
                    "policy": policy,
                    "act": item["act"],
                    "tier": item["tier"],
                    "encounter": item["encounter"]["model_id"],
                    "archetype": archetype,
                    "deck_size": deck_size,
                    "hp_ratio": hp_ratio,
                })
                scenario_rows.append(result)
            best_return = max(row["return"] for row in scenario_rows)
            for row in scenario_rows:
                row["realized_regret"] = best_return - row["return"]
            rows.extend(scenario_rows)

    summaries = {policy: aggregate([row for row in rows if row["policy"] == policy]) for policy in POLICIES}
    critic = summaries["value_search"]
    best_baseline_return = max(summaries[policy]["mean_return"] for policy in ("greedy", "heuristic"))
    best_baseline_win_rate = max(summaries[policy]["win_rate"] for policy in ("greedy", "heuristic"))
    gate = {
        "minimum_return_lift": args.minimum_return_lift,
        "observed_return_lift": critic["mean_return"] - best_baseline_return,
        "critic_win_rate": critic["win_rate"],
        "best_baseline_win_rate": best_baseline_win_rate,
        "all_episodes_completed": all(row["completed"] for row in rows),
    }
    gate["passed"] = gate["all_episodes_completed"] and gate["observed_return_lift"] >= args.minimum_return_lift and critic["win_rate"] >= best_baseline_win_rate
    report = {
        "success": True,
        "search_lift_promoted": gate["passed"],
        "checkpoint": str(args.checkpoint.resolve()),
        "freshness": f"encounter IDs excluded from critic training, validation, and promotion; evaluation seed namespace is {args.seed_namespace}",
        "fresh_encounter_coverage_gaps": coverage_gaps,
        "mechanics_source": "shipped_native",
        "nonterminal_budget_policy": "250-decision exhaustion receives return/survival zero and is reported; no terminal label is invented",
        "budgets": {"episode_native_decisions": args.max_decisions, "search_depth": args.search_depth, "search_nodes_per_value_decision": args.node_budget, "beam_width": args.beam_width},
        "gate": gate,
        "summaries": summaries,
        "scenarios": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2), encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
