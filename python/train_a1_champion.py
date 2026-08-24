"""
STS2 Ascension 1 Champion Policy Trainer.
Trains a specialized Set Transformer on 65,106 winning A0/A1 human expert decisions
across all 5 characters (Ironclad, Silent, Defect, Necrobinder, Regent).
"""

import os
import sys
import json
import time
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


def normalize_card_name(name: str) -> str:
    clean = name.upper().replace("+", "").replace(" ", "_").replace("CARD.", "").replace("STS2.", "").strip()
    return clean


class A1ChampionDraftDataset(Dataset):
    def __init__(self, decisions: List[Dict[str, Any]], card_to_idx: Dict[str, int], max_offered: int = 5):
        self.samples = []
        self.card_to_idx = card_to_idx
        self.max_offered = max_offered

        for d in decisions:
            if d.get("decision_type") != "card_reward":
                continue
            if not d.get("run_won", False) or d.get("ascension", 0) > 1:
                continue

            char = str(d.get("character", "IRONCLAD")).upper()
            char_idx = CHAR_TO_IDX.get(char, 0)
            asc = float(d.get("ascension", 0))
            floor = float(d.get("floor", 1)) / 50.0

            offered = d.get("offered", [])
            picked = d.get("picked", "")
            if not offered or not picked:
                continue

            offered_norm = [normalize_card_name(c) for c in offered[:max_offered]]
            picked_norm = normalize_card_name(picked)

            if picked_norm not in offered_norm:
                continue

            label_idx = offered_norm.index(picked_norm)
            offered_ids = [self.card_to_idx.get(c, 0) for c in offered_norm]

            # Pad offered to max_offered
            pad_len = max_offered - len(offered_ids)
            mask = [1.0] * len(offered_ids) + [0.0] * pad_len
            offered_ids = offered_ids + [0] * pad_len

            self.samples.append({
                "char_idx": char_idx,
                "asc": asc,
                "floor": floor,
                "offered_ids": offered_ids,
                "mask": mask,
                "label": label_idx,
                "num_offered": len(offered_norm)
            })

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        s = self.samples[idx]
        return {
            "char_idx": torch.tensor(s["char_idx"], dtype=torch.long),
            "context": torch.tensor([s["asc"], s["floor"]], dtype=torch.float32),
            "offered_ids": torch.tensor(s["offered_ids"], dtype=torch.long),
            "mask": torch.tensor(s["mask"], dtype=torch.float32),
            "label": torch.tensor(s["label"], dtype=torch.long),
            "num_offered": s["num_offered"]
        }


class A1ChampionPolicyNet(nn.Module):
    def __init__(self, vocab_size: int, num_chars: int = 5, embed_dim: int = 64, num_heads: int = 4):
        super().__init__()
        self.card_embed = nn.Embedding(vocab_size + 1, embed_dim, padding_idx=0)
        self.char_embed = nn.Embedding(num_chars, embed_dim)
        self.context_proj = nn.Linear(2, embed_dim)

        self.self_attn = nn.MultiheadAttention(embed_dim=embed_dim, num_heads=num_heads, batch_first=True)
        self.norm1 = nn.LayerNorm(embed_dim)
        self.norm2 = nn.LayerNorm(embed_dim)

        self.ffn = nn.Sequential(
            nn.Linear(embed_dim, embed_dim * 2),
            nn.ReLU(),
            nn.Dropout(0.1),
            nn.Linear(embed_dim * 2, embed_dim)
        )

        self.score_head = nn.Sequential(
            nn.Linear(embed_dim * 2, embed_dim),
            nn.ReLU(),
            nn.Linear(embed_dim, 1)
        )

    def forward(self, char_idx: torch.Tensor, context: torch.Tensor, offered_ids: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
        B, N = offered_ids.shape
        # Card embeddings: [B, N, D]
        card_e = self.card_embed(offered_ids)

        # Context embedding: [B, D]
        char_e = self.char_embed(char_idx)
        ctx_e = self.context_proj(context)
        global_ctx = char_e + ctx_e  # [B, D]

        # Key padding mask: True for padded items
        key_pad_mask = (mask == 0.0)

        # Self-attention among offered cards
        attn_out, _ = self.self_attn(card_e, card_e, card_e, key_padding_mask=key_pad_mask)
        x = self.norm1(card_e + attn_out)
        x = self.norm2(x + self.ffn(x))  # [B, N, D]

        # Concat global context to each card representation
        global_ctx_expanded = global_ctx.unsqueeze(1).expand(-1, N, -1)  # [B, N, D]
        pair_repr = torch.cat([x, global_ctx_expanded], dim=-1)  # [B, N, 2D]

        # Raw scores: [B, N]
        scores = self.score_head(pair_repr).squeeze(-1)

        # Mask invalid padding choices
        scores = scores.masked_fill(key_pad_mask, -1e9)
        return scores


def train_a1_champion(epochs: int = 10, batch_size: int = 64, lr: float = 1e-3):
    print("=" * 80)
    print("STS2 ASCENSION 1 CHAMPION MACRO PRIOR TRAINING (65k WINNING DECISIONS)")
    print("=" * 80)

    data_p = REPO_ROOT / "game_database" / "compiled_macro_decisions.json"
    with open(data_p, "r", encoding="utf-8") as f:
        all_decisions = json.load(f)

    # Build vocab of unique cards
    vocab = set()
    for d in all_decisions:
        for c in d.get("offered", []):
            vocab.add(normalize_card_name(c))
        if d.get("picked"):
            vocab.add(normalize_card_name(d["picked"]))

    card_to_idx = {c: i + 1 for i, c in enumerate(sorted(vocab))}
    print(f"Built Card Vocabulary: {len(card_to_idx)} unique cards")

    dataset = A1ChampionDraftDataset(all_decisions, card_to_idx, max_offered=5)
    print(f"Loaded {len(dataset)} valid A0/A1 winning draft samples.")

    # 85 / 15 Train / Val Split
    indices = list(range(len(dataset)))
    random.seed(42)
    random.shuffle(indices)
    split = int(len(dataset) * 0.85)
    train_idx, val_idx = indices[:split], indices[split:]

    train_loader = DataLoader(torch.utils.data.Subset(dataset, train_idx), batch_size=batch_size, shuffle=True)
    val_loader = DataLoader(torch.utils.data.Subset(dataset, val_idx), batch_size=batch_size, shuffle=False)

    model = A1ChampionPolicyNet(vocab_size=len(card_to_idx), embed_dim=64, num_heads=4)
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)
    criterion = nn.CrossEntropyLoss()

    best_top1_acc = 0.0
    models_dir = REPO_ROOT / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    out_path = models_dir / "v9_a1_champion_macro.pt"

    for epoch in range(1, epochs + 1):
        model.train()
        total_loss, total_correct, total_samples = 0.0, 0, 0

        for batch in train_loader:
            char_idx = batch["char_idx"]
            context = batch["context"]
            offered_ids = batch["offered_ids"]
            mask = batch["mask"]
            labels = batch["label"]

            optimizer.zero_grad()
            scores = model(char_idx, context, offered_ids, mask)
            loss = criterion(scores, labels)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()

            B = labels.size(0)
            preds = scores.argmax(dim=-1)
            total_loss += loss.item() * B
            total_correct += (preds == labels).sum().item()
            total_samples += B

        scheduler.step()
        train_loss = total_loss / max(1, total_samples)
        train_acc = (total_correct / max(1, total_samples)) * 100.0

        # Validation
        model.eval()
        val_loss, val_correct, val_top3_correct, val_samples = 0.0, 0, 0, 0
        with torch.no_grad():
            for batch in val_loader:
                char_idx = batch["char_idx"]
                context = batch["context"]
                offered_ids = batch["offered_ids"]
                mask = batch["mask"]
                labels = batch["label"]

                scores = model(char_idx, context, offered_ids, mask)
                loss = criterion(scores, labels)

                B = labels.size(0)
                val_loss += loss.item() * B

                preds = scores.argmax(dim=-1)
                val_correct += (preds == labels).sum().item()

                _, top3 = scores.topk(min(3, scores.size(-1)), dim=-1)
                val_top3_correct += (top3 == labels.unsqueeze(-1)).any(dim=-1).sum().item()
                val_samples += B

        avg_val_loss = val_loss / max(1, val_samples)
        val_acc = (val_correct / max(1, val_samples)) * 100.0
        val_top3 = (val_top3_correct / max(1, val_samples)) * 100.0

        print(f"Epoch {epoch:2d}/{epochs}: Train Loss={train_loss:.4f}, Train Acc={train_acc:.2f}% | Val Loss={avg_val_loss:.4f}, Val Top-1={val_acc:.2f}%, Val Top-3={val_top3:.2f}%")

        if val_acc > best_top1_acc:
            best_top1_acc = val_acc
            torch.save({
                "model_state_dict": model.state_dict(),
                "card_to_idx": card_to_idx,
                "val_top1_acc": val_acc,
                "val_top3_acc": val_top3,
                "epoch": epoch
            }, str(out_path))

    print(f"\n[OK] A1 Champion Macro Prior Training Complete! Best Val Top-1: {best_top1_acc:.2f}%")
    print(f"Saved Champion Checkpoint to: {out_path}")
    print("=" * 80)


if __name__ == "__main__":
    train_a1_champion()
