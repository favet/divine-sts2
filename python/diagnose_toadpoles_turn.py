"""Compact reproducer for the low-HP Toadpoles native turn boundary."""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sts2_native_sim import NativeSimError, NativeWorker
from train_native_value_matrix import choose_action, profile_spec, stable_int, validate_archetypes


def main() -> None:
    root_id = "v8-breadth:ranking-train:TOADPOLES_WEAK:strength:35:0.15:depth-0"
    identity = f"{root_id}:play:skill-defend_ironclad-2:none:heuristic:1"
    with NativeWorker() as worker:
        cards = validate_archetypes(worker.catalog()["cards"])
        spec = profile_spec("TOADPOLES_WEAK", "strength", 35, 0.15, "NATIVE-MATRIX-v8-breadth:ranking-train:TOADPOLES_WEAK:strength:35:0.15", cards)
        state = worker.reset(spec)
        state = worker.step("play:skill-defend_ironclad-2:none")
        rng = random.Random(stable_int(identity))
        rows = []
        for step in range(20):
            rows.append({"step": step, "phase": state["observation"]["combat"]["phase"], "hp": state["scoring_features"]["current_hp"], "actions": [action["action_id"] for action in state["legal_actions"]]})
            if state["terminated"]:
                break
            action_id = choose_action(state, "heuristic", rng)
            try:
                state = worker.step(action_id)
            except NativeSimError as error:
                print(json.dumps({"success": False, "action_id": action_id, "error": error.code, "details": error.details, "rows": rows}, indent=2))
                return
        print(json.dumps({"success": True, "terminal": state["terminated"], "rows": rows}, indent=2))


if __name__ == "__main__":
    main()
