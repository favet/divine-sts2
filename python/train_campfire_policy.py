"""
STS2 Neural Campfire Policy Trainer.
Trains a calibrated neural network on 27,044 human expert campfire decisions (Smith vs Heal)
conditioned on HP percentage, current HP, max HP, character, ascension, and floor.
"""

import os
import sys
import json
import random
from pathlib import Path
from typing import List, Dict, Any, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader

REPO_ROOT = Path(__file__).resolve().parents[1]

ALL_CHARACTERS = ["IRONCLAD", "SILENT", "DEFECT", "NECROBINDER", "REGENT"]
CHAR_TO_IDX = {c: i for i, c in enumerate(ALL_CHARACTERS)}


class CampfireDataset(Dataset):
    def __init__(self, decisions: List[Dict[str, Any]]):
        self.features = []
        self.labels = []

        for d in decisions:
            if d.get("decision_type") != "campfire":
                continue
            char = str(d.get("character", "IRONCLAD")).upper()
            char_idx = CHAR_TO_IDX.get(char, 0)
            asc = float(d.get("ascension", 0)) / 20.0
            floor = float(d.get("floor", 1)) / 50.0

            hp_str = d.get("hp_str", "60/80")
            cur_hp, max_hp = 60, 80
            if "/" in hp_str:
                try:
                    parts = hp_str.split("/")
                    cur_hp, max_hp = int(parts[0]), int(parts[1])
                except Exception:
                    pass

            hp_pct = cur_hp / max(1, max_hp)
            cur_hp_norm = cur_hp / 100.0
            max_hp_norm = max_hp / 100.0

            choice = d.get("choice", "Smith")
            label = 1.0 if choice == "Smith" else 0.0  # 1 = Smith, 0 = Heal

            # One-hot character vector (5) + hp_pct, cur_hp, max_hp, asc, floor (5) = 10 features
            char_onehot = [0.0] * len(ALL_CHARACTERS)
            char_onehot[char_idx] = 1.0

            feat = char_onehot + [hp_pct, cur_hp_norm, max_hp_norm, asc, floor]
            self.features.append(feat)
            self.labels.append(label)

    def __len__(self):
        return len(self.features)

    def __getitem__(self, idx):
        return (
            torch.tensor(self.features[idx], dtype=torch.float32),
            torch.tensor(self.labels[idx], dtype=torch.float32)
        )


class CampfirePolicyNet(nn.Module):
    def __init__(self, input_dim: int = 10, hidden_dim: int = 64):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(input_dim, hidden_dim),
            nn.ReLU(),
            nn.BatchNorm1d(hidden_dim),
            nn.Linear(hidden_dim, hidden_dim // 2),
            nn.ReLU(),
            nn.Linear(hidden_dim // 2, 1),
            nn.Sigmoid()
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x).squeeze(-1)


def train_campfire_policy(epochs: int = 15, batch_size: int = 128, lr: float = 1e-3):
    print("=" * 80)
    print("STS2 NEURAL CAMPFIRE POLICY TRAINING (27k DECISIONS)")
    print("=" * 80)

    data_path = REPO_ROOT / "game_database" / "compiled_macro_decisions.json"
    with open(data_path, "r", encoding="utf-8") as f:
        all_decisions = json.load(f)

    dataset = CampfireDataset(all_decisions)
    print(f"Loaded {len(dataset)} valid campfire decisions.")

    # 80 / 20 Train / Validation Split
    indices = list(range(len(dataset)))
    random.seed(42)
    random.shuffle(indices)
    split = int(len(dataset) * 0.8)
    train_idx, val_idx = indices[:split], indices[split:]

    train_loader = DataLoader(torch.utils.data.Subset(dataset, train_idx), batch_size=batch_size, shuffle=True)
    val_loader = DataLoader(torch.utils.data.Subset(dataset, val_idx), batch_size=batch_size, shuffle=False)

    model = CampfirePolicyNet(input_dim=10, hidden_dim=64)
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    criterion = nn.BCELoss()

    best_val_acc = 0.0
    models_dir = REPO_ROOT / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    out_model_p = models_dir / "v9_campfire_policy.pt"

    for epoch in range(1, epochs + 1):
        model.train()
        total_loss, total_correct, total_samples = 0.0, 0, 0

        for feats, labels in train_loader:
            optimizer.zero_grad()
            preds = model(feats)
            loss = criterion(preds, labels)
            loss.backward()
            optimizer.step()

            total_loss += loss.item() * len(labels)
            total_correct += ((preds >= 0.5) == (labels >= 0.5)).sum().item()
            total_samples += len(labels)

        train_loss = total_loss / total_samples
        train_acc = (total_correct / total_samples) * 100.0

        # Validation
        model.eval()
        val_loss, val_correct, val_samples = 0.0, 0, 0
        with torch.no_grad():
            for feats, labels in val_loader:
                preds = model(feats)
                loss = criterion(preds, labels)
                val_loss += loss.item() * len(labels)
                val_correct += ((preds >= 0.5) == (labels >= 0.5)).sum().item()
                val_samples += len(labels)

        avg_val_loss = val_loss / val_samples
        val_acc = (val_correct / val_samples) * 100.0

        print(f"Epoch {epoch:2d}/{epochs}: Train Loss={train_loss:.4f}, Train Acc={train_acc:.2f}% | Val Loss={avg_val_loss:.4f}, Val Acc={val_acc:.2f}%")

        if val_acc > best_val_acc:
            best_val_acc = val_acc
            torch.save({
                "model_state_dict": model.state_dict(),
                "val_acc": val_acc,
                "val_loss": avg_val_loss,
                "epoch": epoch
            }, str(out_model_p))

    print(f"\n[OK] Campfire Policy Training Complete! Best Val Acc: {best_val_acc:.2f}%")
    print(f"Saved model to: {out_model_p}")
    print("=" * 80)


if __name__ == "__main__":
    train_campfire_policy()
