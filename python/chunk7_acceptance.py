"""Chunk 7 acceptance tests:

1. 1000-observe idempotency: After a single reset, 1000 calls to observe()
   must leave branch_count strictly equal to 1.

2. Deep branch retention: A 50-step trajectory produces leaf handles that
   restore cleanly because History is stored directly on each Branch record
   -- immune to ancestor eviction regardless of LRU order.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sts2_native_sim import NativeWorkerPool

_DECK = (
    [{"instance_id": f"strike-{i}", "model_id": "STRIKE_IRONCLAD"} for i in range(5)]
    + [{"instance_id": f"defend-{i}", "model_id": "DEFEND_IRONCLAD"} for i in range(5)]
)
_RESET = {
    "game_build": {},
    "seed": "CHUNK7TESTS",
    "rng_counters": {},
    "character": "IRONCLAD",
    "ascension": 0,
    "encounter": "NIBBITS_WEAK",
    "current_hp": 80,
    "max_hp": 80,
    "gold": 99,
    "deck": _DECK,
    "initial_hand": ["strike-0"],
    "relics": [],
    "potions": [],
}


def test_observe_idempotency(worker) -> None:
    """1000 Observe() calls on an unchanged worker must leave branch_count == 1."""
    worker.reset(_RESET)
    initial = worker.diagnostics()
    assert initial["branch_count"] == 1, (
        f"Expected 1 branch after reset, got {initial['branch_count']}"
    )
    for _ in range(1000):
        worker.observe()
    diag = worker.diagnostics()
    assert diag["branch_count"] == 1, (
        f"branch_count drifted to {diag['branch_count']} after 1000 observe() calls"
    )
    print(f"  [PASS] 1000 observe() calls: branch_count remains {diag['branch_count']}")


def test_deep_branch_retention(worker) -> None:
    """50-step trajectory: deepest leaf restores cleanly despite ancestor eviction."""
    state = worker.reset(_RESET)
    handles = [state["state_handle"]]
    hashes = [state["state_hash"]]

    for _ in range(50):
        legal = state["legal_actions"]
        if not legal:
            break
        action_id = sorted(a["action_id"] for a in legal)[0]
        state = worker.step(action_id)
        handles.append(state["state_handle"])
        hashes.append(state["state_hash"])

    steps_taken = len(handles) - 1
    leaf_handle = handles[-1]
    leaf_hash = hashes[-1]

    diag = worker.diagnostics()
    print(f"  Branch count after {steps_taken} steps: {diag['branch_count']}")

    restored = worker.restore(leaf_handle)
    assert restored["state_hash"] == leaf_hash, (
        f"Leaf restore diverged: expected {leaf_hash}, got {restored['state_hash']}"
    )
    print(f"  [PASS] Leaf handle restored cleanly: {leaf_hash[:16]}...")

    root_restored = worker.restore(handles[0])
    assert root_restored is not None
    print(f"  [PASS] Root handle still restorable after {steps_taken}-step trajectory")


def main() -> None:
    with NativeWorkerPool(1) as pool:
        worker = pool.workers[0]

        print("--- Test 1: Observe idempotency ---")
        test_observe_idempotency(worker)

        print("--- Test 2: Deep branch retention ---")
        test_deep_branch_retention(worker)

        print(json.dumps({
            "success": True,
            "workers": 1,
            "tests": ["observe_idempotency_1000", "deep_branch_retention_50_steps"],
            "steps_taken": "up to 50",
        }, indent=2))


if __name__ == "__main__":
    main()
