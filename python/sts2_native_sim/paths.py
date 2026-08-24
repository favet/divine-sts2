"""Portable discovery for user-owned game and tool installations."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
GAME_DIRECTORY_NAME = "Slay the Spire 2"
GAME_DATA_DIRECTORY_NAME = "data_sts2_windows_x86_64"


class DiscoveryError(FileNotFoundError):
    """Raised when a required local dependency cannot be discovered."""


def _steam_roots() -> list[Path]:
    candidates: list[Path] = []
    for variable in ("PROGRAMFILES(X86)", "PROGRAMFILES"):
        base = os.environ.get(variable)
        if base:
            candidates.append(Path(base) / "Steam")
    steam_path = os.environ.get("STEAM_PATH")
    if steam_path:
        candidates.insert(0, Path(steam_path))

    roots: list[Path] = []
    for steam in candidates:
        roots.append(steam)
        manifest = steam / "steamapps" / "libraryfolders.vdf"
        if not manifest.is_file():
            continue
        try:
            text = manifest.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for value in re.findall(r'"path"\s+"([^"]+)"', text):
            roots.append(Path(value.replace("\\\\", "\\")))
    return list(dict.fromkeys(path.resolve() for path in roots if path.exists()))


def find_game_root(explicit: str | Path | None = None) -> Path:
    override = explicit or os.environ.get("STS2_GAME_ROOT")
    if override:
        candidate = Path(override).expanduser().resolve()
        if (
            (candidate / "SlayTheSpire2.exe").is_file()
            and (candidate / "SlayTheSpire2.pck").is_file()
            and (candidate / GAME_DATA_DIRECTORY_NAME / "sts2.dll").is_file()
        ):
            return candidate
        raise DiscoveryError(f"Configured STS2_GAME_ROOT is not a complete game install: {candidate}")

    candidates: list[Path] = []
    candidates.extend(root / "steamapps" / "common" / GAME_DIRECTORY_NAME for root in _steam_roots())
    for candidate in candidates:
        candidate = candidate.resolve()
        if (
            (candidate / "SlayTheSpire2.exe").is_file()
            and (candidate / "SlayTheSpire2.pck").is_file()
            and (candidate / GAME_DATA_DIRECTORY_NAME / "sts2.dll").is_file()
        ):
            return candidate
    searched = ", ".join(str(path) for path in candidates) or "standard Steam libraries"
    raise DiscoveryError(
        "Slay the Spire 2 was not found. Set STS2_GAME_ROOT to the installed game directory. "
        f"Searched: {searched}"
    )


def find_game_assembly(explicit: str | Path | None = None) -> Path:
    override = explicit or os.environ.get("STS2_ASSEMBLY")
    candidate = Path(override).expanduser().resolve() if override else find_game_root() / GAME_DATA_DIRECTORY_NAME / "sts2.dll"
    if not candidate.is_file():
        raise DiscoveryError(f"STS2 assembly not found: {candidate}")
    return candidate


def find_godot(explicit: str | Path | None = None) -> Path:
    override = explicit or os.environ.get("GODOT")
    if override:
        candidate = Path(override).expanduser().resolve()
        if candidate.is_file():
            return candidate
        raise DiscoveryError(f"Godot executable not found: {candidate}")

    tool_root = REPOSITORY_ROOT / ".tools" / "godot-4.5.1-mono"
    names = (
        "Godot_v4.5.1-stable_mono_win64_console.exe",
        "Godot_v4.5.1-stable_mono_win64.exe",
    )
    if tool_root.exists():
        for name in names:
            match = next(tool_root.rglob(name), None)
            if match:
                return match.resolve()
    for name in ("godot", "godot4", *names):
        resolved = shutil.which(name)
        if resolved:
            return Path(resolved).resolve()
    raise DiscoveryError("Godot 4.5.1 .NET was not found. Run scripts/bootstrap.ps1 or set GODOT.")


def default_sandbox_root() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())
    return base / "divine-sts2" / "full-app-sandboxes"
