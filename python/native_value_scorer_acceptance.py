"""Provenance-gated native value checkpoint search integration acceptance."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from native_corpus import encounter_reset
from sts2_native_sim import NativeSearchCoordinator, NativeTorchValueScorer, NativeWorkerPool


def main() -> None:
    checkpoint = Path(__file__).resolve().parents[1] / "artifacts" / "native-value-smoke.pt"
    if not checkpoint.exists():
        raise RuntimeError("run train_native_value_smoke.py before this acceptance")
    with NativeWorkerPool(4) as pool:
        state = pool.workers[0].reset(encounter_reset("FOGMOG_NORMAL"))
        scorer = NativeTorchValueScorer.load(checkpoint, state["observation"]["game_build"], allow_unpromoted=True)
        coordinator = NativeSearchCoordinator(pool)
        first = coordinator.rank(scorer)
        second = coordinator.rank(scorer)
        compact_first = [(item["action"]["action_id"], item["score"], item["state_hash"]) for item in first]
        compact_second = [(item["action"]["action_id"], item["score"], item["state_hash"]) for item in second]
        assert compact_first == compact_second
        mismatched = dict(state["observation"]["game_build"])
        mismatched["assembly_sha256"] = "00" * 32
        mismatch_rejected = False
        try:
            NativeTorchValueScorer.load(checkpoint, mismatched, allow_unpromoted=True)
        except ValueError:
            mismatch_rejected = True
        assert mismatch_rejected
        print(json.dumps({
            "success": True,
            "checkpoint": str(checkpoint),
            "metadata": scorer.metadata,
            "root_hash": state["state_hash"],
            "candidate_count": len(first),
            "selected_action": first[0]["action"]["action_id"],
            "selected_value_logit": first[0]["score"],
            "build_mismatch_rejected": mismatch_rejected,
            "candidate_hashes": [item["state_hash"] for item in first],
        }, indent=2))


if __name__ == "__main__":
    main()
