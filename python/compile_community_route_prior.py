"""Compile successful-run node occupancy used to calibrate exact route choices."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INPUT_DIR = REPO_ROOT / "game_database" / "synergy_dataset" / "raw_a10_runs"
OUTPUT = REPO_ROOT / "artifacts" / "community_route_prior.json"


def normalize_node(value: str) -> str:
    token = str(value or "").upper().replace(" ", "")
    return "RESTSITE" if token == "REST" else token


def compile_prior(input_dir: Path = INPUT_DIR):
    visits = defaultdict(Counter)
    runs = Counter()
    elite_totals = Counter()
    hashes = set()
    for path in sorted(input_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        run_hash = str(data.get("run_hash", path.stem))
        if run_hash in hashes or str(data.get("result", "")).upper() != "WIN":
            continue
        hashes.add(run_hash)
        seen_acts = set()
        for entry in data.get("timeline") or []:
            try:
                act = min(3, max(1, int(entry.get("Act", 1))))
            except (TypeError, ValueError):
                act = 1
            node = normalize_node(entry.get("Type", ""))
            if not node:
                continue
            visits[act][node] += 1
            elite_totals[act] += int(node == "ELITE")
            seen_acts.add(act)
        for act in seen_acts:
            runs[act] += 1

    result = {"schema_version": "community_route_v1", "source": "A10 winning run timelines", "runs": len(hashes), "acts": {}}
    for act in (1, 2, 3):
        total_visits = sum(visits[act].values())
        result["acts"][str(act)] = {
            "runs": runs[act],
            "elite_mean": round(elite_totals[act] / max(1, runs[act]), 6),
            "node_visit_share": {
                node: round(count / max(1, total_visits), 8)
                for node, count in sorted(visits[act].items())
            },
        }
    return result


def main():
    prior = compile_prior()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(prior, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(prior, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
