"""Multi-scenario native differential gate for the isolated shadow simulator."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from shadow_sim_audit import _native_snapshot
from sts2_native_sim import NativeWorker
from sts2_native_sim.parity import compare_snapshots


SCENARIOS = {
    "bygone_three_strike_turn": {
        "cards": [
            ("strike-0", "STRIKE_IRONCLAD"),
            ("strike-1", "STRIKE_IRONCLAD"),
            ("strike-2", "STRIKE_IRONCLAD"),
        ],
        "actions": [
            ("after_play_1", "strike-0"),
            ("after_play_2", "strike-1"),
            ("after_play_3", "strike-2"),
            ("after_turn", "end_turn"),
        ],
    },
    "bygone_defend_strike_turn": {
        "cards": [("defend-0", "DEFEND_IRONCLAD"), ("strike-0", "STRIKE_IRONCLAD")],
        "actions": [("after_defend", "defend-0"), ("after_strike", "strike-0"), ("after_turn", "end_turn")],
    },
    "bygone_bash_turn": {
        "cards": [("bash-0", "BASH")],
        "actions": [("after_bash", "bash-0"), ("after_turn", "end_turn")],
    },
    "bygone_purity_choice": {
        "cards": [
            ("purity-0", "PURITY"),
            ("strike-0", "STRIKE_IRONCLAD"),
            ("defend-0", "DEFEND_IRONCLAD"),
            ("strike-1", "STRIKE_IRONCLAD"),
        ],
        "actions": [
            ("after_play", "purity-0"),
            ("after_choice", ("strike-0", "defend-0")),
        ],
    },
    "axebot_defend_turn": {
        "encounter": "AXEBOTS_NORMAL",
        "cards": [("defend-0", "DEFEND_IRONCLAD")],
        "actions": [("after_defend", "defend-0"), ("after_turn", "end_turn")],
    },
    "bowlbugs_normal_initial": {
        "encounter": "BOWLBUGS_NORMAL",
        "cards": [("defend-0", "DEFEND_IRONCLAD")],
        "actions": [],
    },
    "seapunk_normal_initial": {
        "encounter": "SEAPUNK_NORMAL",
        "cards": [("defend-0", "DEFEND_IRONCLAD")],
        "actions": [],
    },
    "aeonglass_boss_turn": {
        "encounter": "AEONGLASS_BOSS",
        "cards": [("defend-0", "DEFEND_IRONCLAD")],
        "actions": [("after_turn", "end_turn")],
    },
}


def _run(command: list[str], cwd: Path) -> str:
    return subprocess.run(command, cwd=cwd, text=True, encoding="utf-8", capture_output=True, check=True).stdout


def _patch_sha(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "diff", "HEAD", "--binary", "--no-ext-diff"],
        capture_output=True,
        check=True,
    )
    return hashlib.sha256(result.stdout).hexdigest()


def _native_checkpoints(name: str, spec: dict) -> dict[str, dict]:
    deck = [{"instance_id": instance_id, "model_id": model_id} for instance_id, model_id in spec["cards"]]
    scenario = {
        "game_build": {},
        "seed": f"SHADOW-MATRIX-{name}",
        "rng_counters": {},
        "character": "IRONCLAD",
        "ascension": 0,
        "encounter": spec.get("encounter", "first"),
        "current_hp": 80,
        "max_hp": 80,
        "gold": 0,
        "deck": deck,
        "initial_hand": [card["instance_id"] for card in deck],
        "relics": [],
        "potions": [],
    }
    with NativeWorker() as worker:
        state = worker.reset(scenario)
        checkpoints = {"initial": _native_snapshot(state)}
        for checkpoint, instance_id in spec["actions"]:
            if instance_id == "end_turn":
                action_id = next(action["action_id"] for action in state["legal_actions"] if action["kind"] == "end_turn")
            elif isinstance(instance_id, tuple):
                selected = list(instance_id)
                action_id = next(
                    action["action_id"]
                    for action in state["legal_actions"]
                    if action["kind"] == "choose_cards" and action["parameters"]["option_ids"] == selected
                )
            else:
                action_id = next(
                    action["action_id"]
                    for action in state["legal_actions"]
                    if action["kind"] == "play_card" and action["parameters"]["instance_id"] == instance_id
                )
            state = worker.step(action_id)
            checkpoints[checkpoint] = _native_snapshot(state)
        return checkpoints


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shadow-root", type=Path, required=True)
    parser.add_argument("--shadow-python", type=Path, required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--expected-patch-sha256", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.shadow_root.resolve()
    revision = _run(["git", "-C", str(root), "rev-parse", "HEAD"], root).strip()
    if revision != args.expected_revision:
        raise RuntimeError(f"shadow revision mismatch: expected {args.expected_revision}, got {revision}")
    patch_sha = _patch_sha(root)
    if patch_sha != args.expected_patch_sha256.lower():
        raise RuntimeError(f"shadow patch mismatch: expected {args.expected_patch_sha256}, got {patch_sha}")
    probe = str(Path(__file__).with_name("shadow_sim_probe.py"))
    results = []
    for name, spec in SCENARIOS.items():
        native = _native_checkpoints(name, spec)
        shadow = json.loads(_run([str(args.shadow_python), probe, "--scenario", name], root))["checkpoints"]
        comparisons = {checkpoint: compare_snapshots(native[checkpoint], shadow[checkpoint]) for checkpoint in native}
        results.append({
            "scenario_id": name,
            "success": all(item["status"] == "python_parity_verified" for item in comparisons.values()),
            "comparisons": comparisons,
        })
    report = {
        "schema_version": 1,
        "success": all(result["success"] for result in results),
        "shadow_revision": revision,
        "shadow_patch_sha256": patch_sha,
        "scenarios": results,
        "scope_warning": "Only the exact recorded scenarios, fields, and action sequences are parity verified.",
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
