"""Differential audit of an isolated, non-authoritative Python shadow simulator."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sts2_native_sim import NativeWorker
from sts2_native_sim.parity import build_trust_matrix, compare_snapshots


SCENARIO = {
    "game_build": {},
    "seed": "SHADOW-AUDIT-BYGONE-STRIKE",
    "rng_counters": {},
    "character": "IRONCLAD",
    "ascension": 0,
    "encounter": "first",
    "current_hp": 80,
    "max_hp": 80,
    "gold": 0,
    "deck": [
        {"instance_id": "strike-0", "model_id": "STRIKE_IRONCLAD"},
        {"instance_id": "strike-1", "model_id": "STRIKE_IRONCLAD"},
        {"instance_id": "strike-2", "model_id": "STRIKE_IRONCLAD"},
    ],
    "initial_hand": ["strike-0", "strike-1", "strike-2"],
    "relics": [],
    "potions": [],
}


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, encoding="utf-8", capture_output=True, check=True)


def _git(root: Path, *args: str) -> str:
    return _run(["git", "-C", str(root), *args], cwd=root).stdout.strip()


def _tracked_patch_sha256(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "diff", "HEAD", "--binary", "--no-ext-diff"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    return hashlib.sha256(result.stdout).hexdigest()


def _native_snapshot(state: dict) -> dict:
    observation = state["observation"]
    combat = observation["combat"]
    creatures = combat["creatures"]
    player = next(creature for creature in creatures if creature["side"].lower() == "player")
    enemies = []
    for creature in creatures:
        if creature["side"].lower() != "enemy":
            continue
        powers = sorted(
            ({"model_id": power["model_id"][:-6] if power["model_id"].endswith("_POWER") else power["model_id"], "amount": int(power["amount"])} for power in creature["powers"]),
            key=lambda item: item["model_id"],
        )
        next_move = creature.get("next_move")
        if next_move is not None:
            next_move = {
                "id": next_move["id"],
                "intents": [{
                    "intent_type": str(intent["intent_type"]).upper(),
                    "damage": intent.get("damage") if str(intent["intent_type"]).upper() in {"ATTACK", "MULTI_ATTACK"} else None,
                    "repeats": intent.get("repeats") if str(intent["intent_type"]).upper() in {"ATTACK", "MULTI_ATTACK"} else None,
                } for intent in next_move["intents"]],
            }
        enemies.append({
            "model_id": creature["model_id"],
            "hp": int(creature["hp"]),
            "max_hp": int(creature["max_hp"]),
            "block": int(creature["block"]),
            "alive": bool(creature["alive"]),
            "powers": powers,
            "next_move": next_move,
        })
    pile_names = {"Hand": "hand", "DrawPile": "draw", "DiscardPile": "discard", "ExhaustPile": "exhaust", "PlayPile": "play"}
    piles = {name: [] for name in pile_names.values()}
    for pile in combat["piles"]:
        piles[pile_names[pile["name"]]] = [card["model_id"] for card in pile["cards"]]
    kind_names = {"choose_cards", "play_card", "end_turn"}
    kinds = sorted({action["kind"] for action in state["legal_actions"] if action["kind"] in kind_names})
    outstanding = state["observation"].get("outstanding_choice")
    pending_choice = None
    if outstanding is not None:
        pending_choice = {
            "kind": "card_choice",
            "min_select": int(outstanding["min_select"]),
            "max_select": int(outstanding["max_select"]),
            "options": [option["model_id"] for option in outstanding["options"]],
        }
    return {
        "turn": int(combat["turn"]),
        "player": {
            "hp": int(player["hp"]),
            "max_hp": int(player["max_hp"]),
            "block": int(player["block"]),
            "energy": int(combat["energy"]),
            "max_energy": int(combat["max_energy"]),
            "powers": sorted(
                ({"model_id": power["model_id"][:-6] if power["model_id"].endswith("_POWER") else power["model_id"], "amount": int(power["amount"])}
                 for power in player["powers"]),
                key=lambda item: item["model_id"],
            ),
        },
        "enemies": enemies,
        "piles": piles,
        "legal_action_kinds": kinds,
        "pending_choice": pending_choice,
        "terminated": bool(state["terminated"]),
        "victory": bool(state["victory"]),
    }


def _native_checkpoints() -> tuple[dict, dict]:
    with NativeWorker() as worker:
        initial_state = worker.reset(SCENARIO)
        play = next(action["action_id"] for action in initial_state["legal_actions"] if action["kind"] == "play_card")
        after_play_1_state = worker.step(play)
        second_play = next(action["action_id"] for action in after_play_1_state["legal_actions"] if action["kind"] == "play_card")
        after_play_2_state = worker.step(second_play)
        third_play = next(action["action_id"] for action in after_play_2_state["legal_actions"] if action["kind"] == "play_card")
        after_play_3_state = worker.step(third_play)
        end_turn = next(action["action_id"] for action in after_play_3_state["legal_actions"] if action["kind"] == "end_turn")
        after_turn_state = worker.step(end_turn)
        return (
            {
                "initial": _native_snapshot(initial_state),
                "after_play_1": _native_snapshot(after_play_1_state),
                "after_play_2": _native_snapshot(after_play_2_state),
                "after_play_3": _native_snapshot(after_play_3_state),
                "after_turn": _native_snapshot(after_turn_state),
            },
            initial_state["observation"]["game_build"],
        )


def _verify_suite(python: Path, root: Path) -> dict:
    result = _run([str(python), "-m", "pytest", "-q", "tests"], cwd=root)
    summary = next((line for line in reversed(result.stdout.splitlines()) if " passed" in line), "")
    passed = int(match.group(1)) if (match := re.search(r"(\d+) passed", summary)) else None
    skipped = int(match.group(1)) if (match := re.search(r"(\d+) skipped", summary)) else 0
    return {"success": passed is not None, "passed": passed, "skipped": skipped, "summary": summary}


def _benchmark(python: Path, root: Path) -> dict:
    result = _run([str(python), "scripts/benchmark.py"], cwd=root)
    values: dict[str, float | int] = {}
    mapping = {"Episodes": "episodes", "Total steps": "total_steps", "Time": "seconds", "Episodes/sec": "episodes_per_second", "Steps/sec": "steps_per_second"}
    for line in result.stdout.splitlines():
        if ":" not in line:
            continue
        label, raw = (part.strip() for part in line.split(":", 1))
        if label not in mapping:
            continue
        number = raw.rstrip("s")
        values[mapping[label]] = float(number) if "." in number else int(number)
    return values


def _parallel_benchmark(python: Path, root: Path, *, workers: int, episodes: int) -> dict:
    result = _run(
        [str(python), "scripts/benchmark_parallel.py", "--workers", str(workers), "--episodes", str(episodes)],
        cwd=root,
    )
    values: dict[str, float | int | dict] = {}
    mapping = {
        "Workers": "workers",
        "Episodes": "episodes",
        "Total steps": "total_steps",
        "Time": "seconds",
        "Episodes/sec": "episodes_per_second",
        "Steps/sec": "steps_per_second",
    }
    for line in result.stdout.splitlines():
        if ":" not in line:
            continue
        label, raw = (part.strip() for part in line.split(":", 1))
        if label == "Outcomes":
            values["outcomes"] = json.loads(raw.replace("'", '"'))
            continue
        if label not in mapping:
            continue
        number = raw.rstrip("s")
        values[mapping[label]] = float(number) if "." in number else int(number)
    return values


def _enemy(snapshot: dict) -> dict:
    return snapshot["enemies"][0]


def _power_amount(snapshot: dict, model_id: str) -> int:
    return next((power["amount"] for power in _enemy(snapshot)["powers"] if power["model_id"] == model_id), 0)


def _mechanic_facts(checkpoints: dict[str, dict]) -> dict[str, dict]:
    initial, first, second, third, after_turn = (checkpoints[name] for name in ("initial", "after_play_1", "after_play_2", "after_play_3", "after_turn"))
    return {
        "ordinary_attack": {"first_damage": _enemy(initial)["hp"] - _enemy(first)["hp"]},
        "energy_spend": {
            "first_cost": initial["player"]["energy"] - first["player"]["energy"],
            "second_cost": first["player"]["energy"] - second["player"]["energy"],
            "third_cost": second["player"]["energy"] - third["player"]["energy"],
        },
        "card_pile_movement": {
            "initial_hand": len(initial["piles"]["hand"]),
            "hand_after_three": len(third["piles"]["hand"]),
            "discard_after_three": len(third["piles"]["discard"]),
        },
        "enemy_slow": {
            "initial_amount": _power_amount(initial, "SLOW"),
            "second_damage": _enemy(first)["hp"] - _enemy(second)["hp"],
            "third_damage": _enemy(second)["hp"] - _enemy(third)["hp"],
        },
        "complete_turn": {
            "turn": after_turn["turn"],
            "player_hp": after_turn["player"]["hp"],
            "energy": after_turn["player"]["energy"],
            "hand": after_turn["piles"]["hand"],
        },
        "enemy_wake_move": {
            "move_id": _enemy(after_turn)["next_move"]["id"],
            "intent_type": _enemy(after_turn)["next_move"]["intents"][0]["intent_type"],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shadow-root", type=Path, required=True)
    parser.add_argument("--shadow-python", type=Path, required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--expected-patch-sha256")
    parser.add_argument("--verify-suite", action="store_true")
    parser.add_argument("--benchmark", action="store_true")
    parser.add_argument("--parallel-benchmark-workers", type=int)
    parser.add_argument("--parallel-benchmark-episodes", type=int, default=4000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    root, python = args.shadow_root.resolve(), args.shadow_python.resolve()
    actual_revision = _git(root, "rev-parse", "HEAD")
    if actual_revision != args.expected_revision:
        raise RuntimeError(f"shadow revision mismatch: expected {args.expected_revision}, got {actual_revision}")
    patch_sha256 = _tracked_patch_sha256(root)
    if args.expected_patch_sha256 and patch_sha256 != args.expected_patch_sha256.lower():
        raise RuntimeError(f"shadow patch mismatch: expected {args.expected_patch_sha256}, got {patch_sha256}")
    probe_path = str(Path(__file__).with_name("shadow_sim_probe.py"))
    default_shadow = json.loads(_run([str(python), probe_path], cwd=root).stdout)
    shadow = json.loads(_run([str(python), probe_path, "--register-powers"], cwd=root).stdout)
    native, native_build = _native_checkpoints()
    comparisons = {name: compare_snapshots(native[name], shadow["checkpoints"][name]) for name in native}
    default_comparisons = {name: compare_snapshots(native[name], default_shadow["checkpoints"][name]) for name in native}
    native_facts, shadow_facts = _mechanic_facts(native), _mechanic_facts(shadow["checkpoints"])
    mechanic_comparisons = {mechanic: compare_snapshots(native_facts[mechanic], shadow_facts[mechanic]) for mechanic in native_facts}
    scenario = {
        "scenario_id": shadow["scenario_id"],
        "comparisons": comparisons,
        "mechanic_facts": {"native": native_facts, "shadow": shadow_facts},
        "mechanic_comparisons": mechanic_comparisons,
    }
    matrix = build_trust_matrix([scenario])
    report = {
        "schema_version": 1,
        "success": all(item["status"] == "python_parity_verified" for item in mechanic_comparisons.values()),
        "native_build": native_build,
        "shadow": {
            "repository": "https://github.com/zhiyue/sts2-rl-agent",
            "revision": actual_revision,
            "tracked_patch_sha256": patch_sha256,
            "tracked_patch_files": _git(root, "diff", "HEAD", "--name-only").splitlines(),
            "commit_date": _git(root, "show", "-s", "--format=%cI", "HEAD"),
            "license_file_present": any((root / name).is_file() for name in ("LICENSE", "LICENSE.md", "LICENSE.txt")),
            "suite": _verify_suite(python, root) if args.verify_suite else None,
            "benchmark": _benchmark(python, root) if args.benchmark else None,
            "parallel_benchmark": _parallel_benchmark(
                python,
                root,
                workers=args.parallel_benchmark_workers,
                episodes=args.parallel_benchmark_episodes,
            ) if args.parallel_benchmark_workers else None,
            "required_bootstrap": {
                "import": "sts2_env.powers",
                "production_probe_matches_without_import": all(item["status"] == "python_parity_verified" for item in default_comparisons.values()),
                "default_checkpoint_comparisons": default_comparisons,
                "warning": (
                    "The isolated research patch makes CombatState initialize the power registry directly."
                    if all(item["status"] == "python_parity_verified" for item in default_comparisons.values())
                    else "Production initialization still differs from the explicitly bootstrapped probe."
                ),
            },
        },
        "scenario": scenario,
        "trust_matrix": matrix,
        "training_policy": {
            "default_shadow_provenance": "python_unverified",
            "verified_slice_provenance": "python_parity_verified",
            "shadow_labels_authoritative": False,
            "eligible_for_default_scorer_training": False,
            "native_relabel_required": True,
            "scope_warning": "Parity applies only to the exact compared fields and action sequence on the pinned revisions.",
        },
    }
    encoded = json.dumps(report, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    if not report["success"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
