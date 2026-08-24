"""Strictly replay quarantined candidates and atomically copy only exact passes."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from differential_replay import replay


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def promote(candidate: Path, certified_directory: Path, failure_directory: Path) -> dict[str, object]:
    report = replay(candidate, require_exact=True)
    diagnostic = failure_directory / f"{candidate.stem}.replay.json"
    if not report.get("success") or not report.get("certifying") or report.get("validation_comparison") != "exact":
        atomic_json(diagnostic, report)
        return {"candidate": str(candidate), "promoted": False, "diagnostic": str(diagnostic), "replay": report}

    destination = certified_directory / candidate.name
    if destination.exists():
        if digest(destination) != digest(candidate):
            raise RuntimeError(f"Certified name collision with different content: {destination}")
    else:
        temporary = destination.with_name(f"{destination.name}.tmp-{os.getpid()}")
        shutil.copyfile(candidate, temporary)
        os.replace(temporary, destination)
    diagnostic.unlink(missing_ok=True)
    return {"candidate": str(candidate), "promoted": True, "certified": str(destination), "replay": report}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("candidates", nargs="+", type=Path)
    parser.add_argument("--certified-directory", type=Path, required=True)
    parser.add_argument("--failure-directory", type=Path, required=True)
    args = parser.parse_args()
    args.certified_directory.mkdir(parents=True, exist_ok=True)
    args.failure_directory.mkdir(parents=True, exist_ok=True)
    candidates = sorted(
        {trace.resolve() for value in args.candidates for trace in ([value] if value.is_file() else value.glob("*.jsonl"))},
        key=lambda path: str(path).lower(),
    )
    results = [promote(candidate, args.certified_directory.resolve(), args.failure_directory.resolve()) for candidate in candidates]
    output = {"success": all(result["promoted"] for result in results), "results": results}
    print(json.dumps(output, indent=2))
    return 0 if output["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
