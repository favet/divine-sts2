"""Self-test for trace parsing/comparison; this does not count as differential coverage."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from acceptance import SCENARIO
from differential_replay import replay
from sts2_native_sim import NativeWorker


def write_trace(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records), encoding="utf-8")


def main() -> None:
    with NativeWorker() as worker:
        initial = worker.reset(SCENARIO)
        action = next(a["action_id"] for a in initial["legal_actions"] if a["kind"] == "play_card")
        after = worker.step(action)
        records = [
            {"type": "header", "format_version": 1, "source": "simulator_self_test", "game_build": worker.build, "reset": SCENARIO},
            {"type": "checkpoint", "sequence": 0, "action_id": None, "observation": initial["observation"], "state_hash": initial["state_hash"]},
            {"type": "checkpoint", "sequence": 1, "action_id": action, "observation": after["observation"], "state_hash": after["state_hash"]},
        ]

    with tempfile.TemporaryDirectory(prefix="sts2-differential-self-test-") as directory:
        trace = Path(directory) / "trace.jsonl"
        write_trace(trace, records)
        passing = replay(trace)
        assert passing["success"] and not passing["certifying"] and passing["checkpoints"] == 2
        exact_audit = replay(trace, require_exact=True)
        assert exact_audit["success"] and not exact_audit["certifying"] and exact_audit["validation_comparison"] == "exact"

        records[1]["observation"]["combat"]["energy"] += 1
        write_trace(trace, records)
        failing = replay(trace)
        assert not failing["success"] and failing["difference"]["path"] == "$.combat.energy"

        print(json.dumps({"success": True, "certifying": False, "passing_checkpoints": passing["checkpoints"], "exact_audit": True, "detected_path": failing["difference"]["path"]}, indent=2))


if __name__ == "__main__":
    main()
