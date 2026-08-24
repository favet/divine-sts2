"""Extract exact state/action macro examples from agentic STS2 JSONL archives.

Unlike the legacy timeline parser, this extractor never invents offered options
from whitespace-separated reward text. Each example pairs a structured `state`
event with the successful `decision` emitted for the same run, step, and phase.
Terminal outcome is attached only after the corresponding `run_end` event.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = REPO_ROOT / "community_runs" / "agentic_sts"
DEFAULT_OUTPUT = REPO_ROOT / "artifacts" / "agentic_macro_decisions.jsonl"
DEFAULT_REPORT = REPO_ROOT / "artifacts" / "agentic_macro_decisions_report.json"

MACRO_STATE_TYPES = {
    "card_reward",
    "card_select",
    "event",
    "map",
    "rest_site",
    "shop",
}
COMBAT_STATE_TYPES = {"monster", "elite", "boss"}


def _candidate_collection(state: Dict[str, Any], action_name: str) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    action_to_candidates = {
        "choose_reward_card": ("card_reward_details", "card_options"),
        "choose_reward_alternative": ("card_reward_details", "alternatives"),
        "choose_map_node": ("map_details", "next_options"),
        "choose_rest_option": ("rest_site", "options"),
        "choose_event_option": ("event_details", "options"),
        "select_deck_card": ("selection_details", "cards"),
        "buy_card": ("shop_details", "cards"),
        "buy_relic": ("shop_details", "relics"),
        "buy_potion": ("shop_details", "potions"),
    }
    if state.get("state_type") == "card_reward":
        details = state.get("card_reward_details") or {}
        candidates = [dict(item, _action_name="choose_reward_card") for item in details.get("card_options") or []]
        candidates.extend(dict(item, _action_name="choose_reward_alternative") for item in details.get("alternatives") or [])
        return candidates, None
    if state.get("state_type") == "shop":
        details = state.get("shop_details") or {}
        candidates = [dict(item, _action_name="buy_card") for item in details.get("cards") or []]
        candidates.extend(dict(item, _action_name="buy_relic") for item in details.get("relics") or [])
        candidates.extend(dict(item, _action_name="buy_potion") for item in details.get("potions") or [])
        removal = details.get("card_removal") or {}
        if removal.get("available"):
            candidates.append(dict(removal, index=-1, name="Card Removal", _action_name="remove_card_at_shop"))
        candidates.append({"index": -1, "name": "Leave", "_action_name": "close_shop_inventory"})
        return candidates, None
    path = action_to_candidates.get(action_name)
    if path is None:
        return [], None
    candidates = [dict(item, _action_name=action_name) for item in (state.get(path[0]) or {}).get(path[1]) or []]
    return candidates, None


def _selected_index(action: Dict[str, Any]) -> Optional[int]:
    for field in ("option_index", "card_index", "potion_index"):
        value = action.get(field)
        if value is not None:
            return int(value)
    return None


def _is_strategic_decision(state_type: str, action_name: str) -> bool:
    if state_type in MACRO_STATE_TYPES:
        return action_name != "proceed"
    return state_type in COMBAT_STATE_TYPES and action_name in {"play_card", "end_turn", "use_potion"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_archives(paths: Sequence[Path]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    examples_by_run: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    outcomes: Dict[str, Dict[str, Any]] = {}
    counts: Counter[str] = Counter()
    action_counts: Counter[str] = Counter()
    input_hashes: Dict[str, str] = {}

    for path in sorted(paths):
        input_hashes[str(path)] = _sha256(path)
        states: Dict[Tuple[str, int, str], Dict[str, Any]] = {}
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    counts["invalid_json"] += 1
                    continue

                event_type = event.get("event")
                run_id = str(event.get("run_id", ""))
                if event_type == "state":
                    key = (run_id, int(event.get("step", -1)), str(event.get("state_type", "")))
                    states[key] = event
                    continue
                if event_type == "run_end":
                    outcomes[run_id] = {
                        "victory": bool(event.get("victory", False)),
                        "terminal_floor": int(event.get("floor", 0)),
                        "completion_reason": event.get("completion_reason"),
                        "end_reason": event.get("end_reason"),
                    }
                    continue
                if event_type != "decision":
                    continue

                state_type = str(event.get("state_type", ""))
                action = event.get("action") or {}
                action_name = str(action.get("action", ""))
                if not _is_strategic_decision(state_type, action_name):
                    continue
                if event.get("source") == "random":
                    counts["random_source_excluded"] += 1
                    continue

                key = (run_id, int(event.get("step", -1)), state_type)
                state = states.get(key)
                if state is None:
                    counts["missing_aligned_state"] += 1
                    continue
                if float(state.get("ts", 0.0)) > float(event.get("ts", 0.0)):
                    counts["post_action_state_rejected"] += 1
                    continue

                candidates, _ = _candidate_collection(state, action_name)
                selected_index = _selected_index(action)
                if candidates and selected_index is not None:
                    candidate_indices = {
                        int(candidate.get("index", position))
                        for position, candidate in enumerate(candidates)
                        if candidate.get("_action_name") == action_name
                    }
                    if selected_index not in candidate_indices:
                        counts["invalid_option_index"] += 1
                        continue

                example = {
                    "schema_version": "agentic_macro_v1",
                    "source_archive": path.name,
                    "source_line": line_number,
                    "run_id": run_id,
                    "step": int(event.get("step", -1)),
                    "floor": int(event.get("floor", state.get("floor", 0))),
                    "state_type": state_type,
                    "action_name": action_name,
                    "selected_index": selected_index,
                    "action": action,
                    "candidates": candidates,
                    "state": state,
                    "reasoning": event.get("reasoning", ""),
                    "decision_source": event.get("source", ""),
                }
                examples_by_run[run_id].append(example)
                action_counts[f"{state_type}:{action_name}"] += 1

    examples: List[Dict[str, Any]] = []
    for run_id in sorted(examples_by_run):
        outcome = outcomes.get(run_id)
        if outcome is None:
            counts["missing_terminal_outcome"] += len(examples_by_run[run_id])
            continue
        for example in examples_by_run[run_id]:
            example["outcome"] = outcome
            examples.append(example)

    card_examples = [e for e in examples if e["action_name"] == "choose_reward_card"]
    first_picks = sum(e.get("selected_index") == 0 for e in card_examples)
    report = {
        "schema_version": "agentic_macro_v1",
        "input_archives": len(paths),
        "input_sha256": input_hashes,
        "examples": len(examples),
        "runs_with_outcomes": len({e["run_id"] for e in examples}),
        "victory_examples": sum(bool(e["outcome"]["victory"]) for e in examples),
        "action_counts": dict(sorted(action_counts.items())),
        "card_reward_first_option_rate": round(first_picks / max(1, len(card_examples)), 6),
        "rejections": dict(sorted(counts.items())),
    }
    return examples, report


def write_outputs(examples: Iterable[Dict[str, Any]], report: Dict[str, Any], output: Path, report_path: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for example in examples:
            handle.write(json.dumps(example, sort_keys=True, separators=(",", ":")) + "\n")
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    paths = sorted(args.input_dir.glob("*.jsonl.gz"))
    if not paths:
        raise SystemExit(f"No agentic JSONL archives found under {args.input_dir}")
    examples, report = extract_archives(paths)
    if not examples:
        raise SystemExit("No aligned strategic decisions were extracted")
    write_outputs(examples, report, args.output, args.report)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
