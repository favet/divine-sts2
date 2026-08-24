"""Train a smoke-only value checkpoint from shipped-native terminal combats."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import torch
import torch.nn as nn

sys.path.insert(0, str(Path(__file__).parent))
from sts2_native_sim import NativeTorchValueScorer, NativeWorker, encode_scoring_features


ENCOUNTERS = ("NIBBITS_WEAK", "BOWLBUGS_WEAK", "FOGMOG_NORMAL", "LIVING_FOG_NORMAL")


def reset_state(seed: str, encounter: str, strong: bool) -> dict:
    if strong:
        deck = [{"instance_id": f"bludgeon-{index}", "model_id": "BLUDGEON", "upgrades": 1} for index in range(10)]
        initial = [card["instance_id"] for card in deck]
        hp, energy = 80, 99
    else:
        deck = ([{"instance_id": f"strike-{index}", "model_id": "STRIKE_IRONCLAD"} for index in range(5)] +
                [{"instance_id": f"defend-{index}", "model_id": "DEFEND_IRONCLAD"} for index in range(5)])
        initial = ["strike-0"]
        hp, energy = 1, 0
    return {
        "game_build": {}, "seed": seed, "rng_counters": {}, "character": "IRONCLAD", "ascension": 0,
        "encounter": encounter, "current_hp": hp, "max_hp": 80, "gold": 99,
        "deck": deck, "initial_hand": initial, "relics": [], "potions": [], "energy": energy,
    }


def episode(worker: NativeWorker, encounter: str, strong: bool, ordinal: int) -> tuple[list[list[float]], float, str]:
    name = f"{'WIN' if strong else 'LOSS'}-{encounter}-{ordinal}"
    state = worker.reset(reset_state(name, encounter, strong))
    encoded = []
    for _ in range(80):
        if state["terminated"]:
            target = 1.0 if state["victory"] else 0.0
            if strong and target != 1.0:
                raise RuntimeError(f"strong native rollout did not win: {name}")
            if not strong and target != 0.0:
                raise RuntimeError(f"weak native rollout did not lose: {name}")
            return encoded, target, state["state_hash"]
        encoded.append(encode_scoring_features(state))
        if strong:
            action_id = next((action["action_id"] for action in state["legal_actions"] if action["kind"] == "play_card"), "end_turn")
        else:
            action_id = "end_turn"
        state = worker.step(action_id)
    raise RuntimeError(f"native rollout did not terminate: {name}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("artifacts/native-value-smoke.pt"))
    args = parser.parse_args()
    torch.manual_seed(7)
    train_x, train_y, validation = [], [], []
    records = []
    with NativeWorker() as worker:
        build = worker.build
        for ordinal, encounter in enumerate(ENCOUNTERS):
            for strong in (True, False):
                samples, target, terminal_hash = episode(worker, encounter, strong, ordinal)
                record = {"encounter": encounter, "strong": strong, "samples": len(samples), "target": target, "terminal_hash": terminal_hash}
                records.append(record)
                destination = validation if ordinal == len(ENCOUNTERS) - 1 else None
                if destination is not None:
                    destination.extend((sample, target) for sample in samples)
                else:
                    train_x.extend(samples); train_y.extend([target] * len(samples))

    x = torch.tensor(train_x, dtype=torch.float32)
    y = torch.tensor(train_y, dtype=torch.float32).unsqueeze(1)
    model = NativeTorchValueScorer.create_model()
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.01, weight_decay=1e-4)
    loss_fn = nn.BCEWithLogitsLoss()
    model.train()
    for _ in range(300):
        optimizer.zero_grad()
        loss = loss_fn(model(x), y)
        loss.backward()
        optimizer.step()
    model.eval()
    val_x = torch.tensor([sample for sample, _ in validation], dtype=torch.float32)
    val_y = torch.tensor([target for _, target in validation], dtype=torch.float32)
    with torch.no_grad():
        probabilities = torch.sigmoid(model(val_x)).flatten()
    accuracy = float(((probabilities >= 0.5) == (val_y >= 0.5)).float().mean().item())
    metadata = {
        "game_build": build,
        "intended_use": "integration_smoke_only",
        "certifying": False,
        "label_definition": "eventual isolated-combat victory under a fixed policy, executed entirely by shipped native mechanics",
        "training_episodes": records[:-2],
        "validation_episodes": records[-2:],
        "training_samples": len(train_x),
        "validation_samples": len(validation),
        "validation_accuracy": accuracy,
        "torch_seed": 7,
    }
    NativeTorchValueScorer.save(args.output, model, metadata)
    loaded = NativeTorchValueScorer.load(args.output, build, allow_unpromoted=True)
    with torch.no_grad():
        loaded_values = torch.sigmoid(loaded.model(val_x)).flatten()
    assert torch.equal(probabilities, loaded_values)
    print(json.dumps({
        "success": True,
        "output": str(args.output.resolve()),
        "game_build": build,
        "training_samples": len(train_x),
        "validation_samples": len(validation),
        "validation_accuracy": accuracy,
        "episodes": records,
    }, indent=2))


if __name__ == "__main__":
    main()
