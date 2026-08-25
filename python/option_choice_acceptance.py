"""Focused acceptance for a shipped blocking bundle/option choice."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    tools_root = repository_root / ".tools"
    if not tools_root.exists():
        tools_root = repository_root.parent / ".tools"
    godot = next(tools_root.rglob("Godot_v4.5.1-stable_mono_win64_console.exe"))
    host = repository_root / "src" / "Sts2.NativeSim.GodotHost"
    if not host.exists():
        host = repository_root / "native_sim" / "src" / "Sts2.NativeSim.GodotHost"
    if not host.exists():
        host = repository_root.parent / "native_sim" / "src" / "Sts2.NativeSim.GodotHost"
    dotnet_root = tools_root / "dotnet9"
    sys.path.insert(0, str(repository_root / "python"))
    sys.path.insert(0, str(repository_root.parent / "divine-sts2" / "python"))
    from sts2_native_sim.paths import find_game_assembly

    assembly = find_game_assembly()
    environment = os.environ.copy()
    environment["DOTNET_ROOT"] = str(dotnet_root)
    environment["DOTNET_ROOT_X64"] = str(dotnet_root)
    environment["DOTNET_ROLL_FORWARD"] = "Major"
    environment["STS2_ASSEMBLY"] = str(assembly)
    completed = subprocess.run(
        [str(godot), "--headless", "--path", str(host), "--", str(assembly), "--option-choice-acceptance"],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
    )
    output = completed.stdout + completed.stderr
    if completed.returncode != 0:
        raise RuntimeError(f"Native option-choice acceptance failed ({completed.returncode}).\n{output}")
    result = next((json.loads(line) for line in output.splitlines() if line.startswith('{"success"')), None)
    if result != {"success": True, "bundle_choices": 2, "relic_actions": 3, "final_card_count": 13, "reconstructed_card_count": 13}:
        raise AssertionError(f"Unexpected option-choice result: {result!r}\n{output}")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
