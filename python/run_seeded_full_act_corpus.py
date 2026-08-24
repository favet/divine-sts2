"""Seeded autonomous Act 1 traversal through shipped native mechanics."""
from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from acceptance import SCENARIO
from sts2_native_sim import NativeSimError, NativeWorkerPool


def scenario(seed: str, ascension: int = 0) -> dict:
    state = copy.deepcopy(SCENARIO)
    state.update({
        "seed": seed,
        "ascension": ascension,
        "current_hp": 9999,
        "max_hp": 9999,
        "gold": 999,
        "deck": [{"instance_id": f"anger-{index}", "model_id": "ANGER", "upgrades": 1} for index in range(10)],
        "initial_hand": [],
    })
    return state


def choose_action(state: dict) -> str | None:
    actions = state["legal_actions"]
    if not actions:
        return None
    decision = state["observation"]["decision"]["kind"]
    if decision == "combat_action":
        return next((a["action_id"] for a in actions if a["kind"] == "play_card"), None) or next((a["action_id"] for a in actions if a["kind"] == "end_turn"), actions[0]["action_id"])
    if decision == "card_choice":
        return actions[0]["action_id"]
    if decision == "room_reward_choice":
        return "leave_room_rewards"
    if decision == "shop_choice":
        return "leave_shop"
    if decision == "treasure_open":
        return "open_treasure"
    if decision == "treasure_relic_choice":
        return next(a["action_id"] for a in actions if a["kind"] == "choose_treasure")
    if decision == "treasure_complete":
        return "leave_treasure"
    if decision == "rest_choice":
        return actions[0]["action_id"]
    if decision == "rest_complete":
        return "leave_rest"
    if decision == "event_complete":
        return "leave_event"
    if decision == "custom_reward_choice":
        return next((a["action_id"] for a in actions if a["kind"] == "choose_custom_reward"), actions[-1]["action_id"])
    return actions[0]["action_id"]


def drive(worker, seed: str, step_limit: int = 600, ascension: int = 0) -> dict:
    started = time.perf_counter()
    state = worker.run_reset(scenario(seed, ascension))
    decisions: Counter[str] = Counter()
    rooms: Counter[str] = Counter()
    events: Counter[str] = Counter()
    floors: list[int] = []
    for step in range(step_limit):
        observation = state["observation"]
        decision = observation["decision"]["kind"]
        decisions[decision] += 1
        if decision in {"map_terminal", "run_terminal", "terminal"}:
            victory = bool(observation.get("victory") or state.get("victory"))
            return {
                "seed": seed, "success": decision == "map_terminal" or victory,
                "outcome": "act_complete" if decision == "map_terminal" else "victory" if victory else "death",
                "steps": step, "elapsed_ms": (time.perf_counter() - started) * 1000,
                "final_hash": state["state_hash"], "visited": len((observation.get("map") or {}).get("visited", [])),
                "decisions": dict(decisions), "rooms": dict(rooms), "events": dict(events), "floors": floors,
            }
        if decision == "map_choice":
            action = state["legal_actions"][0]
            rooms[action["parameters"]["point_type"]] += 1
            floors.append(action["parameters"]["row"])
        if "event" in observation and observation["event"]:
            events[observation["event"]["model_id"]] += 1
        action_id = choose_action(state)
        if action_id is None:
            raise RuntimeError(json.dumps({"seed": seed, "step": step, "decision": decision, "observation": observation}, default=str))
        try:
            state = worker.run_step(action_id)
        except NativeSimError as error:
            raise RuntimeError(json.dumps({"seed": seed, "step": step, "decision": decision, "action_id": action_id, "event": observation.get("event"), "details": error.details}, default=str)) from error
    raise RuntimeError(f"seed {seed} exceeded {step_limit} native decisions")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", type=int, default=4)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--repeat", action="store_true", help="run the starting seed independently in every worker")
    parser.add_argument("--step-limit", type=int, default=2000)
    parser.add_argument("--ascension", type=int, default=0, choices=range(0, 11))
    args = parser.parse_args()
    seeds = [f"NATIVE-FULL-ACT-{args.start if args.repeat else index}" for index in range(args.start, args.start + args.seeds)]
    reports = []
    for offset in range(0, len(seeds), 4):
        batch = seeds[offset:offset + 4]
        with NativeWorkerPool(len(batch)) as pool:
            reports.extend(pool.map(lambda worker, seed: drive(worker, seed, args.step_limit, args.ascension), batch))
    deterministic = not args.repeat or len({report["final_hash"] for report in reports}) == 1
    print(json.dumps({
        "success": all(report["success"] for report in reports) and deterministic,
        "deterministic": deterministic,
        "seed_count": len(reports),
        "total_native_decisions": sum(report["steps"] for report in reports),
        "reports": reports,
    }, indent=2))


if __name__ == "__main__":
    main()
