"""Select critic training hyperparameters using cached training and validation only."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).parent))
from sts2_native_sim import NativeTorchValueScorer
from train_native_value_matrix import pairwise_gate, ranking_examples


def main() -> None:
    cache_path = Path("artifacts/native-value-matrix-v5-deep-rollouts.json")
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    training = [row for key, row in cache.items() if key.startswith("v5-deep:train:")]
    ranking = [row for key, row in cache.items() if key.startswith("v5-deep:ranking-train:")]
    validation = [row for key, row in cache.items() if key.startswith("v5-deep:holdout:")]
    if not training or not ranking or not validation:
        raise RuntimeError("v5 deep cache lacks a complete train/ranking/validation split")
    train_x = [sample for episode in training for sample in episode["samples"]]
    train_y = [episode["return"] for episode in training for _ in episode["samples"]]
    x = torch.tensor(train_x, dtype=torch.float32)
    y = torch.tensor(train_y, dtype=torch.float32).unsqueeze(1)
    rank_left, rank_right, rank_sign = ranking_examples(ranking)
    positive_weight = max(1.0, float((y < 0.5).sum()) / max(1.0, float((y >= 0.5).sum())))
    weights = torch.where(y >= 0.5, positive_weight, 1.0)
    rows = []
    for seed in (29, 31, 37):
        for learning_rate in (0.001, 0.003):
            for ranking_weight in (0.35, 1.0, 2.0, 4.0):
                torch.manual_seed(seed)
                model = NativeTorchValueScorer.create_model()
                optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate, weight_decay=1e-4)
                model.train()
                for _ in range(900):
                    optimizer.zero_grad()
                    value_loss = ((torch.sigmoid(model(x)) - y).square() * weights).mean()
                    ranking_loss = torch.nn.functional.softplus(-rank_sign * (model(rank_left) - model(rank_right))).mean()
                    loss = value_loss + ranking_weight * ranking_loss
                    loss.backward()
                    optimizer.step()
                model.eval()
                gate = pairwise_gate(model, validation, minimum_gap=0.02, minimum_pairs=50, threshold=0.90)
                rows.append({
                    "seed": seed,
                    "learning_rate": learning_rate,
                    "ranking_weight": ranking_weight,
                    "accuracy": gate["accuracy"],
                    "correct_pairs": gate["correct_pairs"],
                    "comparable_pairs": gate["comparable_pairs"],
                })
    rows.sort(key=lambda row: (-row["accuracy"], row["seed"], row["learning_rate"], row["ranking_weight"]))
    print(json.dumps({"success": True, "data_source": "v5 training and validation only", "top": rows[:10]}, indent=2))


if __name__ == "__main__":
    main()
