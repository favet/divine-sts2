"""Headless census of every shipped event model; no visible game or save access."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from acceptance import SCENARIO
from sts2_native_sim import NativeSimError, NativeWorkerPool


def main() -> None:
    scenario = copy.deepcopy(SCENARIO)
    scenario.update({"seed": "NATIVE-EVENT-CORPUS", "current_hp": 70, "max_hp": 80, "gold": 300})
    artifact = Path(__file__).resolve().parents[1] / "artifacts" / "native-event-corpus-report.json"
    with NativeWorkerPool(4) as pool:
        event_ids = [event["model_id"] for event in pool.workers[0].catalog()["events"]]
        buckets = [event_ids[index::4] for index in range(4)]

        def inspect(worker, ids):
            rows = []
            for event_id in ids:
                try:
                    state = worker.event_reset(scenario, event_id)
                    observation = state["observation"]
                    rows.append({
                        "event_id": event_id,
                        "status": "initialized",
                        "option_count": len(observation["event"]["options"]),
                        "legal_action_count": len(state["legal_actions"]),
                        "finished_on_entry": observation["event"]["finished"],
                        "state_hash": state["state_hash"],
                    })
                except NativeSimError as error:
                    rows.append({"event_id": event_id, "status": error.code, "message": str(error)})
            return rows

        grouped = pool.map(inspect, buckets)
    rows = sorted((row for group in grouped for row in group), key=lambda row: row["event_id"])
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    report = {"success": counts.get("initialized", 0) > 0, "event_count": len(rows), "status_counts": counts, "events": rows}
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({**{key: value for key, value in report.items() if key != "events"}, "artifact": str(artifact)}, indent=2))


if __name__ == "__main__":
    main()
