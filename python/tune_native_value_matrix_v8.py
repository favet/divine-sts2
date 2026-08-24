"""Validation-only loss recalibration for the breadth v8 native critic corpus."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).parent))
from sts2_native_sim import NativeTorchValueScorer
from train_native_value_matrix import pairwise_gate, ranking_examples


def main() -> None:
    cache = json.loads(Path("artifacts/native-value-matrix-v8-breadth-rollouts.json").read_text(encoding="utf-8"))
    training = [row for key, row in cache.items() if key.startswith("v8-breadth:train:")]
    ranking = [row for key, row in cache.items() if key.startswith("v8-breadth:ranking-train:")]
    validation = [row for key, row in cache.items() if key.startswith("v8-breadth:holdout:")]
    x = torch.tensor([sample for episode in training for sample in episode["samples"]], dtype=torch.float32)
    y = torch.tensor([episode["return"] for episode in training for _ in episode["samples"]], dtype=torch.float32).unsqueeze(1)
    left, right, signs = ranking_examples(ranking)
    positive_weight = max(1.0, float((y < 0.5).sum()) / max(1.0, float((y >= 0.5).sum())))
    weights = torch.where(y >= 0.5, positive_weight, 1.0)
    rows = []
    for pointwise_weight in (0.1, 1.0, 4.0):
        for ranking_weight in (0.1, 0.35, 1.0):
            torch.manual_seed(31)
            model = NativeTorchValueScorer.create_model((128, 64))
            optimizer = torch.optim.AdamW(model.parameters(), lr=0.001, weight_decay=1e-4)
            model.train()
            for _ in range(600):
                optimizer.zero_grad()
                value_loss = ((torch.sigmoid(model(x)) - y).square() * weights).mean()
                rank_loss = torch.nn.functional.softplus(-signs * (model(left) - model(right))).mean()
                loss = pointwise_weight * value_loss + ranking_weight * rank_loss
                loss.backward(); optimizer.step()
            model.eval()
            gate = pairwise_gate(model, validation, minimum_gap=0.02, minimum_pairs=50, threshold=0.90)
            rows.append({"pointwise_weight": pointwise_weight, "ranking_weight": ranking_weight, "accuracy": gate["accuracy"], "correct_pairs": gate["correct_pairs"], "comparable_pairs": gate["comparable_pairs"]})
    rows.sort(key=lambda row: -row["accuracy"])
    print(json.dumps({"success": True, "data_source": "v8 training and validation only", "top": rows}, indent=2))


if __name__ == "__main__":
    main()
