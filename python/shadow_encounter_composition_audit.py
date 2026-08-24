"""Audit observed encounter-composition support against the shipped simulator.

This is deliberately weaker than transition parity: matching observed signature
sets does not certify seed mapping, probabilities, HP rolls, AI, or mechanics.
It is useful for finding impossible or missing encounter compositions cheaply.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from sts2_native_sim import NativeSimError, NativeWorker


BASE_SCENARIO = {
    "game_build": {},
    "rng_counters": {},
    "character": "IRONCLAD",
    "ascension": 0,
    "current_hp": 80,
    "max_hp": 80,
    "gold": 0,
    "deck": [{"instance_id": "strike-0", "model_id": "STRIKE_IRONCLAD"}],
    "initial_hand": ["strike-0"],
    "relics": [],
    "potions": [],
}

DECOMPILED_COMPOSITION_RULE_MATCHES = {
    "RUBY_RAIDERS_NORMAL": "Both implementations select three distinct entries from Axe, Assassin, Brute, Crossbow, and Tracker; finite observed permutation sets differ.",
}


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _patch_sha(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "diff", "HEAD", "--binary", "--no-ext-diff"],
        capture_output=True,
        check=True,
    )
    return hashlib.sha256(result.stdout).hexdigest()


def _native_signature(state: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        creature["model_id"]
        for creature in state["observation"]["combat"]["creatures"]
        if creature["side"] == "Enemy"
    )


def _shadow_signature(combat: Any) -> tuple[str, ...]:
    return tuple(str(creature.monster_id) for creature in combat.enemies)


def _error(error: Exception) -> dict[str, Any]:
    if isinstance(error, NativeSimError):
        return {"type": type(error).__name__, "code": error.code, "message": str(error)}
    return {"type": type(error).__name__, "message": str(error)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shadow-root", type=Path, required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--expected-patch-sha256", required=True)
    parser.add_argument("--seeds", type=int, default=16)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.seeds < 1:
        raise ValueError("seeds must be positive")

    shadow_root = args.shadow_root.resolve()
    output_path = None if args.output is None else args.output.resolve()
    revision = subprocess.run(
        ["git", "-C", str(shadow_root), "rev-parse", "HEAD"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
    if revision != args.expected_revision:
        raise RuntimeError(f"shadow revision mismatch: expected {args.expected_revision}, got {revision}")
    patch_sha = _patch_sha(shadow_root)
    if patch_sha != args.expected_patch_sha256.lower():
        raise RuntimeError(f"shadow patch mismatch: expected {args.expected_patch_sha256}, got {patch_sha}")

    sys.path.insert(0, str(shadow_root))
    os.chdir(shadow_root)
    from sts2_env.cards.base import reset_instance_counter
    from sts2_env.cards.factory import create_card
    from sts2_env.core.combat import CombatState
    from sts2_env.core.enums import CardId
    from sts2_env.core.rng import Rng, deterministic_hash_code

    rows = []
    with NativeWorker() as worker:
        catalog = worker.catalog()
        for encounter in catalog["encounters"]:
            model_id = encounter["model_id"]
            class_name = encounter["runtime_type"].rsplit(".", 1)[-1]
            setup_name = f"setup_{_snake(class_name)}"
            act_indices = encounter.get("act_indices") or []
            preferred_modules = [f"act{index}" for index in act_indices if index in (1, 2, 3, 4)]
            module_candidates = preferred_modules + [
                module for module in ("act1", "act2", "act3", "act4") if module not in preferred_modules
            ]
            shadow_encounter_id = next(
                (
                    f"{module}:{setup_name}"
                    for module in module_candidates
                    if hasattr(__import__(f"sts2_env.encounters.{module}", fromlist=[setup_name]), setup_name)
                ),
                None,
            )
            native_signatures: set[tuple[str, ...]] = set()
            shadow_signatures: set[tuple[str, ...]] = set()
            native_errors: list[dict[str, Any]] = []
            shadow_errors: list[dict[str, Any]] = []
            for seed_index in range(args.seeds):
                seed_text = f"ENCOUNTER-COMPOSITION-{model_id}-{seed_index}"
                try:
                    native_signatures.add(_native_signature(worker.reset({**BASE_SCENARIO, "seed": seed_text, "encounter": model_id})))
                except Exception as error:
                    native_errors.append(_error(error))
                if shadow_encounter_id is not None:
                    try:
                        reset_instance_counter()
                        shadow_seed = deterministic_hash_code(seed_text)
                        combat = CombatState(
                            player_hp=80,
                            player_max_hp=80,
                            deck=[create_card(CardId.STRIKE_IRONCLAD)],
                            rng_seed=shadow_seed,
                            character_id="Ironclad",
                        )
                        module_name, setup_name = shadow_encounter_id.split(":", 1)
                        setup = getattr(
                            __import__(f"sts2_env.encounters.{module_name}", fromlist=[setup_name]),
                            setup_name,
                        )
                        # Composition-support audit only: this intentionally
                        # exercises the raw setup rule with an isolated RNG and
                        # makes no HP, AI, transition, or per-seed parity claim.
                        setup(combat, Rng(shadow_seed))
                        shadow_signatures.add(_shadow_signature(combat))
                    except Exception as error:
                        shadow_errors.append(_error(error))

            native_values = sorted([list(value) for value in native_signatures])
            shadow_values = sorted([list(value) for value in shadow_signatures])
            if shadow_encounter_id is None:
                status = "shadow_setup_missing"
            elif native_errors or shadow_errors:
                status = "execution_incomplete"
            elif native_signatures == shadow_signatures:
                status = "observed_signature_support_match"
            elif model_id in DECOMPILED_COMPOSITION_RULE_MATCHES:
                status = "decompiled_composition_rule_match"
            else:
                status = "observed_signature_support_mismatch"
            rows.append({
                "model_id": model_id,
                "runtime_type": encounter["runtime_type"],
                "shadow_encounter_id": shadow_encounter_id,
                "status": status,
                "native_signatures": native_values,
                "shadow_signatures": shadow_values,
                "native_errors": native_errors[:3],
                "shadow_errors": shadow_errors[:3],
                "decompiled_rule_evidence": DECOMPILED_COMPOSITION_RULE_MATCHES.get(model_id),
            })

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    report = {
        "schema_version": 1,
        "audit_kind": "observed_encounter_signature_support",
        "seeds_per_encounter": args.seeds,
        "shadow_revision": revision,
        "shadow_patch_sha256": patch_sha,
        "counts": counts,
        "encounters": rows,
        "scope_warning": "A match certifies only observed ordered enemy-model signature support, not exact RNG or mechanics parity.",
    }
    encoded = json.dumps(report, indent=2)
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(encoded + "\n", encoding="utf-8")
    print(json.dumps({"counts": counts, "mismatches": [row["model_id"] for row in rows if row["status"] == "observed_signature_support_mismatch"]}, indent=2))


if __name__ == "__main__":
    main()
