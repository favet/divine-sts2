"""Validation-only structural ablation for the robust v7 native critic corpus."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).parent))
from sts2_native_sim import FEATURE_NAMES, NativeTorchValueScorer
from train_native_value_matrix import pairwise_gate, ranking_examples


def main() -> None:
    cache = json.loads(Path("artifacts/native-value-matrix-v7-robust-rollouts.json").read_text(encoding="utf-8"))
    training = [row for key, row in cache.items() if key.startswith("v7-robust:train:")]
    ranking = [row for key, row in cache.items() if key.startswith("v7-robust:ranking-train:")]
    validation = [row for key, row in cache.items() if key.startswith("v7-robust:holdout:")]
    train_x = torch.tensor([sample for episode in training for sample in episode["samples"]], dtype=torch.float32)
    train_y = torch.tensor([episode["return"] for episode in training for _ in episode["samples"]], dtype=torch.float32).unsqueeze(1)
    rank_left, rank_right, rank_sign = ranking_examples(ranking)
    positive_weight = max(1.0, float((train_y < 0.5).sum()) / max(1.0, float((train_y >= 0.5).sum())))
    sample_weights = torch.where(train_y >= 0.5, positive_weight, 1.0)
    enemy_columns = [index for index, name in enumerate(FEATURE_NAMES) if name.startswith("enemy_hash_")]
    rows = []
    for ablate_enemy_identity in (False, True):
        x, left, right = train_x.clone(), rank_left.clone(), rank_right.clone()
        evaluation = validation
        if ablate_enemy_identity:
            x[:, enemy_columns] = 0; left[:, enemy_columns] = 0; right[:, enemy_columns] = 0
            evaluation = [{**row, "child_features": [0.0 if index in enemy_columns else value for index, value in enumerate(row["child_features"])]} for row in validation]
        for hidden_sizes in ((64, 32), (128, 64)):
            for pointwise_weight in (0.0, 0.1, 1.0):
                for ranking_weight in (1.0, 4.0, 10.0):
                    torch.manual_seed(31)
                    model = NativeTorchValueScorer.create_model(hidden_sizes)
                    optimizer = torch.optim.AdamW(model.parameters(), lr=0.003, weight_decay=1e-4)
                    model.train()
                    for _ in range(600):
                        optimizer.zero_grad()
                        value_loss = ((torch.sigmoid(model(x)) - train_y).square() * sample_weights).mean()
                        rank_loss = torch.nn.functional.softplus(-rank_sign * (model(left) - model(right))).mean()
                        loss = pointwise_weight * value_loss + ranking_weight * rank_loss
                        loss.backward(); optimizer.step()
                    model.eval()
                    gate = pairwise_gate(model, evaluation, minimum_gap=0.02, minimum_pairs=50, threshold=0.90)
                    rows.append({
                        "ablate_enemy_identity": ablate_enemy_identity,
                        "hidden_sizes": hidden_sizes,
                        "pointwise_weight": pointwise_weight,
                        "ranking_weight": ranking_weight,
                        "accuracy": gate["accuracy"],
                        "correct_pairs": gate["correct_pairs"],
                        "comparable_pairs": gate["comparable_pairs"],
                    })
    rows.sort(key=lambda row: -row["accuracy"])
    print(json.dumps({"success": True, "data_source": "v7 training and validation only", "top": rows[:12]}, indent=2))


if __name__ == "__main__":
    main()
