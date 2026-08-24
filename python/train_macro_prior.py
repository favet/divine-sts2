"""
Supervised Macro Policy Prior Trainer (Behavioral Cloning for STS2).
Trains a Set Transformer on 40k+ human expert macro choices (card drafting, campfires, pathing)
filtering for high-ascension / winning runs to establish a strong behavioral prior.
"""

import os
import sys
import json
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
sys.path.insert(0, str(REPO_ROOT))

from deck_transformer import CardVocab, CardRelicSetTransformer


class MacroChoiceDataset(Dataset):
    """PyTorch Dataset for human macro choices."""

    def __init__(self, decisions: List[Dict[str, Any]], max_deck: int = 40, max_relics: int = 15):
        self.samples = []
        self.max_deck = max_deck
        self.max_relics = max_relics

        for d in decisions:
            if d.get("decision_type") != "card_reward":
                continue
            offered = d.get("offered", [])
            picked = d.get("picked", "SKIP")
            if not offered or len(offered) < 2:
                continue

            char = d.get("character", "IRONCLAD")
            floor = int(d.get("floor", 1))
            asc = int(d.get("ascension", 0))
            hp = int(d.get("hp", 65))
            max_hp = int(d.get("max_hp", 80))
            gold = int(d.get("gold", 100))

            deck_raw = d.get("deck_snapshot", ["Strike", "Strike", "Defend", "Defend", "Bash"])
            relics_raw = d.get("relics_snapshot", ["Burning Blood"])

            # Encode deck tokens
            card_ids, up_ids, ench_ids = [], [], []
            for c in deck_raw[:max_deck]:
                c_idx, u_idx, e_idx = CardVocab.encode_card(str(c))
                card_ids.append(c_idx)
                up_ids.append(u_idx)
                ench_ids.append(e_idx)

            while len(card_ids) < max_deck:
                card_ids.append(CardVocab.card_to_idx.get("PAD", 0))
                up_ids.append(0)
                ench_ids.append(0)

            # Encode relics
            relic_ids = [CardVocab.encode_relic(str(r)) for r in relics_raw[:max_relics]]
            while len(relic_ids) < max_relics:
                relic_ids.append(CardVocab.relic_to_idx.get("PAD", 0))

            # Encode context
            ctx = CardVocab.encode_context(hp, max_hp, floor, gold, char)

            # Encode offered candidate cards (up to 4 candidates)
            cand_tokens = []
            target_idx = 0
            for i, cand in enumerate(offered[:4]):
                c_idx, u_idx, e_idx = CardVocab.encode_card(str(cand))
                cand_tokens.append([c_idx, u_idx, e_idx])
                if cand == picked or normalize_card(cand) == normalize_card(picked):
                    target_idx = i

            while len(cand_tokens) < 4:
                cand_tokens.append([CardVocab.card_to_idx.get("PAD", 0), 0, 0])

            self.samples.append({
                "cards": torch.tensor(card_ids, dtype=torch.long),
                "upgrades": torch.tensor(up_ids, dtype=torch.long),
                "enchantments": torch.tensor(ench_ids, dtype=torch.long),
                "relics": torch.tensor(relic_ids, dtype=torch.long),
                "context": torch.tensor(ctx, dtype=torch.float32),
                "candidates": torch.tensor(cand_tokens, dtype=torch.long),
                "target": torch.tensor(target_idx, dtype=torch.long),
                "num_options": min(len(offered), 4)
            })

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        return self.samples[idx]


def normalize_card(c: str) -> str:
    return str(c).upper().replace("CARD.", "").replace(" ", "_").split("+")[0].strip()


def train_macro_prior(epochs: int = 5, batch_size: int = 64, lr: float = 1e-3):
    print("=" * 80)
    print("STS2 SUPERVISED MACRO POLICY PRIOR (SET TRANSFORMER BEHAVIORAL CLONING)")
    print("=" * 80)

    decisions_p = REPO_ROOT / "game_database" / "compiled_macro_decisions.json"
    if not decisions_p.exists():
        print(f"Error: {decisions_p} not found. Run ingest_community_runs.py first.")
        return

    with open(decisions_p, "r", encoding="utf-8") as f:
        all_decisions = json.load(f)

    print(f"Loaded {len(all_decisions)} macro decisions.")
    dataset = MacroChoiceDataset(all_decisions)
    print(f"Constructed {len(dataset)} card draft choice training samples.")

    # Train / Val Split (80 / 20)
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
    print(f"Training on device: {device} (Samples: {len(train_set)} train, {len(val_set)} val)")

    model = CardRelicSetTransformer(d_model=64, n_heads=4, num_layers=2, ctx_dim=8).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    criterion = nn.CrossEntropyLoss()

    best_val_acc = 0.0
    models_dir = REPO_ROOT / "artifacts" / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    out_model_p = models_dir / "v9_macro_prior_pretrained.pt"
    out_model_root = REPO_ROOT / "models" / "v9_macro_prior_pretrained.pt"
    out_model_root.parent.mkdir(parents=True, exist_ok=True)

    for epoch in range(1, epochs + 1):
        model.train()
        total_loss = 0.0
        correct = 0
        total = 0

        for batch in train_loader:
            cards = batch["cards"].to(device)
            upgrades = batch["upgrades"].to(device)
            enchantments = batch["enchantments"].to(device)
            relics = batch["relics"].to(device)
            context = batch["context"].to(device)
            candidates = batch["candidates"].to(device)
            target = batch["target"].to(device)

            optimizer.zero_grad()
            deck_rep = model.forward_deck_representation(cards, upgrades, enchantments, relics, context)
            logits = model.score_candidate_cards(deck_rep, candidates)  # [B, 4]

            loss = criterion(logits, target)
            loss.backward()
            optimizer.step()

            total_loss += loss.item() * cards.size(0)
            preds = logits.argmax(dim=-1)
            correct += (preds == target).sum().item()
            total += cards.size(0)

        train_acc = correct / max(1, total)
        avg_train_loss = total_loss / max(1, total)

        # Validation
        model.eval()
        val_loss = 0.0
        val_correct = 0
        val_top3_correct = 0
        val_total = 0

        with torch.no_grad():
            for batch in val_loader:
                cards = batch["cards"].to(device)
                upgrades = batch["upgrades"].to(device)
                enchantments = batch["enchantments"].to(device)
                relics = batch["relics"].to(device)
                context = batch["context"].to(device)
                candidates = batch["candidates"].to(device)
                target = batch["target"].to(device)

                deck_rep = model.forward_deck_representation(cards, upgrades, enchantments, relics, context)
                logits = model.score_candidate_cards(deck_rep, candidates)
                loss = criterion(logits, target)

                val_loss += loss.item() * cards.size(0)
                preds = logits.argmax(dim=-1)
                val_correct += (preds == target).sum().item()

                # Top-3 Accuracy
                top3 = logits.topk(min(3, logits.size(-1)), dim=-1).indices
                for i in range(cards.size(0)):
                    if target[i] in top3[i]:
                        val_top3_correct += 1

                val_total += cards.size(0)

        val_acc = val_correct / max(1, val_total)
        val_top3_acc = val_top3_correct / max(1, val_total)
        avg_val_loss = val_loss / max(1, val_total)

        print(f"Epoch {epoch}/{epochs}: Train Loss={avg_train_loss:.4f}, Train Acc={train_acc*100:.2f}% | Val Loss={avg_val_loss:.4f}, Val Top-1={val_acc*100:.2f}%, Val Top-3={val_top3_acc*100:.2f}%")

        if val_acc > best_val_acc:
            best_val_acc = val_acc
            torch.save({
                "model_state_dict": model.state_dict(),
                "vocab_cards": len(CardVocab.CARDS),
                "val_top1_acc": val_acc,
                "val_top3_acc": val_top3_acc,
                "epoch": epoch
            }, str(out_model_p))
            torch.save({
                "model_state_dict": model.state_dict(),
                "vocab_cards": len(CardVocab.CARDS),
                "val_top1_acc": val_acc,
                "val_top3_acc": val_top3_acc,
                "epoch": epoch
            }, str(out_model_root))

    print(f"\n[OK] Training Complete! Best Val Top-1 Accuracy: {best_val_acc*100:.2f}%")
    print(f"Model saved to:\n  - {out_model_p}\n  - {out_model_root}")
    print("=" * 80)


if __name__ == "__main__":
    train_macro_prior(epochs=5, batch_size=64, lr=1e-3)
