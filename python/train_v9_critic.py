"""
v9 Action-Conditioned Set Transformer Critic Trainer with Cosine Annealing & Gradient Clipping.
Trains on unified dataset (densified high-ascension winning runs + native exploration runs)
with multi-task loss (V_win, V_hp_loss, V_relic_ev, V_boss_readiness) and pairwise ranking evaluation.
"""

import os
import sys
import json
import glob
import math
import random
import time
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "python"))

from sts2_native_sim.v9_tokenizer import Sts2TokenEncoder
from sts2_native_sim.v9_transformer import Sts2SetTransformerCritic


class Sts2TransitionDataset(Dataset):
    """PyTorch Dataset over streamed native JSONL transitions."""

    def __init__(self, shard_files: List[Path], max_samples: int = 40000):
        self.samples = []

        for shard in shard_files:
            if not shard.exists():
                continue
            with open(shard, "r", encoding="utf-8") as f:
                for line in f:
                    if len(self.samples) >= max_samples:
                        break
                    try:
                        rec = json.loads(line)
                        obs = rec.get("observation", {})
                        legal_actions = rec.get("legal_actions", [])
                        targets = rec.get("targets", {})
                        if not legal_actions or not obs:
                            continue

                        # Tokenize observation
                        obs_tokens = Sts2TokenEncoder.tokenize_observation(obs)

                        # Encode legal actions (up to 8 candidates)
                        act_tokens = []
                        chosen_act_id = rec.get("action", "")
                        chosen_idx = 0

                        for idx, act in enumerate(legal_actions[:8]):
                            atype_idx, card_target, sub_idx = Sts2TokenEncoder.encode_action(act)
                            act_tokens.append([atype_idx, card_target, sub_idx])
                            aid = act.get("action_id", "") if isinstance(act, dict) else str(act)
                            if aid == chosen_act_id:
                                chosen_idx = idx

                        while len(act_tokens) < 8:
                            act_tokens.append([0, 0, 0])

                        self.samples.append({
                            "context": obs_tokens["context"],
                            "hand": obs_tokens["hand"],
                            "deck": obs_tokens["deck"],
                            "enemies": obs_tokens["enemies"],
                            "relics": obs_tokens["relics"],
                            "actions": torch.tensor(act_tokens, dtype=torch.long),
                            "chosen_idx": torch.tensor(chosen_idx, dtype=torch.long),
                            "v_win": torch.tensor(float(targets.get("v_win", 0.0)), dtype=torch.float32),
                            "v_hp_loss": torch.tensor(float(targets.get("v_hp_loss", 0.0)) / 100.0, dtype=torch.float32),
                            "v_relic_ev": torch.tensor(float(targets.get("v_relic_ev", 0.0)) / 10.0, dtype=torch.float32),
                            "v_boss_readiness": torch.tensor(float(targets.get("v_boss_readiness_heuristic", 0.0)) / 5.0, dtype=torch.float32),
                        })
                    except Exception:
                        continue

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        return self.samples[idx]


def train_v9_critic(epochs: int = 5, batch_size: int = 64, lr: float = 1e-3):
    print("=" * 80)
    print("STS2 v9 ACTION-CONDITIONED SET TRANSFORMER CRITIC TRAINING")
    print("=" * 80)

    # Locate shards
    traj_dir = REPO_ROOT / "artifacts" / "trajectories"
    shards = list(traj_dir.glob("shard_worker_*.jsonl")) + list((traj_dir / "densified_shards").glob("*.jsonl"))

    print(f"Discovered {len(shards)} trajectory shards:")
    for s in shards[:5]:
        print(f"  - {s.name}")
    if len(shards) > 5:
        print(f"  ... and {len(shards) - 5} more shards")

    dataset = Sts2TransitionDataset(shards, max_samples=40000)
    print(f"\nLoaded {len(dataset)} valid transition records for multi-task training.")

    # 80 / 20 Train / Validation Split
    indices = list(range(len(dataset)))
    random.seed(42)
    random.shuffle(indices)
    split = int(len(dataset) * 0.8)
    train_idx, val_idx = indices[:split], indices[split:]

    train_set = torch.utils.data.Subset(dataset, train_idx)
    val_set = torch.utils.data.Subset(dataset, val_idx)

    train_loader = DataLoader(train_set, batch_size=batch_size, shuffle=True)
    val_loader = DataLoader(val_set, batch_size=batch_size, shuffle=False)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Training on device: {device} ({len(train_set)} train, {len(val_set)} val)")

    model = Sts2SetTransformerCritic(d_model=64, n_heads=4, num_layers=2, ctx_dim=12).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs, eta_min=1e-5)

    criterion_bce = nn.BCELoss()
    criterion_mse = nn.MSELoss()

    best_val_loss = float("inf")
    best_ranking_acc = 0.0
    models_dir = REPO_ROOT / "artifacts" / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    best_model_path = models_dir / "v9_set_transformer_promoted.pt"
    root_model_path = REPO_ROOT / "models" / "v9_set_transformer_promoted.pt"
    root_model_path.parent.mkdir(parents=True, exist_ok=True)

    for epoch in range(1, epochs + 1):
        model.train()
        total_loss = 0.0
        total_samples = 0

        for batch in train_loader:
            ctx = batch["context"].to(device)
            hand = batch["hand"].to(device)
            deck = batch["deck"].to(device)
            enemies = batch["enemies"].to(device)
            relics = batch["relics"].to(device)
            actions = batch["actions"].to(device)
            chosen_idx = batch["chosen_idx"].to(device)

            target_win = batch["v_win"].to(device)
            target_hp = batch["v_hp_loss"].to(device)
            target_relic = batch["v_relic_ev"].to(device)
            target_boss = batch["v_boss_readiness"].to(device)

            optimizer.zero_grad()
            out = model(ctx, hand, deck, enemies, relics, actions)

            B = ctx.size(0)
            pred_win_chosen = out["v_win"][torch.arange(B), chosen_idx]
            pred_hp_chosen = out["v_hp_loss"][torch.arange(B), chosen_idx]
            pred_relic_chosen = out["v_relic_ev"][torch.arange(B), chosen_idx]
            pred_boss_chosen = out["v_boss_readiness"][torch.arange(B), chosen_idx]

            loss_win = criterion_bce(pred_win_chosen, target_win)
            loss_hp = criterion_mse(pred_hp_chosen, target_hp)
            loss_relic = criterion_mse(pred_relic_chosen, target_relic)
            loss_boss = criterion_mse(pred_boss_chosen, target_boss)

            loss = loss_win + 0.1 * loss_hp + 0.1 * loss_relic + 0.1 * loss_boss
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()

            total_loss += loss.item() * B
            total_samples += B

        scheduler.step()
        avg_train_loss = total_loss / max(1, total_samples)

        # Validation & Ranking Evaluation
        model.eval()
        val_loss = 0.0
        val_samples = 0
        correct_rankings = 0
        total_rankings = 0

        with torch.no_grad():
            for batch in val_loader:
                ctx = batch["context"].to(device)
                hand = batch["hand"].to(device)
                deck = batch["deck"].to(device)
                enemies = batch["enemies"].to(device)
                relics = batch["relics"].to(device)
                actions = batch["actions"].to(device)
                chosen_idx = batch["chosen_idx"].to(device)

                target_win = batch["v_win"].to(device)
                target_hp = batch["v_hp_loss"].to(device)
                target_relic = batch["v_relic_ev"].to(device)
                target_boss = batch["v_boss_readiness"].to(device)

                B = ctx.size(0)
                out = model(ctx, hand, deck, enemies, relics, actions)

                pred_win_chosen = out["v_win"][torch.arange(B), chosen_idx]
                pred_hp_chosen = out["v_hp_loss"][torch.arange(B), chosen_idx]
                pred_relic_chosen = out["v_relic_ev"][torch.arange(B), chosen_idx]
                pred_boss_chosen = out["v_boss_readiness"][torch.arange(B), chosen_idx]

                loss_win = criterion_bce(pred_win_chosen, target_win)
                loss_hp = criterion_mse(pred_hp_chosen, target_hp)
                loss_relic = criterion_mse(pred_relic_chosen, target_relic)
                loss_boss = criterion_mse(pred_boss_chosen, target_boss)

                batch_val_loss = loss_win + 0.1 * loss_hp + 0.1 * loss_relic + 0.1 * loss_boss
                val_loss += batch_val_loss.item() * B
                val_samples += B

                for b_i in range(B):
                    c_idx = chosen_idx[b_i].item()
                    win_scores = out["v_win"][b_i]
                    chosen_score = win_scores[c_idx]
                    for other_idx in range(len(win_scores)):
                        if other_idx != c_idx and actions[b_i, other_idx, 0] != 0:
                            total_rankings += 1
                            if chosen_score >= win_scores[other_idx]:
                                correct_rankings += 1

        avg_val_loss = val_loss / max(1, val_samples)
        ranking_acc = (correct_rankings / max(1, total_rankings)) * 100.0

        print(f"Epoch {epoch}/{epochs}: LR={scheduler.get_last_lr()[0]:.6f}, Train Loss={avg_train_loss:.4f} | Val Loss={avg_val_loss:.4f}, Pairwise Ranking Acc={ranking_acc:.2f}%")

        if avg_val_loss < best_val_loss:
            best_val_loss = avg_val_loss
            best_ranking_acc = ranking_acc
            torch.save({
                "model_state_dict": model.state_dict(),
                "ranking_acc": ranking_acc,
                "val_loss": avg_val_loss,
                "epoch": epoch,
                "d_model": 64,
                "n_heads": 4
            }, str(best_model_path))
            torch.save({
                "model_state_dict": model.state_dict(),
                "ranking_acc": ranking_acc,
                "val_loss": avg_val_loss,
                "epoch": epoch,
                "d_model": 64,
                "n_heads": 4
            }, str(root_model_path))

    print(f"\n[OK] v9 Critic Training Complete! Best Val Loss: {best_val_loss:.4f}, Ranking Acc: {best_ranking_acc:.2f}%")
    print(f"Promoted checkpoint saved to:\n  - {best_model_path}\n  - {root_model_path}")
    print("=" * 80)


if __name__ == "__main__":
    train_v9_critic(epochs=5, batch_size=64, lr=1e-3)
