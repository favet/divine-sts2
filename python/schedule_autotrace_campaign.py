"""
STS2 Automated AutoTrace Campaign Scheduler.
Schedules diverse seeds across all 5 characters and multiple combats to discover
unseen encounters, executes isolated AutoTrace captures with exact differential replay,
and promotes new certified traces into artifacts/shipped-autotraces/certified/.
"""

import os
import sys
import json
import glob
import time
import random
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Set

REPO_ROOT = Path(__file__).resolve().parents[1]
AUTOTRACE_SCRIPT = REPO_ROOT / "scripts" / "run-isolated-autotrace.ps1"
CERTIFIED_DIR = REPO_ROOT / "artifacts" / "shipped-autotraces" / "certified"

ALL_CHARACTERS = ["IRONCLAD", "SILENT", "DEFECT", "NECROBINDER", "REGENT"]


def get_certified_encounters() -> Set[str]:
    """Inspects currently certified traces and extracts distinct encounter IDs."""
    encounters = set()
    for p in CERTIFIED_DIR.glob("*.jsonl"):
        name = p.name
        # format: combat-TIMESTAMP-ENCOUNTER.jsonl
        parts = name.replace(".jsonl", "").split("-")
        if len(parts) >= 3:
            encounters.add("-".join(parts[2:]))
    return encounters


def generate_candidate_seeds(count: int = 10) -> List[Dict[str, Any]]:
    """Generates distinct candidate run configurations using the game's valid seed charset."""
    alphabet = "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # Excludes I and O
    candidates = []
    for i in range(count):
        char = ALL_CHARACTERS[i % len(ALL_CHARACTERS)]
        rng = random.Random(f"AUTOTRACE_CAMPAIGN_BATCH_V2_{i}_{char}_{int(time.time())}")
        seed_str = "".join(rng.choice(alphabet) for _ in range(10))
        combats = 5  # 5 combats deep per run to reach Act 1 elites and late hallway encounters
        candidates.append({
            "seed": seed_str,
            "character": char,
            "combats": combats,
            "policy": "coverage"
        })
    return candidates


def run_campaign(count: int = 5):
    print("=" * 80)
    print("STS2 AUTOMATED AUTOTRACE CAMPAIGN SCHEDULER")
    print("=" * 80)

    initial_encounters = get_certified_encounters()
    print(f"Currently certified encounters ({len(initial_encounters)}):")
    for enc in sorted(initial_encounters):
        print(f"  - {enc}")

    candidates = generate_candidate_seeds(count=count)
    print(f"\nScheduled {len(candidates)} isolated autotrace runs...")

    for idx, c in enumerate(candidates, 1):
        seed = c["seed"]
        combats = c["combats"]
        policy = c["policy"]
        print(f"\n[{idx}/{len(candidates)}] Running isolated AutoTrace: Seed={seed}, Combats={combats}, Policy={policy}...")

        cmd = [
            "powershell",
            "-ExecutionPolicy", "Bypass",
            "-File", str(AUTOTRACE_SCRIPT),
            "-Seed", seed,
            "-CombatCount", str(combats),
            "-Policy", policy
        ]

        try:
            res = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=300)
            for line in res.stdout.splitlines():
                if "certified_trace_count" in line or "checkpoints" in line or "final_hash" in line or "combat-" in line:
                    print(f"    {line.strip()}")
            if res.returncode != 0 and res.stderr:
                print(f"    Error: {res.stderr[:200]}")
        except subprocess.TimeoutExpired:
            print("    Run timed out after 300s.")
        except Exception as e:
            print(f"    Exception: {e}")

    updated_encounters = get_certified_encounters()
    new_encounters = updated_encounters - initial_encounters
    total_traces = len(list(CERTIFIED_DIR.glob("*.jsonl")))

    print("\n" + "=" * 80)
    print("CAMPAIGN EXECUTION COMPLETE:")
    print(f"  - Total Certified Traces in Inventory: {total_traces}")
    print(f"  - Total Certified Encounters: {len(updated_encounters)}")
    if new_encounters:
        print(f"  - Newly Certified Encounters ({len(new_encounters)}):")
        for enc in sorted(new_encounters):
            print(f"    + {enc}")
    else:
        print("  - No new encounter types discovered in this batch (semantic deduplication applied).")
    print("=" * 80)


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    run_campaign(count=n)
