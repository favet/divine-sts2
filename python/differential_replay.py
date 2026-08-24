"""Strict replay comparator for canonical traces exported from the shipped game.

This tool never captures or controls the visible game. It consumes an existing JSONL
trace and replays its stable action IDs in one isolated native headless worker.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from sts2_native_sim import NativeWorker


FORMAT_VERSION = 1


@dataclass(frozen=True)
class Difference:
    path: str
    expected: Any
    actual: Any


def first_difference(expected: Any, actual: Any, path: str = "$") -> Difference | None:
    if type(expected) is not type(actual):
        return Difference(path, expected, actual)
    if isinstance(expected, dict):
        if expected.keys() != actual.keys():
            return Difference(path, sorted(expected.keys()), sorted(actual.keys()))
        for key in expected:
            difference = first_difference(expected[key], actual[key], f"{path}.{key}")
            if difference:
                return difference
        return None
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return Difference(f"{path}.length", len(expected), len(actual))
        for index, (left, right) in enumerate(zip(expected, actual, strict=True)):
            difference = first_difference(left, right, f"{path}[{index}]")
            if difference:
                return difference
        return None
    return None if expected == actual else Difference(path, expected, actual)


def first_subset_difference(expected: Any, actual: Any, path: str = "$") -> Difference | None:
    """Compare an explicit projected observation while allowing unprojected object keys."""
    if type(expected) is not type(actual):
        return Difference(path, expected, actual)
    if isinstance(expected, dict):
        for key in expected:
            if key not in actual:
                return Difference(f"{path}.{key}", expected[key], "<missing>")
            difference = first_subset_difference(expected[key], actual[key], f"{path}.{key}")
            if difference:
                return difference
        return None
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return Difference(f"{path}.length", len(expected), len(actual))
        for index, (left, right) in enumerate(zip(expected, actual, strict=True)):
            difference = first_subset_difference(left, right, f"{path}[{index}]")
            if difference:
                return difference
        return None
    return None if expected == actual else Difference(path, expected, actual)


def load_trace(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not records or records[0].get("type") != "header":
        raise ValueError("First JSONL record must be a trace header.")
    header, checkpoints = records[0], records[1:]
    if header.get("format_version") != FORMAT_VERSION:
        raise ValueError(f"Unsupported trace format {header.get('format_version')!r}.")
    if not isinstance(header.get("reset"), dict) or not checkpoints:
        raise ValueError("Trace requires reset input and at least one checkpoint.")
    for sequence, checkpoint in enumerate(checkpoints):
        if checkpoint.get("type") != "checkpoint" or checkpoint.get("sequence") != sequence or not isinstance(checkpoint.get("observation"), dict):
            raise ValueError(f"Invalid checkpoint at record {sequence + 2}.")
        if sequence == 0 and checkpoint.get("action_id") is not None:
            raise ValueError("Checkpoint zero must describe reset and have null action_id.")
        if sequence > 0 and not isinstance(checkpoint.get("action_id"), str):
            raise ValueError(f"Checkpoint {sequence} requires action_id.")
    return header, checkpoints


def replay(path: Path, *, require_exact: bool = False) -> dict[str, Any]:
    header, checkpoints = load_trace(path)
    declared_comparison = header.get("comparison", "exact")
    if declared_comparison not in {"exact", "subset"}:
        raise ValueError(f"Unsupported comparison mode {declared_comparison!r}.")
    validation_comparison = "exact" if require_exact else declared_comparison
    compare = first_difference if validation_comparison == "exact" else first_subset_difference
    resolved_choices: list[dict[str, Any]] = []
    with NativeWorker() as worker:
        expected_build = header.get("game_build") or {}
        build_difference = first_difference(expected_build, {key: worker.build.get(key) for key in expected_build})
        if build_difference:
            return {"success": False, "certifying": False, "trace": str(path), "stage": "build", "difference": build_difference.__dict__}
        result = worker.reset(header["reset"])
        for sequence, checkpoint in enumerate(checkpoints):
            if sequence:
                result = worker.step(checkpoint["action_id"])
            difference = compare(checkpoint["observation"], result["observation"])
            if difference and validation_comparison == "exact" and _is_pending_choice(result):
                resolution = _resolve_choice_to_checkpoint(worker, result, checkpoint, compare)
                if not resolution["success"]:
                    return {
                        "success": False,
                        "certifying": False,
                        "trace": str(path),
                        "stage": "choice_resolution",
                        "sequence": sequence,
                        "action_id": checkpoint.get("action_id"),
                        **resolution,
                    }
                result = resolution["result"]
                resolved_choices.append({"sequence": sequence, "action_ids": resolution["action_ids"]})
                difference = compare(checkpoint["observation"], result["observation"])
            if difference:
                return {"success": False, "certifying": False, "trace": str(path), "stage": "checkpoint", "sequence": sequence, "action_id": checkpoint.get("action_id"), "difference": difference.__dict__}
            expected_hash = checkpoint.get("state_hash")
            if expected_hash is not None and expected_hash != result["state_hash"]:
                return {"success": False, "certifying": False, "trace": str(path), "stage": "hash", "sequence": sequence, "expected": expected_hash, "actual": result["state_hash"]}
        return {
            "success": True,
            "certifying": header.get("source") == "shipped_game" and declared_comparison == "exact",
            "comparison": declared_comparison,
            "validation_comparison": validation_comparison,
            "trace": str(path),
            "source": header.get("source", "unknown"),
            "checkpoints": len(checkpoints),
            "final_hash": result["state_hash"],
            "resolved_choices": resolved_choices,
        }


def _is_pending_choice(result: dict[str, Any]) -> bool:
    decision = result.get("observation", {}).get("decision", {})
    return decision.get("kind") in {"card_choice", "option_choice"} and bool(result.get("legal_actions"))


def _resolve_choice_to_checkpoint(worker: NativeWorker, pending: dict[str, Any], checkpoint: dict[str, Any], compare: Any) -> dict[str, Any]:
    """Infer an omitted exporter choice only when one bounded native path matches exactly."""
    expected_observation = checkpoint["observation"]
    expected_hash = checkpoint.get("state_hash")
    matches: list[list[str]] = []
    explored = 0
    max_nodes = 256
    max_depth = 4

    def visit(state: dict[str, Any], path: list[str]) -> None:
        nonlocal explored
        if explored >= max_nodes or len(path) >= max_depth or len(matches) > 1:
            return
        handle = state["state_handle"]
        actions = state.get("legal_actions", [])
        for action in actions:
            if explored >= max_nodes or len(matches) > 1:
                return
            explored += 1
            worker.restore(handle)
            candidate = worker.step(action["action_id"])
            observation_matches = compare(expected_observation, candidate["observation"]) is None
            hash_matches = expected_hash is None or expected_hash == candidate["state_hash"]
            candidate_path = [*path, action["action_id"]]
            if observation_matches and hash_matches:
                matches.append(candidate_path)
            elif _is_pending_choice(candidate):
                visit(candidate, candidate_path)

    visit(pending, [])
    if len(matches) != 1:
        return {
            "success": False,
            "error": "No unique bounded native choice path reproduced the exported checkpoint.",
            "matching_path_count": len(matches),
            "explored_choice_nodes": explored,
            "choice_kind": pending["observation"]["decision"]["kind"],
        }
    worker.restore(pending["state_handle"])
    result = pending
    for action_id in matches[0]:
        result = worker.step(action_id)
    return {
        "success": True,
        "result": result,
        "action_ids": matches[0],
        "explored_choice_nodes": explored,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("trace", type=Path)
    parser.add_argument(
        "--require-exact",
        action="store_true",
        help="Audit exact observation equality even when the trace declares subset; never upgrades certification.",
    )
    args = parser.parse_args()
    try:
        report = replay(args.trace, require_exact=args.require_exact)
    except Exception as error:
        report = {"success": False, "certifying": False, "trace": str(args.trace), "stage": "trace_validation", "error": str(error)}
    print(json.dumps(report, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
