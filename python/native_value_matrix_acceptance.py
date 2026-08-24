"""Acceptance that rejected critics cannot silently guide native search."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from native_corpus import encounter_reset
from sts2_native_sim import NativeSearchCoordinator, NativeTorchValueScorer, NativeWorkerPool


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    checkpoint = root / "artifacts" / "native-value-matrix.pt"
    experimental_checkpoint = root / "artifacts" / "native-value-matrix-v6-intents-validation.pt"
    smoke_checkpoint = root / "artifacts" / "native-value-smoke.pt"
    if not checkpoint.exists() or not experimental_checkpoint.exists():
        raise RuntimeError("native value matrix artifacts are missing")

    with NativeWorkerPool(4) as pool:
        state = pool.workers[0].reset(encounter_reset("FOGMOG_NORMAL"))
        build = state["observation"]["game_build"]
        default_rejected = False
        try:
            NativeTorchValueScorer.load(checkpoint, build)
        except ValueError as error:
            default_rejected = "search-lift gate" in str(error)
        assert default_rejected
        scorer = NativeTorchValueScorer.load(experimental_checkpoint, build, allow_unpromoted=True)
        validation = scorer.metadata.get("validation_ranking") or {}
        assert validation.get("promoted") is False

        coordinator = NativeSearchCoordinator(pool)
        first = coordinator.rank(scorer)
        second = coordinator.rank(scorer)
        compact_first = [(item["action"]["action_id"], item["score"], item["state_hash"]) for item in first]
        compact_second = [(item["action"]["action_id"], item["score"], item["state_hash"]) for item in second]
        assert compact_first == compact_second
        assert coordinator.select(scorer)["action"]["action_id"] == first[0]["action"]["action_id"]

        unpromoted_rejected = False
        try:
            NativeTorchValueScorer.load(smoke_checkpoint, build)
        except ValueError as error:
            unpromoted_rejected = "promotion gate" in str(error)
        assert unpromoted_rejected

        print(json.dumps({
            "success": True,
            "checkpoint": str(checkpoint),
            "experimental_checkpoint": str(experimental_checkpoint),
            "experimental_validation_ranking": validation,
            "candidate_count": len(first),
            "selected_action": first[0]["action"]["action_id"],
            "selected_value_logit": first[0]["score"],
            "deterministic_candidate_hashes": [item["state_hash"] for item in first],
            "unpromoted_default_rejected": unpromoted_rejected,
            "sibling_promoted_but_search_lift_rejected": default_rejected,
        }, indent=2))


if __name__ == "__main__":
    main()
