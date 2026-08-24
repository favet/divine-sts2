"""Normalized transition comparison for non-authoritative shadow simulators."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable


PROVENANCE_NATIVE = "shipped_native"
PROVENANCE_SHADOW_VERIFIED = "python_parity_verified"
PROVENANCE_SHADOW_UNVERIFIED = "python_unverified"


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten JSON-compatible values into stable leaf paths."""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key in sorted(value):
            child = f"{prefix}.{key}" if prefix else str(key)
            result.update(_flatten(value[key], child))
        return result
    if isinstance(value, list):
        result = {}
        for index, item in enumerate(value):
            child = f"{prefix}[{index}]"
            result.update(_flatten(item, child))
        if not value:
            result[prefix] = []
        return result
    return {prefix: value}


def compare_snapshots(native: dict[str, Any], shadow: dict[str, Any]) -> dict[str, Any]:
    """Compare two normalized snapshots exactly and retain every mismatch."""
    native_fields, shadow_fields = _flatten(native), _flatten(shadow)
    paths = sorted(set(native_fields) | set(shadow_fields))
    comparisons = []
    for path in paths:
        native_present, shadow_present = path in native_fields, path in shadow_fields
        native_value, shadow_value = native_fields.get(path), shadow_fields.get(path)
        matches = native_present and shadow_present and native_value == shadow_value
        comparisons.append({
            "path": path,
            "matches": matches,
            "native_present": native_present,
            "shadow_present": shadow_present,
            "native": native_value,
            "shadow": shadow_value,
        })
    matched = sum(item["matches"] for item in comparisons)
    return {
        "status": PROVENANCE_SHADOW_VERIFIED if comparisons and matched == len(comparisons) else "mismatch",
        "matched_fields": matched,
        "total_fields": len(comparisons),
        "mismatches": [item for item in comparisons if not item["matches"]],
    }


def build_trust_matrix(scenarios: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate scenario checkpoint comparisons into a per-mechanic matrix."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for scenario in scenarios:
        mechanic_comparisons = scenario.get("mechanic_comparisons")
        if mechanic_comparisons:
            for mechanic, comparison in mechanic_comparisons.items():
                grouped[mechanic].append({"comparisons": {mechanic: comparison}})
        else:
            for mechanic in scenario.get("mechanics", []):
                grouped[mechanic].append(scenario)

    matrix = []
    for mechanic in sorted(grouped):
        entries = grouped[mechanic]
        checkpoints = [comparison for entry in entries for comparison in entry.get("comparisons", {}).values()]
        total = sum(item["total_fields"] for item in checkpoints)
        matched = sum(item["matched_fields"] for item in checkpoints)
        verified = bool(checkpoints) and matched == total
        matrix.append({
            "mechanic": mechanic,
            "status": PROVENANCE_SHADOW_VERIFIED if verified else PROVENANCE_SHADOW_UNVERIFIED,
            "scenario_count": len(entries),
            "checkpoint_count": len(checkpoints),
            "matched_fields": matched,
            "total_fields": total,
            "eligible_as_authoritative_label_source": False,
        })
    return matrix
