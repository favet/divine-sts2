"""
STS2 V11 Enemy-Aware Combat Policy Trainer.
Embeds exact enemy identities (Nibbits, Ruby Raiders, Slimes, Byrds, Elites, Bosses) alongside
card tokens and player context, learning encounter-specific combat tactics.
"""

import os
import sys
import glob
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

ACTION_TYPE_MAP = {
    "play_card": 1,
    "use_potion": 2,
    "end_turn": 3,
    "choose_reward": 4,
    "choose_card": 5,
    "choose_map": 6,
    "choose_rest": 7,
    "proceed": 8
}


def normalize_id(name: str) -> str:
    return name.upper().replace("+", "").replace(" ", "_").replace("CARD.", "").replace("STS2.", "").strip()


class EnemyAwareCombatDataset(Dataset):
    def __init__(self, shards: List[str], card_to_idx: Dict[str, int], enemy_to_idx: Dict[str, int], max_actions: int = 32):
        self.samples = []
        self.card_to_idx = card_to_idx
        self.enemy_to_idx = enemy_to_idx
        self.max_actions = max_actions

        for shard_path in shards:
            with open(shard_path, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue

                    obs = d.get("observation", {})
                    if isinstance(obs, str):
                        try:
                            obs = json.loads(obs)
                        except Exception:
                            obs = {}

                    if d.get("state_hash") and d.get("state_hash") == obs.get("state_hash"):
                        continue

                    phase = d.get("phase") or obs.get("phase")
                    if phase != "combat":
                        continue

                    combat = obs.get("combat", {})
                    legal_actions = d.get("legal_actions", [])
                    chosen_action = d.get("action", "")

                    if not legal_actions or not chosen_action:
                        continue

                    action_ids = [a.get("action_id", "") for a in legal_actions]
                    if chosen_action not in action_ids:
                        continue

                    label_idx = action_ids.index(chosen_action)
                    if label_idx >= max_actions:
                        continue

                    floor = float(d.get("floor", 1))
                    hp_cur = float(obs.get("player_hp", 60))
                    hp_max = max(1.0, float(obs.get("player_max_hp", 80)))
                    hp_pct = hp_cur / hp_max
                    adv_weight = 1.0 + (floor / 15.0) + (hp_pct * 0.5)

                    char = str(d.get("character", "IRONCLAD")).upper()
                    char_idx = CHAR_TO_IDX.get(char, 0)
                    block_norm = float(obs.get("player_block", 0)) / 50.0
                    energy_norm = float(obs.get("player_energy", 3)) / 5.0
                    turn_norm = float(combat.get("turn", 1)) / 15.0
                    floor_norm = floor / 50.0

                    context = [hp_pct, block_norm, energy_norm, turn_norm, floor_norm]

                    # Hand tokens: [10, 3] (card_id, cost, upgrades)
                    hand_raw = combat.get("hand", [])
                    hand_tokens = []
                    for c in hand_raw[:10]:
                        cid = normalize_id(c.get("card_id", "UNKNOWN"))
                        c_idx = self.card_to_idx.get(cid, 0)
                        cost = min(5, max(0, int(c.get("cost", 1))))
                        upgrades = 1 if c.get("upgrades", 0) > 0 else 0
                        hand_tokens.append([c_idx, cost, upgrades])

                    pad_hand = 10 - len(hand_tokens)
                    hand_tokens = hand_tokens + [[0, 0, 0]] * pad_hand

                    # Enemy tokens: [5, 5] (enemy_id, hp_pct, block_norm, intent_dmg_norm, is_alive)
                    enemies_raw = combat.get("enemies", [])
                    enemy_tokens = []
                    for e in enemies_raw[:5]:
                        eid_raw = normalize_id(e.get("model_id") or e.get("id") or e.get("name") or "UNKNOWN")
                        e_idx = self.enemy_to_idx.get(eid_raw, 0)
                        e_hp = float(e.get("hp", 20)) / max(1.0, float(e.get("max_hp", 20)))
                        e_blk = float(e.get("block", 0)) / 50.0
                        dmg = float(e.get("damage", 0)) * max(1, float(e.get("repeats", 1))) / 30.0
                        alive = 1.0 if e.get("is_alive", True) else 0.0
                        enemy_tokens.append([float(e_idx), e_hp, e_blk, dmg, alive])

                    pad_enemies = 5 - len(enemy_tokens)
                    enemy_tokens = enemy_tokens + [[0.0, 0.0, 0.0, 0.0, 0.0]] * pad_enemies

                    # Candidate legal actions: [max_actions, 3]
                    action_tokens = []
                    for a in legal_actions[:max_actions]:
                        atype_str = a.get("action_type", "")
                        atype_int = ACTION_TYPE_MAP.get(atype_str, 1)
                        meta = a.get("metadata", {}) or {}
                        cid = normalize_id(meta.get("card_id", a.get("description", "")))
                        c_idx = self.card_to_idx.get(cid, 0)
                        target = int(meta.get("target_id", 0))
                        action_tokens.append([atype_int, c_idx, target])

                    num_valid_actions = len(action_tokens)
                    pad_act = max_actions - num_valid_actions
                    mask = [1.0] * num_valid_actions + [0.0] * pad_act
                    action_tokens = action_tokens + [[0, 0, 0]] * pad_act

                    self.samples.append({
                        "char_idx": char_idx,
                        "context": context,
                        "hand_tokens": hand_tokens,
                        "enemy_tokens": enemy_tokens,
                        "action_tokens": action_tokens,
                        "mask": mask,
                        "label": label_idx,
                        "weight": adv_weight
                    })

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        s = self.samples[idx]
        return {
            "char_idx": torch.tensor(s["char_idx"], dtype=torch.long),
            "context": torch.tensor(s["context"], dtype=torch.float32),
            "hand_tokens": torch.tensor(s["hand_tokens"], dtype=torch.long),
            "enemy_tokens": torch.tensor(s["enemy_tokens"], dtype=torch.float32),
            "action_tokens": torch.tensor(s["action_tokens"], dtype=torch.long),
            "mask": torch.tensor(s["mask"], dtype=torch.float32),
            "label": torch.tensor(s["label"], dtype=torch.long),
            "weight": torch.tensor(s["weight"], dtype=torch.float32)
        }


class V11EnemyAwareCombatPolicyNet(nn.Module):
    """V11 Enemy-Aware Action-Conditioned Transformer Combat Policy Net."""

    def __init__(self, vocab_size: int, enemy_vocab_size: int, embed_dim: int = 128, num_heads: int = 4, max_actions: int = 16):
        super().__init__()
        self.embed_dim = embed_dim
        self.max_actions = max_actions

        self.card_embed = nn.Embedding(vocab_size + 1, embed_dim, padding_idx=0)
        self.enemy_embed = nn.Embedding(enemy_vocab_size + 1, embed_dim // 2, padding_idx=0)
        self.char_embed = nn.Embedding(5, embed_dim)
        self.context_proj = nn.Linear(5, embed_dim)

        self.cost_embed = nn.Embedding(6, embed_dim // 4)
        self.upgrade_embed = nn.Embedding(2, embed_dim // 4)
        self.hand_proj = nn.Linear(embed_dim + embed_dim // 2, embed_dim)

        self.enemy_stats_proj = nn.Linear(4, embed_dim // 2)
        self.enemy_fusion = nn.Linear(embed_dim, embed_dim)

        self.action_type_embed = nn.Embedding(10, embed_dim // 4)
        self.action_target_embed = nn.Embedding(10, embed_dim // 4)
        self.action_proj = nn.Linear(embed_dim + embed_dim // 2, embed_dim)

        encoder_layer = nn.TransformerEncoderLayer(
            d_model=embed_dim,
            nhead=num_heads,
            dim_feedforward=embed_dim * 2,
            dropout=0.1,
            activation="gelu",
            batch_first=True
        )
        self.transformer = nn.TransformerEncoder(encoder_layer, num_layers=4)

        self.action_cross_attn = nn.MultiheadAttention(embed_dim, num_heads, batch_first=True)
        self.score_head = nn.Sequential(
            nn.Linear(embed_dim * 2, embed_dim),
            nn.GELU(),
            nn.Dropout(0.1),
            nn.Linear(embed_dim, 1)
        )

    def forward(
        self,
        char_idx: torch.Tensor,
        context: torch.Tensor,
        hand_tokens: torch.Tensor,
        enemy_tokens: torch.Tensor,
        action_tokens: torch.Tensor,
        mask: torch.Tensor
    ) -> torch.Tensor:
        B, K, _ = action_tokens.shape

        char_tok = self.char_embed(char_idx).unsqueeze(1)
        ctx_tok = self.context_proj(context).unsqueeze(1)

        # Hand tokens
        h_card = self.card_embed(hand_tokens[:, :, 0])
        h_cost = self.cost_embed(hand_tokens[:, :, 1])
        h_upg = self.upgrade_embed(hand_tokens[:, :, 2])
        hand_toks = self.hand_proj(torch.cat([h_card, h_cost, h_upg], dim=-1))

        # Enemy tokens with identity embedding
        e_ids = enemy_tokens[:, :, 0].long().clamp(0, self.enemy_embed.num_embeddings - 1)
        e_id_emb = self.enemy_embed(e_ids)
        e_stats_emb = self.enemy_stats_proj(enemy_tokens[:, :, 1:])
        enemy_toks = self.enemy_fusion(torch.cat([e_id_emb, e_stats_emb], dim=-1))

        state_bag = torch.cat([char_tok, ctx_tok, hand_toks, enemy_toks], dim=1)
        state_encoded = self.transformer(state_bag)

        global_state = state_encoded.mean(dim=1, keepdim=True)

        act_type = self.action_type_embed(action_tokens[:, :, 0])
        act_card = self.card_embed(action_tokens[:, :, 1])
        act_target = self.action_target_embed(action_tokens[:, :, 2].clamp(0, 9))
        act_emb = self.action_proj(torch.cat([act_card, act_type, act_target], dim=-1))

        key_pad_mask = (mask == 0.0)
        cross_out, _ = self.action_cross_attn(act_emb, state_encoded, state_encoded)

        global_expanded = global_state.expand(-1, K, -1)
        fusion = torch.cat([cross_out, global_expanded], dim=-1)
        logits = self.score_head(fusion).squeeze(-1)

        logits = logits.masked_fill(key_pad_mask, -1e9)
        return logits


def train_v11_enemy_aware_policy(epochs: int = 15, batch_size: int = 64, lr: float = 1e-3):
    print("=" * 80)
    print("TRAINING V11 ENEMY-AWARE COMBAT POLICY (IDENTITY EMBEDDINGS)")
    print("=" * 80)

    shards = glob.glob(str(REPO_ROOT / "artifacts" / "trajectories" / "*.jsonl"))
    print(f"Discovered {len(shards)} trajectory shards.")

    vocab = set()
    enemy_vocab = set()
    for shard in shards:
        with open(shard, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip(): continue
                try:
                    d = json.loads(line)
                    obs = d.get("observation", {})
                    if isinstance(obs, str): obs = json.loads(obs)

                    for a in d.get("legal_actions", []):
                        meta = a.get("metadata") or {}
                        cid = normalize_id(meta.get("card_id", a.get("description", "")))
                        if cid: vocab.add(cid)
                    for c in obs.get("combat", {}).get("hand", []):
                        cid = normalize_id(c.get("card_id", ""))
                        if cid: vocab.add(cid)
                    for e in obs.get("combat", {}).get("enemies", []):
                        eid = normalize_id(e.get("model_id") or e.get("id") or e.get("name") or "")
                        if eid: enemy_vocab.add(eid)
                except Exception:
                    pass

    card_to_idx = {c: i + 1 for i, c in enumerate(sorted(vocab))}
    enemy_to_idx = {e: i + 1 for i, e in enumerate(sorted(enemy_vocab))}
    print(f"Card Vocab: {len(card_to_idx)} cards | Enemy Vocab: {len(enemy_to_idx)} unique enemy models")

    if not enemy_to_idx:
        raise RuntimeError("Enemy vocabulary is empty; refusing to train a falsely enemy-aware checkpoint")

    dataset = EnemyAwareCombatDataset(shards, card_to_idx, enemy_to_idx, max_actions=32)
    print(f"Dataset Size: {len(dataset)} enemy-aware combat transitions.")

    indices = list(range(len(dataset)))
    random.seed(42)
    random.shuffle(indices)
    split = int(len(dataset) * 0.85)
    train_idx, val_idx = indices[:split], indices[split:]

    train_loader = DataLoader(torch.utils.data.Subset(dataset, train_idx), batch_size=batch_size, shuffle=True)
    val_loader = DataLoader(torch.utils.data.Subset(dataset, val_idx), batch_size=batch_size, shuffle=False)

    model = V11EnemyAwareCombatPolicyNet(
        vocab_size=len(card_to_idx),
        enemy_vocab_size=len(enemy_to_idx),
        embed_dim=128,
        num_heads=4,
        max_actions=32
    )
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs, eta_min=1e-5)

    best_top1 = 0.0
    out_path = REPO_ROOT / "models" / "v11_combat_policy.pt"

    for epoch in range(1, epochs + 1):
        model.train()
        total_loss, total_correct, total_samples = 0.0, 0, 0

        for batch in train_loader:
            char_idx = batch["char_idx"]
            context = batch["context"]
            hand_tokens = batch["hand_tokens"]
            enemy_tokens = batch["enemy_tokens"]
            action_tokens = batch["action_tokens"]
            mask = batch["mask"]
            labels = batch["label"]
            weights = batch["weight"]

            optimizer.zero_grad()
            logits = model(char_idx, context, hand_tokens, enemy_tokens, action_tokens, mask)

            log_probs = F.log_softmax(logits, dim=-1)
            loss_unweighted = F.nll_loss(log_probs, labels, reduction="none")
            loss = (loss_unweighted * weights).mean()

            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()

            B = labels.size(0)
            preds = logits.argmax(dim=-1)
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
                hand_tokens = batch["hand_tokens"]
                enemy_tokens = batch["enemy_tokens"]
                action_tokens = batch["action_tokens"]
                mask = batch["mask"]
                labels = batch["label"]

                logits = model(char_idx, context, hand_tokens, enemy_tokens, action_tokens, mask)
                loss = F.cross_entropy(logits, labels)

                B = labels.size(0)
                val_loss += loss.item() * B
                preds = logits.argmax(dim=-1)
                val_correct += (preds == labels).sum().item()

                _, top3 = logits.topk(min(3, logits.size(-1)), dim=-1)
                val_top3_correct += (top3 == labels.unsqueeze(-1)).any(dim=-1).sum().item()
                val_samples += B

        avg_val_loss = val_loss / max(1, val_samples)
        val_acc = (val_correct / max(1, val_samples)) * 100.0
        val_top3 = (val_top3_correct / max(1, val_samples)) * 100.0

        print(f"Epoch {epoch:2d}/{epochs}: Train Loss={train_loss:.4f}, Train Acc={train_acc:.2f}% | Val Loss={avg_val_loss:.4f}, Val Top-1={val_acc:.2f}%, Val Top-3={val_top3:.2f}%")

        if val_acc > best_top1:
            best_top1 = val_acc
            torch.save({
                "model_state_dict": model.state_dict(),
                "card_to_idx": card_to_idx,
                "enemy_to_idx": enemy_to_idx,
                "val_top1_acc": val_acc,
                "val_top3_acc": val_top3,
                "epoch": epoch
            }, str(out_path))

    print(f"\n[OK] V11 Enemy-Aware Training Complete! Best Val Top-1: {best_top1:.2f}%")
    print(f"Saved checkpoint to: {out_path}")
    print("=" * 80)


if __name__ == "__main__":
    train_v11_enemy_aware_policy()
