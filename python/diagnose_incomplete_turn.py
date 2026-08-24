"""Regression for terminal player death in a summon-capable native combat."""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sts2_native_sim import NativeSimError, NativeWorker
from train_native_value_matrix import choose_action, profile_spec, stable_int, validate_archetypes


def main() -> None:
    encounter, archetype, deck_size, hp_ratio, policy = "SPINY_TOAD_NORMAL", "summon", 35, 0.15, "greedy"
    identity = f"v8-breadth:train:{encounter}:{archetype}:{deck_size}:{hp_ratio}:{policy}"
    with NativeWorker() as worker:
        cards = validate_archetypes(worker.catalog()["cards"])
        spec = profile_spec(encounter, archetype, deck_size, hp_ratio, f"NATIVE-MATRIX-{identity.rsplit(':', 1)[0]}", cards)
        state = worker.reset(spec)
        rng = random.Random(stable_int(identity))
        rows = []
        for step in range(20):
            rows.append({"step": step, "phase": state["observation"]["combat"]["phase"], "hp": state["scoring_features"]["current_hp"], "actions": [action["action_id"] for action in state["legal_actions"]]})
            if state["terminated"]:
                assert state["victory"] is False
                assert state["scoring_features"]["current_hp"] <= 0
                break
            action_id = choose_action(state, policy, rng)
            try:
                state = worker.step(action_id)
            except NativeSimError as error:
                observed = worker.observe()
                print(json.dumps({"success": False, "action_id": action_id, "error": error.code, "details": error.details, "rows": rows, "post_error": {"phase": observed["observation"]["combat"]["phase"], "terminal": observed["terminated"], "victory": observed["victory"], "creatures": observed["observation"]["combat"]["creatures"]}}, indent=2))
                return
        assert state["terminated"]
        print(json.dumps({"success": True, "terminal_loss": True, "rows": rows}, indent=2))


if __name__ == "__main__":
    main()
