"""Discover, strictly replay, hash, and aggregate all available differential traces."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).parent))
from differential_replay import replay
from trace_inventory import semantic_checkpoint_hashes, summarize


def default_roots() -> list[Path]:
    appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return [
        appdata / "SlayTheSpire2" / "native_sim_traces",
        appdata / "Godot" / "app_userdata" / "STS2 Native Simulator Godot Host" / "native_sim_traces",
    ]


def discover(inputs: Iterable[Path]) -> list[Path]:
    traces: dict[str, Path] = {}
    for target in inputs:
        if target.is_file() and target.suffix.lower() == ".jsonl":
            traces[str(target.resolve()).lower()] = target.resolve()
        elif target.is_dir():
            for trace in target.rglob("*.jsonl"):
                traces[str(trace.resolve()).lower()] = trace.resolve()
    return sorted(traces.values(), key=lambda path: str(path).lower())


def aggregate(entries: list[dict[str, Any]]) -> dict[str, Any]:
    certifying = [entry for entry in entries if entry["replay"].get("certifying") and entry["replay"].get("success")]
    action_counts: Counter[str] = Counter()
    card_counts: Counter[str] = Counter()
    for entry in certifying:
        action_counts.update(entry["inventory"]["actions"])
        for resolution in entry["replay"].get("resolved_choices", []):
            action_counts.update(action_id.split(":", 1)[0] for action_id in resolution["action_ids"])
        card_counts.update(entry["inventory"]["played_cards"])
    powers_by_side: dict[str, dict[str, set[int | float]]] = {}
    for entry in certifying:
        for side, powers in entry["inventory"]["powers"].items():
            for model, amounts in powers.items():
                powers_by_side.setdefault(side, {}).setdefault(model, set()).update(amounts)
    return {
        "trace_count": len(entries),
        "passing_count": sum(bool(entry["replay"].get("success")) for entry in entries),
        "certifying_trace_count": len(certifying),
        "certifying_checkpoint_count": sum(entry["replay"]["checkpoints"] for entry in certifying),
        "characters": sorted({entry["inventory"]["character"] for entry in certifying if entry["inventory"]["character"]}),
        "encounters": sorted({entry["inventory"]["encounter"] for entry in certifying if entry["inventory"]["encounter"]}),
        "encounter_tiers": sorted({entry["inventory"]["encounter_tier"] for entry in certifying if entry["inventory"]["encounter_tier"]}),
        "actions": dict(sorted(action_counts.items())),
        "played_cards": dict(sorted(card_counts.items())),
        "observed_cards": sorted({model for entry in certifying for model in entry["inventory"]["observed_cards"]}),
        "enemy_models": sorted({model for entry in certifying for model in entry["inventory"]["enemy_models"]}),
        "enemy_moves": sorted({move for entry in certifying for move in entry["inventory"]["enemy_moves"]}),
        "intents": sorted({intent for entry in certifying for intent in entry["inventory"]["intents"]}),
        "powers": {
            side: {model: sorted(amounts) for model, amounts in sorted(powers.items())}
            for side, powers in sorted(powers_by_side.items())
        },
        "relics": sorted({model for entry in certifying for model in entry["inventory"]["relics"]}),
        "potions": sorted({model for entry in certifying for model in entry["inventory"]["potions"]}),
        "orbs": sorted({model for entry in certifying for model in entry["inventory"].get("orbs", [])}),
        "orb_states": [json.loads(value) for value in sorted(
            {json.dumps(state, sort_keys=True, separators=(",", ":")) for entry in certifying for state in entry["inventory"].get("orb_states", [])}
        )],
        "terminal_victory_traces": sum(entry["inventory"]["terminal"] is True and entry["inventory"]["victory"] is True for entry in certifying),
        "terminal_outcomes": dict(sorted(Counter(entry["inventory"]["terminal_outcome"] for entry in certifying).items())),
        "character_resources": {
            key: sorted({value for entry in certifying for value in entry["inventory"]["character_resources"][key]})
            for key in ("energy", "max_energy", "stars", "orb_capacity")
        },
        "global_certification": False,
        "scope": "Only the exact checkpoints and mechanics enumerated above are certified; unobserved mechanics remain non-certifying.",
    }


def coverage_inventory(entries: list[dict[str, Any]]) -> dict[str, Any]:
    certifying = [entry for entry in entries if entry["replay"].get("certifying") and entry["replay"].get("success")]
    builds = {json.dumps(entry["inventory"]["game_build"], sort_keys=True, separators=(",", ":")) for entry in certifying}
    if len(builds) != 1:
        raise ValueError(f"Certified campaign must contain exactly one build fingerprint; found {len(builds)}.")
    build = json.loads(next(iter(builds)))
    semantic_hashes = sorted({value for entry in certifying for value in entry["semantic_checkpoint_sha256"]})
    return {
        "format_version": 1,
        "game_build": build,
        "build_key": f'{build.get("version")}|{build.get("assembly_sha256")}|{build.get("pck_sha256")}',
        "certified_trace_sha256": sorted(entry["sha256"] for entry in certifying),
        "semantic_checkpoint_sha256": semantic_hashes,
        "exact_sets": aggregate(entries),
        "global_certification": False,
    }


def run_campaign(traces: list[Path]) -> dict[str, Any]:
    entries = []
    for path in traces:
        entries.append({
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "inventory": summarize(path),
            "semantic_checkpoint_sha256": semantic_checkpoint_hashes(path),
            "replay": replay(path, require_exact=True),
        })
    return {"format_version": 2, "traces": entries, "aggregate": aggregate(entries), "coverage_inventory": coverage_inventory(entries)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="*", type=Path, help="Trace files or directories; defaults to both STS2 trace directories.")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--coverage-output", type=Path)
    args = parser.parse_args()
    traces = discover(args.inputs or default_roots())
    report = run_campaign(traces)
    rendered = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    if args.coverage_output:
        args.coverage_output.parent.mkdir(parents=True, exist_ok=True)
        args.coverage_output.write_text(json.dumps(report["coverage_inventory"], indent=2) + "\n", encoding="utf-8")
    print(rendered, end="")
    if not traces:
        return 2
    return 0 if all(entry["replay"].get("success") for entry in report["traces"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
