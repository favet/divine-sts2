"""Unit acceptance for provenance and exact shadow parity aggregation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sts2_native_sim.parity import build_trust_matrix, compare_snapshots


def main() -> None:
    native = {"player": {"hp": 80}, "piles": {"hand": ["STRIKE_IRONCLAD"]}}
    exact = compare_snapshots(native, dict(native))
    assert exact["status"] == "python_parity_verified" and exact["matched_fields"] == exact["total_fields"]
    mismatch = compare_snapshots(native, {"player": {"hp": 79}, "piles": {"hand": ["STRIKE_IRONCLAD"]}})
    assert mismatch["status"] == "mismatch"
    assert mismatch["mismatches"] == [{
        "path": "player.hp", "matches": False, "native_present": True, "shadow_present": True, "native": 80, "shadow": 79,
    }]
    matrix = build_trust_matrix([
        {"mechanic_comparisons": {"ordinary_attack": exact}},
        {"mechanic_comparisons": {"complete_turn": mismatch}},
    ])
    by_name = {item["mechanic"]: item for item in matrix}
    assert by_name["ordinary_attack"]["status"] == "python_parity_verified"
    assert by_name["complete_turn"]["status"] == "python_unverified"
    assert not any(item["eligible_as_authoritative_label_source"] for item in matrix)
    print(json.dumps({"success": True, "matrix": matrix}, indent=2))


if __name__ == "__main__":
    main()
