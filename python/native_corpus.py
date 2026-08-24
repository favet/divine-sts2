"""Automatically exercise the shipped native card and encounter catalogs headlessly."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from sts2_native_sim import NativeSimError, NativeWorker


def card_reset(model_id: str) -> dict[str, Any]:
    deck = [{"instance_id": "candidate", "model_id": model_id}]
    deck += [{"instance_id": f"strike-{index}", "model_id": "STRIKE_IRONCLAD"} for index in range(4)]
    deck += [{"instance_id": f"defend-{index}", "model_id": "DEFEND_IRONCLAD"} for index in range(5)]
    return {
        "game_build": {}, "seed": "NATIVE-CARD-CORPUS", "rng_counters": {},
        "character": "IRONCLAD", "ascension": 0, "encounter": "NIBBITS_WEAK",
        "current_hp": 80, "max_hp": 80, "gold": 99, "deck": deck,
        "initial_hand": ["candidate", "strike-0", "strike-1", "defend-0", "defend-1"],
        "relics": [], "potions": [], "turn": 1, "energy": 99,
    }


def encounter_reset(model_id: str) -> dict[str, Any]:
    deck = [{"instance_id": f"strike-{index}", "model_id": "STRIKE_IRONCLAD"} for index in range(5)]
    deck += [{"instance_id": f"defend-{index}", "model_id": "DEFEND_IRONCLAD"} for index in range(5)]
    return {
        "game_build": {}, "seed": "NATIVE-ENCOUNTER-CORPUS", "rng_counters": {},
        "character": "IRONCLAD", "ascension": 0, "encounter": model_id,
        "current_hp": 80, "max_hp": 80, "gold": 99, "deck": deck,
        "initial_hand": ["strike-0", "strike-1", "strike-2", "defend-0", "defend-1"],
        "relics": [], "potions": [], "turn": 1, "energy": 3,
    }


def error_record(error: Exception) -> dict[str, Any]:
    if isinstance(error, NativeSimError):
        return {"error_code": error.code, "error": str(error), "details": error.details}
    return {"error_code": type(error).__name__, "error": str(error)}


def replace_worker(worker: NativeWorker) -> NativeWorker:
    try:
        worker.close()
    except Exception:
        pass
    return NativeWorker()


def run(output: Path, card_limit: int | None, encounter_limit: int | None) -> dict[str, Any]:
    worker = NativeWorker()
    catalog = worker.catalog()
    cards = catalog["cards"][:card_limit] if card_limit else catalog["cards"]
    encounters = catalog["encounters"][:encounter_limit] if encounter_limit else catalog["encounters"]
    card_results: list[dict[str, Any]] = []
    encounter_results: list[dict[str, Any]] = []
    try:
        for card in cards:
            result = {"model_id": card["model_id"], "card_type": card.get("card_type"), "target_type": card.get("target_type")}
            if card.get("card_type") == "None" or card["model_id"] == "DEPRECATED_CARD":
                result.update({"status": "non_runtime_template", "runtime_type": card.get("runtime_type")})
                card_results.append(result)
                continue
            try:
                initial = worker.reset(card_reset(card["model_id"]))
                actions = [action for action in initial["legal_actions"] if action["parameters"].get("instance_id") == "candidate"]
                if not actions:
                    result["status"] = "not_native_playable_in_baseline"
                else:
                    action = actions[0]
                    after = worker.step(action["action_id"])
                    result.update({
                        "status": "played", "action_id": action["action_id"],
                        "decision": after["observation"]["decision"]["kind"],
                        "legal_actions_after": len(after["legal_actions"]),
                        "terminated": after["terminated"], "state_hash": after["state_hash"],
                    })
            except Exception as error:
                result.update({"status": "error", **error_record(error)})
                if worker.process.poll() is not None:
                    worker = replace_worker(worker)
            card_results.append(result)

        for encounter in encounters:
            result = {"model_id": encounter["model_id"]}
            try:
                initial = worker.reset(encounter_reset(encounter["model_id"]))
                enemies = [creature["model_id"] for creature in initial["observation"]["combat"]["creatures"] if creature["side"] == "Enemy"]
                after = worker.step("end_turn")
                result.update({
                    "status": "completed_turn", "enemies": enemies,
                    "turn_after": after["observation"]["combat"]["turn"],
                    "terminated": after["terminated"], "state_hash": after["state_hash"],
                })
            except Exception as error:
                result.update({"status": "error", **error_record(error)})
                if worker.process.poll() is not None:
                    worker = replace_worker(worker)
            encounter_results.append(result)
    finally:
        worker.close()

    report = {
        "game_build": catalog["game_build"],
        "catalog_counts": {name: len(catalog[name]) for name in ("cards", "encounters", "relics", "potions", "characters", "enchantments")},
        "attempted": {"cards": len(card_results), "encounters": len(encounter_results)},
        "card_status": dict(sorted(Counter(result["status"] for result in card_results).items())),
        "encounter_status": dict(sorted(Counter(result["status"] for result in encounter_results).items())),
        "cards": card_results,
        "encounters": encounter_results,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("artifacts/native-corpus-report.json"))
    parser.add_argument("--card-limit", type=int)
    parser.add_argument("--encounter-limit", type=int)
    args = parser.parse_args()
    report = run(args.output, args.card_limit, args.encounter_limit)
    print(json.dumps({key: report[key] for key in ("game_build", "catalog_counts", "attempted", "card_status", "encounter_status")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
