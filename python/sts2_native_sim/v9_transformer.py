"""
v9 Action-Conditioned Set Transformer Critic for Slay the Spire 2.
Implements:
1. Permutation-invariant Set Attention / ISAB over Hand, Deck, Enemies, and Relics.
2. Cross-Attention between Candidate Actions and State representations.
3. 4 Decoupled Multi-Task Heads: V_win, V_hp_loss, V_relic_ev, V_boss_readiness.
4. Skip-Relative Advantage Head: Advantage(a) = Q(s, a) - Q(s, skip).
"""

import os
import sys
import math
from pathlib import Path
from typing import Dict, List, Any, Tuple, Optional

import torch
import torch.nn as nn
import torch.nn.functional as F

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from deck_transformer import CardVocab
from native_sim.python.sts2_native_sim.v9_tokenizer import Sts2TokenEncoder


class Sts2SetTransformerCritic(nn.Module):
    """Action-conditioned multi-task Set Transformer Critic."""

    def __init__(self, d_model: int = 64, n_heads: int = 4, num_layers: int = 2, ctx_dim: int = 12):
        super().__init__()
        self.d_model = d_model
        self.ctx_dim = ctx_dim

        # 1. Embeddings
        self.card_embed = nn.Embedding(len(CardVocab.CARDS), d_model)
        self.upgrade_embed = nn.Embedding(2, d_model // 4)
        self.cost_embed = nn.Embedding(6, d_model // 4)
        self.card_proj = nn.Linear(d_model + d_model // 2, d_model)

        self.relic_embed = nn.Embedding(len(CardVocab.RELICS), d_model)
        self.action_type_embed = nn.Embedding(len(Sts2TokenEncoder.ACTION_TYPES), d_model // 2)
        self.action_proj = nn.Linear(d_model // 2 + d_model, d_model)

        self.enemy_proj = nn.Sequential(
            nn.Linear(5, d_model),
            nn.GELU(),
            nn.Linear(d_model, d_model)
        )

        self.context_proj = nn.Sequential(
            nn.Linear(ctx_dim, d_model),
            nn.GELU(),
            nn.Linear(d_model, d_model)
        )

        # 2. Transformer Encoder Layers
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=d_model,
            nhead=n_heads,
            dim_feedforward=d_model * 2,
            dropout=0.1,
            activation="gelu",
            batch_first=True
        )
        self.transformer = nn.TransformerEncoder(encoder_layer, num_layers=num_layers)

        # 3. Learnable Pooling Seed Query
        self.pool_query = nn.Parameter(torch.randn(1, 1, d_model))
        self.pool_attn = nn.MultiheadAttention(d_model, n_heads, batch_first=True)

        # 4. Action Conditioning Cross-Attention / Interaction
        self.action_cross_attn = nn.MultiheadAttention(d_model, n_heads, batch_first=True)
        self.action_fusion = nn.Sequential(
            nn.Linear(d_model * 2, d_model),
            nn.GELU(),
            nn.Linear(d_model, d_model)
        )

        # 5. Decoupled Multi-Task Heads
        # Head 1: Win Probability V_win in [0, 1]
        self.head_win = nn.Sequential(
            nn.Linear(d_model, d_model // 2),
            nn.GELU(),
            nn.Linear(d_model // 2, 1),
            nn.Sigmoid()
        )

        # Head 2: Expected HP Loss V_hp_loss >= 0
        self.head_hp_loss = nn.Sequential(
            nn.Linear(d_model, d_model // 2),
            nn.GELU(),
            nn.Linear(d_model // 2, 1),
            nn.ReLU()
        )

        # Head 3: Relic Expected Value V_relic_ev >= 0
        self.head_relic_ev = nn.Sequential(
            nn.Linear(d_model, d_model // 2),
            nn.GELU(),
            nn.Linear(d_model // 2, 1),
            nn.ReLU()
        )

        # Head 4: Boss Readiness Heuristic V_boss_readiness >= 0
        self.head_boss_readiness = nn.Sequential(
            nn.Linear(d_model, d_model // 2),
            nn.GELU(),
            nn.Linear(d_model // 2, 1),
            nn.ReLU()
        )

    def encode_state(
        self,
        context: torch.Tensor,   # [B, ctx_dim]
        hand: torch.Tensor,      # [B, N_hand, 3]
        deck: torch.Tensor,      # [B, N_deck, 2]
        enemies: torch.Tensor,   # [B, N_enemies, 5]
        relics: torch.Tensor     # [B, N_relics]
    ) -> torch.Tensor:
        B = context.size(0)

        # Context Token: [B, 1, d_model]
        ctx_tok = self.context_proj(context).unsqueeze(1)

        # Hand Tokens: [B, N_hand, d_model]
        c_emb = self.card_embed(hand[:, :, 0])
        u_emb = self.upgrade_embed(hand[:, :, 1])
        cost_emb = self.cost_embed(hand[:, :, 2])
        hand_toks = self.card_proj(torch.cat([c_emb, u_emb, cost_emb], dim=-1))

        # Deck Bag Tokens: [B, N_deck, d_model]
        d_c_emb = self.card_embed(deck[:, :, 0])
        d_u_emb = self.upgrade_embed(deck[:, :, 1])
        d_cost_zero = self.cost_embed(torch.zeros_like(deck[:, :, 1]))
        deck_toks = self.card_proj(torch.cat([d_c_emb, d_u_emb, d_cost_zero], dim=-1))

        # Enemy Tokens: [B, N_enemies, d_model]
        enemy_toks = self.enemy_proj(enemies)

        # Relic Tokens: [B, N_relics, d_model]
        relic_toks = self.relic_embed(relics)

        # Concatenate entire token bag: [B, 1 + N_h + N_d + N_e + N_r, d_model]
        token_bag = torch.cat([ctx_tok, hand_toks, deck_toks, enemy_toks, relic_toks], dim=1)

        # Permutation-Invariant Self-Attention Transformer
        transformed = self.transformer(token_bag)

        # Global Set Pooling
        pool_q = self.pool_query.expand(B, -1, -1)
        state_rep, _ = self.pool_attn(pool_q, transformed, transformed)
        return state_rep.squeeze(1)  # [B, d_model]

    def encode_action_tokens(
        self,
        action_tokens: torch.Tensor  # [B, K, 3] (action_type, target_or_card_id, sub_idx)
    ) -> torch.Tensor:
        atype_emb = self.action_type_embed(action_tokens[:, :, 0])
        card_target_emb = self.card_embed(action_tokens[:, :, 1])
        act_combined = torch.cat([atype_emb, card_target_emb], dim=-1)
        return self.action_proj(act_combined)  # [B, K, d_model]

    def forward(
        self,
        context: torch.Tensor,
        hand: torch.Tensor,
        deck: torch.Tensor,
        enemies: torch.Tensor,
        relics: torch.Tensor,
        action_tokens: torch.Tensor  # [B, K, 3]
    ) -> Dict[str, torch.Tensor]:
        B, K, _ = action_tokens.shape

        # 1. State Representation: [B, d_model]
        state_rep = self.encode_state(context, hand, deck, enemies, relics)

        # 2. Action Embeddings: [B, K, d_model]
        act_emb = self.encode_action_tokens(action_tokens)

        # 3. Action-Conditioned Fusion: [B, K, d_model]
        state_rep_expanded = state_rep.unsqueeze(1).expand(-1, K, -1)
        fusion_input = torch.cat([state_rep_expanded, act_emb], dim=-1)
        sa_rep = self.action_fusion(fusion_input)

        # 4. Multi-Task Heads
        v_win = self.head_win(sa_rep).squeeze(-1)                      # [B, K]
        v_hp_loss = self.head_hp_loss(sa_rep).squeeze(-1)              # [B, K]
        v_relic_ev = self.head_relic_ev(sa_rep).squeeze(-1)            # [B, K]
        v_boss_readiness = self.head_boss_readiness(sa_rep).squeeze(-1)# [B, K]

        # 5. Skip-Relative Advantage (Advantage relative to action index 0 or skip action)
        skip_baseline = v_win[:, :1].expand(-1, K)
        advantage = v_win - skip_baseline

        return {
            "v_win": v_win,
            "v_hp_loss": v_hp_loss,
            "v_relic_ev": v_relic_ev,
            "v_boss_readiness": v_boss_readiness,
            "advantage": advantage,
            "state_rep": state_rep
        }
