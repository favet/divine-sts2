"""Command-line entry point for setup validation and native workers."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .paths import DiscoveryError, REPOSITORY_ROOT, find_game_assembly, find_game_root, find_godot

SUPPORTED_BUILD = {
    "assembly_sha256": "A1F9E653F1E28E4076558FEE1E60D218619CB7E057B887C6417F62C62C6D7A52",
    "pck_sha256": "42520EB8B0911C6C0F0BD102D92B33F41ABD4D26B83489817D0A6DBD7DD48587",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def doctor(deep: bool = False) -> dict[str, Any]:
    bundled_dotnet = REPOSITORY_ROOT / ".tools" / "dotnet9" / "dotnet.exe"
    dotnet = str(bundled_dotnet) if bundled_dotnet.is_file() else shutil.which("dotnet")
    dotnet_sdks: list[str] = []
    if dotnet:
        try:
            result = subprocess.run([dotnet, "--list-sdks"], capture_output=True, text=True, timeout=10, check=False)
            if result.returncode == 0:
                dotnet_sdks = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        except (OSError, subprocess.TimeoutExpired):
            pass
    checks: dict[str, Any] = {
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "repository_root": str(REPOSITORY_ROOT),
        "dotnet": dotnet,
        "dotnet_sdks": dotnet_sdks,
    }
    try:
        import torch

        checks["torch"] = torch.__version__
        checks["cuda_available"] = torch.cuda.is_available()
        checks["cuda_device"] = torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
    except ImportError:
        checks["torch"] = None
        checks["cuda_available"] = False
        checks["cuda_device"] = None
    failures: list[str] = []
    for name, discover in (("game_root", find_game_root), ("game_assembly", find_game_assembly), ("godot", find_godot)):
        try:
            checks[name] = str(discover())
        except DiscoveryError as error:
            checks[name] = None
            failures.append(str(error))

    if deep and not failures:
        assembly = Path(checks["game_assembly"])
        game_root = Path(checks["game_root"])
        checks["sts2_assembly_sha256"] = _sha256(assembly)
        checks["sts2_pck_sha256"] = _sha256(game_root / "SlayTheSpire2.pck")
        for key, expected in SUPPORTED_BUILD.items():
            actual = checks["sts2_" + key]
            if actual != expected:
                failures.append(f"Unsupported game build: {key}={actual}; expected {expected}.")
        try:
            from .client import NativeWorker

            with NativeWorker() as worker:
                checks["worker_hello"] = worker.hello()
                checks["worker_catalog_counts"] = {
                    key: len(value) for key, value in worker.catalog().items() if isinstance(value, list)
                }
        except Exception as error:  # The report should retain every earlier check.
            details = getattr(error, "details", None)
            suffix = f" details={details}" if details else ""
            failures.append(f"Native worker smoke failed: {error}{suffix}")

    has_dotnet_9 = any(version.startswith("9.") for version in dotnet_sdks)
    checks["ok"] = not failures and has_dotnet_9
    checks["failures"] = failures + ([] if has_dotnet_9 else [".NET 9 SDK was not found; run scripts/bootstrap.ps1."])
    checks["ok"] = not checks["failures"]
    return checks


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="divine-sts2")
    subcommands = parser.add_subparsers(dest="command", required=True)
    doctor_parser = subcommands.add_parser("doctor", help="validate the local game/tool installation")
    doctor_parser.add_argument("--deep", action="store_true", help="hash game files and start a native worker")
    doctor_parser.add_argument("--json", action="store_true", help="emit compact JSON")
    args = parser.parse_args(argv)

    if args.command == "doctor":
        report = doctor(args.deep)
        print(json.dumps(report, indent=None if args.json else 2, default=str))
        raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
