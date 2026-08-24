"""Cross-worker portable branch provenance for every environment reset mode."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from acceptance import SCENARIO
from sts2_native_sim import NativeWorkerPool


def main() -> None:
    with NativeWorkerPool(2) as pool:
        source = pool.workers[0]
        cases = [
            ("combat", lambda: source.reset(SCENARIO)),
            ("run", lambda: source.run_reset(SCENARIO)),
            ("map", lambda: source.map_reset(SCENARIO)),
            ("card_reward", lambda: source.reward_reset(SCENARIO)),
            ("item_reward", lambda: source.item_reward_reset(SCENARIO, "potion")),
            ("custom_reward", lambda: source.custom_reward_reset(SCENARIO, ["gold"])),
            ("rest", lambda: source.rest_reset(SCENARIO)),
            ("event", lambda: source.event_reset(SCENARIO, "THIS_OR_THAT")),
        ]
        results = {}
        for name, reset in cases:
            state = reset()
            action_id = state["legal_actions"][0]["action_id"]
            stepped = source.step(action_id)
            branch = source.export_branch()
            restored = pool.restore_portable(1, branch)
            assert restored["state_hash"] == stepped["state_hash"]
            assert restored["scoring_features"] == stepped["scoring_features"]
            results[name] = {"action_id": action_id, "hash": restored["state_hash"]}
        print(json.dumps({"success": True, "workers": 2, "modes": results}, indent=2))


if __name__ == "__main__":
    main()
