"""Build and train a larger value corpus using only shipped-native terminal rollouts."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import torch
import torch.nn as nn

sys.path.insert(0, str(Path(__file__).parent))
from sts2_native_sim import NativeSimError, NativeTorchValueScorer, NativeWorkerPool, encode_scoring_features


def profile_state(encounter: str, profile: str, seed: str) -> dict:
    starter = ([{"instance_id": f"strike-{index}", "model_id": "STRIKE_IRONCLAD"} for index in range(5)] +
               [{"instance_id": f"defend-{index}", "model_id": "DEFEND_IRONCLAD"} for index in range(5)])
    if profile == "bludgeon":
        deck = [{"instance_id": f"bludgeon-{index}", "model_id": "BLUDGEON", "upgrades": 1} for index in range(10)]
        initial, hp, energy = [card["instance_id"] for card in deck], 80, 99
    elif profile == "anger":
        deck = [{"instance_id": f"anger-{index}", "model_id": "ANGER", "upgrades": 1} for index in range(10)]
        initial, hp, energy = [card["instance_id"] for card in deck], 80, 3
    elif profile == "starter":
        deck, initial, hp, energy = starter, ["strike-0", "strike-1", "strike-2", "defend-0", "defend-1"], 80, 3
    elif profile == "doomed":
        deck, initial, hp, energy = starter, ["strike-0"], 1, 0
    else:
        raise ValueError(profile)
    return {
        "game_build": {}, "seed": seed, "rng_counters": {}, "character": "IRONCLAD", "ascension": 0,
        "encounter": encounter, "current_hp": hp, "max_hp": 80, "gold": 99,
        "deck": deck, "initial_hand": initial, "relics": [], "potions": [], "energy": energy,
    }


def run_episode(worker, task: dict) -> dict:
    state = worker.reset(profile_state(task["encounter"], task["profile"], task["seed"]))
    samples = []
    for step in range(120):
        if state["terminated"]:
            if not samples:
                raise RuntimeError(f"episode terminated without a pre-terminal state: {task}")
            stride = max(1, len(samples) // 12)
            retained = samples[::stride][-12:]
            return {
                **task,
                "target": 1.0 if state["victory"] else 0.0,
                "steps": step,
                "terminal_hash": state["state_hash"],
                "samples": retained,
            }
        samples.append(encode_scoring_features(state))
        if task["profile"] == "doomed":
            action_id = "end_turn"
        else:
            action_id = next((action["action_id"] for action in state["legal_actions"] if action["kind"] == "play_card"), None)
            action_id = action_id or next((action["action_id"] for action in state["legal_actions"] if action["kind"] == "choose_cards"), None)
            action_id = action_id or "end_turn"
        try:
            state = worker.step(action_id)
        except NativeSimError as error:
            raise RuntimeError(json.dumps({"task": task, "step": step, "action_id": action_id, "details": error.details, "diagnostics": worker.diagnostics()}, default=str)) from error
    raise RuntimeError(f"native episode exceeded 120 decisions: {task}")


def evenly_spaced(values: list[str], count: int) -> list[str]:
    if count >= len(values):
        return values
    return [values[round(index * (len(values) - 1) / (count - 1))] for index in range(count)]


def metrics(model, episodes: list[dict]) -> dict:
    rows = []
    with torch.no_grad():
        for episode in episodes:
            tensor = torch.tensor(episode["samples"], dtype=torch.float32)
            probability = float(torch.sigmoid(model(tensor)).mean().item())
            rows.append({"encounter": episode["encounter"], "profile": episode["profile"], "seed": episode["seed"], "target": episode["target"], "probability": probability})
    correct = sum((row["probability"] >= 0.5) == (row["target"] >= 0.5) for row in rows)
    return {"episodes": len(rows), "accuracy": correct / max(1, len(rows)), "mean_probability": sum(row["probability"] for row in rows) / max(1, len(rows)), "rows": rows}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--encounters", type=int, default=12)
    parser.add_argument("--output", type=Path, default=Path("artifacts/native-value-corpus.pt"))
    args = parser.parse_args()
    torch.manual_seed(17)
    with NativeWorkerPool(4) as pool:
        catalog = pool.workers[0].catalog()
        eligible = [entry["model_id"] for entry in catalog["encounters"] if "TEST" not in entry["model_id"] and "DEBUG" not in entry["model_id"]]
        encounters = evenly_spaced(eligible, args.encounters)
        held_out_encounters = set(encounters[::4])
        train_encounters = [encounter for encounter in encounters if encounter not in held_out_encounters]
        tasks = []
        for encounter in train_encounters:
            tasks.extend({"split": "train", "encounter": encounter, "profile": profile, "seed": f"NATIVE-TRAIN-{encounter}-{profile}"} for profile in ("bludgeon", "starter", "doomed"))
            tasks.append({"split": "deck_holdout", "encounter": encounter, "profile": "anger", "seed": f"NATIVE-DECK-{encounter}"})
            tasks.extend({"split": "seed_holdout", "encounter": encounter, "profile": profile, "seed": f"NATIVE-SEED-HOLDOUT-{encounter}-{profile}"} for profile in ("bludgeon", "starter", "doomed"))
        for encounter in sorted(held_out_encounters):
            tasks.extend({"split": "encounter_holdout", "encounter": encounter, "profile": profile, "seed": f"NATIVE-ENCOUNTER-HOLDOUT-{encounter}-{profile}"} for profile in ("bludgeon", "starter", "doomed"))

        episodes = []
        for offset in range(0, len(tasks), 4):
            batch = tasks[offset:offset + 4]
            episodes.extend(pool.map(run_episode, batch))
        build = pool.workers[0].build

    training = [episode for episode in episodes if episode["split"] == "train"]
    train_x = [sample for episode in training for sample in episode["samples"]]
    train_y = [episode["target"] for episode in training for _ in episode["samples"]]
    x = torch.tensor(train_x, dtype=torch.float32)
    y = torch.tensor(train_y, dtype=torch.float32).unsqueeze(1)
    model = NativeTorchValueScorer.create_model()
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.005, weight_decay=1e-4)
    loss_fn = nn.BCEWithLogitsLoss()
    model.train()
    for _ in range(500):
        optimizer.zero_grad(); loss = loss_fn(model(x), y); loss.backward(); optimizer.step()
    model.eval()
    evaluations = {split: metrics(model, [episode for episode in episodes if episode["split"] == split]) for split in ("train", "encounter_holdout", "deck_holdout", "seed_holdout")}
    metadata = {
        "game_build": build,
        "intended_use": "native_corpus_candidate_not_promoted",
        "certifying": False,
        "label_definition": "eventual isolated-combat victory under fixed profile policies, executed entirely by shipped native mechanics",
        "encounters": encounters,
        "held_out_encounters": sorted(held_out_encounters),
        "profiles": ["bludgeon", "starter", "doomed", "anger"],
        "episode_count": len(episodes),
        "training_samples": len(train_x),
        "evaluations": {split: {key: value for key, value in report.items() if key != "rows"} for split, report in evaluations.items()},
        "torch_seed": 17,
    }
    NativeTorchValueScorer.save(args.output, model, metadata)
    print(json.dumps({
        "success": True,
        "output": str(args.output.resolve()),
        "game_build": build,
        "encounters": encounters,
        "held_out_encounters": sorted(held_out_encounters),
        "episode_count": len(episodes),
        "training_samples": len(train_x),
        "outcomes": {"victories": sum(episode["target"] == 1.0 for episode in episodes), "losses": sum(episode["target"] == 0.0 for episode in episodes)},
        "evaluations": evaluations,
    }, indent=2))


if __name__ == "__main__":
    main()
