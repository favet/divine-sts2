"""Validate scenario definitions and policies across all 5 characters."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "python"))
from sts2_native_sim import NativeWorker

SCENARIOS = [
    {
        "name": "ironclad_hallway",
        "character": "IRONCLAD",
        "encounter": "first",
        "deck": [{"instance_id": f"ic-{i}", "model_id": m} for i, m in enumerate(["STRIKE_IRONCLAD"]*5 + ["DEFEND_IRONCLAD"]*4 + ["BASH"])],
        "relics": [{"model_id": "BURNING_BLOOD"}],
        "potions": [{"model_id": "FIRE_POTION", "slot": 0}],
        "invoke_combat_entry_hooks": True
    },
    {
        "name": "silent_hallway_potions",
        "character": "SILENT",
        "encounter": "NIBBITS_WEAK",
        "deck": [{"instance_id": f"sil-{i}", "model_id": m} for i, m in enumerate(["STRIKE_SILENT"]*5 + ["DEFEND_SILENT"]*5 + ["NEUTRALIZE", "SURVIVOR"])],
        "relics": [{"model_id": "RING_OF_THE_SNAKE"}],
        "potions": [{"model_id": "ENERGY_POTION", "slot": 0}],
        "invoke_combat_entry_hooks": True
    },
    {
        "name": "defect_orbs_multi",
        "character": "DEFECT",
        "encounter": "BOWLBUGS_WEAK",
        "deck": [{"instance_id": f"def-{i}", "model_id": m} for i, m in enumerate(["STRIKE_DEFECT"]*4 + ["DEFEND_DEFECT"]*4 + ["ZAP", "DUALCAST"])],
        "relics": [{"model_id": "CRACKED_CORE"}],
        "potions": [],
        "invoke_combat_entry_hooks": True,
        "capture_orbs": True
    },
    {
        "name": "necrobinder_summon",
        "character": "NECROBINDER",
        "encounter": "CORPSE_SLUGS_WEAK",
        "deck": [{"instance_id": f"necro-{i}", "model_id": m} for i, m in enumerate(["STRIKE_NECROBINDER"]*4 + ["DEFEND_NECROBINDER"]*4 + ["SUMMON_FORTH", "REANIMATE"])],
        "relics": [],
        "potions": [],
        "invoke_combat_entry_hooks": True
    },
    {
        "name": "regent_stars",
        "character": "REGENT",
        "encounter": "SEAPUNK_WEAK",
        "deck": [{"instance_id": f"reg-{i}", "model_id": m} for i, m in enumerate(["STRIKE_REGENT"]*4 + ["DEFEND_REGENT"]*4 + ["SOVEREIGN_BLADE", "CLOAK_OF_STARS"])],
        "relics": [],
        "potions": [],
        "invoke_combat_entry_hooks": True
    },
    {
        "name": "elite_terror_eel",
        "character": "IRONCLAD",
        "encounter": "TERROR_EEL_ELITE",
        "deck": [{"instance_id": f"ic-e-{i}", "model_id": m} for i, m in enumerate(["STRIKE_IRONCLAD"]*5 + ["DEFEND_IRONCLAD"]*4 + ["BASH"])],
        "relics": [{"model_id": "BURNING_BLOOD"}],
        "potions": [],
        "invoke_combat_entry_hooks": True
    },
    {
        "name": "boss_waterfall_giant",
        "character": "SILENT",
        "encounter": "WATERFALL_GIANT_BOSS",
        "deck": [{"instance_id": f"sil-b-{i}", "model_id": m} for i, m in enumerate(["STRIKE_SILENT"]*5 + ["DEFEND_SILENT"]*5 + ["NEUTRALIZE", "SURVIVOR"])],
        "relics": [{"model_id": "RING_OF_THE_SNAKE"}],
        "potions": [],
        "invoke_combat_entry_hooks": True
    },
    {
        "name": "choice_and_generation",
        "character": "IRONCLAD",
        "encounter": "first",
        "deck": [
            {"instance_id": "disc-0", "model_id": "DISCOVERY"},
            {"instance_id": "pur-0", "model_id": "PURITY"},
            {"instance_id": "acro-0", "model_id": "ACROBATICS"}
        ] + [{"instance_id": f"ic-g-{i}", "model_id": "STRIKE_IRONCLAD"} for i in range(4)],
        "relics": [],
        "potions": [],
        "invoke_combat_entry_hooks": True
    }
]

def make_scenario_dict(sc_def, seed="TEST_SEED"):
    return {
        "game_build": {},
        "seed": seed,
        "rng_counters": {},
        "character": sc_def["character"],
        "ascension": 0,
        "encounter": sc_def["encounter"],
        "current_hp": 80,
        "max_hp": 80,
        "gold": 99,
        "deck": sc_def["deck"],
        "initial_hand": [],
        "relics": sc_def["relics"],
        "potions": sc_def["potions"],
        "invoke_combat_entry_hooks": sc_def.get("invoke_combat_entry_hooks", False),
        "capture_orbs": sc_def.get("capture_orbs", False)
    }

def main():
    print("Testing all scenario configurations...")
    with NativeWorker() as worker:
        for sc in SCENARIOS:
            sc_dict = make_scenario_dict(sc, f"SEED_{sc['name']}")
            state = worker.reset(sc_dict)
            legals = len(state.get("legal_actions", []))
            enemies = len([c for c in state.get("observation", {}).get("combat", {}).get("creatures", []) if c.get("side") == "Enemy"])
            print(f"  [OK] {sc['name']}: {enemies} enemies, {legals} legal actions, hash: {state.get('state_hash')[:12]}")
            if legals > 0:
                act = state["legal_actions"][0]["action_id"]
                s1 = worker.step(act)
                print(f"       Step {act[:20]}: {len(s1.get('legal_actions', []))} legals remaining")
    print("All scenarios passed validation successfully!")

if __name__ == "__main__":
    main()
