"""Policy-facing native branch expansion over isolated persistent workers."""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any

from .client import NativeWorkerPool


def _branch_identity(branch: dict[str, Any]) -> tuple[str, tuple[str, ...], str]:
    reset_request = branch.get("reset_request") or {}
    history = tuple(branch.get("history") or ())
    expected_hash = str(branch.get("expected_hash") or "")
    return (json.dumps(reset_request, sort_keys=True, separators=(",", ":")), history, expected_hash)


class NativeSearchCoordinator:
    """Expands one portable native state without approximating game mechanics."""

    def __init__(self, pool: NativeWorkerPool):
        self.pool = pool

    def expand(self, source_worker_index: int = 0, action_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
        source = self.pool._replace_if_dead(source_worker_index)
        branch = source.export_branch()
        try:
            return self._expand_branch(branch, action_ids)
        finally:
            self.pool.restore_portable(source_worker_index, branch)

    def _expand_branch(self, branch: dict[str, Any], action_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
        root = self.pool.restore_portable(0, branch)
        legal = root["legal_actions"]
        requested = list(action_ids) if action_ids is not None else [action["action_id"] for action in legal]
        legal_by_id = {action["action_id"]: action for action in legal}
        unknown = [action_id for action_id in requested if action_id not in legal_by_id]
        if unknown:
            raise ValueError(f"actions are not legal at root {root['state_hash']}: {unknown}")

        expansions: list[dict[str, Any]] = []
        width = len(self.pool.workers)
        for offset in range(0, len(requested), width):
            batch = requested[offset:offset + width]

            def execute(_: Any, item: tuple[int, str]) -> dict[str, Any]:
                worker_index, action_id = item
                restored = self.pool.restore_portable(worker_index, branch)
                if restored["state_hash"] != root["state_hash"]:
                    raise RuntimeError(f"portable root mismatch for {action_id}")
                child_worker = self.pool.workers[worker_index]
                child = child_worker.step(action_id)
                return {"action": legal_by_id[action_id], "state": child, "branch": child_worker.export_branch()}

            values = list(enumerate(batch))
            expansions.extend(self.pool.map(execute, values))
        return expansions

    def rank(
        self,
        scorer: Callable[[dict[str, Any]], float],
        source_worker_index: int = 0,
        action_ids: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        ranked = []
        for ordinal, expansion in enumerate(self.expand(source_worker_index, action_ids)):
            ranked.append({
                "action": expansion["action"],
                "score": float(scorer(expansion["state"])),
                "state_hash": expansion["state"]["state_hash"],
                "decision": expansion["state"]["observation"]["decision"]["kind"],
                "ordinal": ordinal,
            })
        ranked.sort(key=lambda candidate: (-candidate["score"], candidate["ordinal"]))
        return ranked

    def select(
        self,
        scorer: Callable[[dict[str, Any]], float],
        source_worker_index: int = 0,
        action_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        ranked = self.rank(scorer, source_worker_index, action_ids)
        if not ranked:
            raise ValueError("the native root has no requested legal actions")
        return ranked[0]

    def search(
        self,
        scorer: Callable[[dict[str, Any]], float],
        *,
        max_depth: int = 2,
        node_budget: int = 64,
        beam_width: int = 8,
        source_worker_index: int = 0,
    ) -> dict[str, Any]:
        if max_depth < 1 or node_budget < 1 or beam_width < 1:
            raise ValueError("max_depth, node_budget, and beam_width must be positive")
        source = self.pool._replace_if_dead(source_worker_index)
        root_state = source.observe()
        root_branch = source.export_branch()
        root_score = float(scorer(root_state))
        frontier = [{"branch": root_branch, "state": root_state, "path": [], "score": root_score, "ordinal": 0}]
        best = None
        seen = {_branch_identity(root_branch)}
        nodes = 0
        duplicate_branch_hits = 0
        ordinal = 1
        depth_reached = 0
        try:
            for depth in range(1, max_depth + 1):
                next_frontier = []
                for node in frontier:
                    remaining = node_budget - nodes
                    if remaining <= 0:
                        break
                    legal_ids = [action["action_id"] for action in node["state"]["legal_actions"]][:remaining]
                    if not legal_ids:
                        continue
                    for expansion in self._expand_branch(node["branch"], legal_ids):
                        nodes += 1
                        child = expansion["state"]
                        child_branch = expansion["branch"]
                        branch_id = _branch_identity(child_branch)
                        if branch_id in seen:
                            duplicate_branch_hits += 1
                            continue
                        seen.add(branch_id)
                        candidate = {
                            "branch": child_branch,
                            "state": child,
                            "path": node["path"] + [expansion["action"]["action_id"]],
                            "score": float(scorer(child)),
                            "ordinal": ordinal,
                        }
                        ordinal += 1
                        next_frontier.append(candidate)
                        if best is None or candidate["score"] > best["score"]:
                            best = candidate
                    if nodes >= node_budget:
                        break
                if not next_frontier:
                    break
                depth_reached = depth
                next_frontier.sort(key=lambda candidate: (-candidate["score"], candidate["ordinal"]))
                frontier = next_frontier[:beam_width]
            selected = best or frontier[0]
            return {
                "root_hash": root_state["state_hash"],
                "best_path": selected["path"],
                "best_score": selected["score"],
                "best_state_hash": selected["state"]["state_hash"],
                "best_decision": selected["state"]["observation"]["decision"]["kind"],
                "nodes_evaluated": nodes,
                "unique_states": len(seen),
                "duplicate_branch_hits": duplicate_branch_hits,
                "depth_reached": depth_reached,
                "budgets": {"max_depth": max_depth, "node_budget": node_budget, "beam_width": beam_width},
            }
        finally:
            self.pool.restore_portable(source_worker_index, root_branch)

