"""Complete-state action-conditioned combat policy used by native rollouts."""

from __future__ import annotations

import torch
import torch.nn as nn


class V12CombatPolicyNet(nn.Module):
    def __init__(self, vocab_size: int, embed_dim: int = 128, num_heads: int = 4):
        super().__init__()
        self.entity_embed = nn.Embedding(vocab_size + 1, embed_dim, padding_idx=0)
        self.char_embed = nn.Embedding(5, embed_dim)
        self.context_proj = nn.Linear(12, embed_dim)
        self.hand_numeric = nn.Linear(5, embed_dim)
        self.enemy_numeric = nn.Linear(6, embed_dim)
        self.aux_numeric = nn.Linear(2, embed_dim)
        self.slot_embed = nn.Embedding(10, embed_dim)
        self.state_type_embed = nn.Embedding(6, embed_dim)
        self.action_type_embed = nn.Embedding(4, embed_dim)
        self.action_numeric = nn.Linear(5, embed_dim)

        layer = nn.TransformerEncoderLayer(
            d_model=embed_dim, nhead=num_heads, dim_feedforward=embed_dim * 3,
            dropout=0.1, activation="gelu", batch_first=True, norm_first=True,
        )
        self.encoder = nn.TransformerEncoder(layer, num_layers=4)
        self.cross_attention = nn.MultiheadAttention(embed_dim, num_heads, dropout=0.1, batch_first=True)
        self.score = nn.Sequential(
            nn.LayerNorm(embed_dim * 3), nn.Linear(embed_dim * 3, embed_dim),
            nn.GELU(), nn.Dropout(0.1), nn.Linear(embed_dim, 1),
        )

    def forward(
        self,
        char_idx: torch.Tensor,
        context: torch.Tensor,
        hand_ids: torch.Tensor,
        hand_numeric: torch.Tensor,
        enemy_ids: torch.Tensor,
        enemy_numeric: torch.Tensor,
        aux_ids: torch.Tensor,
        aux_numeric: torch.Tensor,
        state_mask: torch.Tensor,
        action_types: torch.Tensor,
        action_ids: torch.Tensor,
        action_slots: torch.Tensor,
        action_numeric: torch.Tensor,
        action_mask: torch.Tensor,
    ) -> torch.Tensor:
        batch, action_count = action_ids.shape
        char = self.char_embed(char_idx).unsqueeze(1) + self.state_type_embed.weight[0].view(1, 1, -1)
        ctx = self.context_proj(context).unsqueeze(1) + self.state_type_embed.weight[1].view(1, 1, -1)
        hand = self.entity_embed(hand_ids) + self.hand_numeric(hand_numeric) + self.state_type_embed.weight[2].view(1, 1, -1)
        enemy = (
            self.entity_embed(enemy_ids) + self.enemy_numeric(enemy_numeric)
            + self.slot_embed(enemy_numeric[:, :, 5].long().clamp(0, 9))
            + self.state_type_embed.weight[3].view(1, 1, -1)
        )
        aux = self.entity_embed(aux_ids) + self.aux_numeric(aux_numeric) + self.state_type_embed.weight[4].view(1, 1, -1)
        state = torch.cat([char, ctx, hand, enemy, aux], dim=1)
        encoded = self.encoder(state, src_key_padding_mask=~state_mask.bool())
        valid = state_mask.unsqueeze(-1)
        pooled = (encoded * valid).sum(1) / valid.sum(1).clamp_min(1.0)

        actions = (
            self.entity_embed(action_ids)
            + self.action_type_embed(action_types)
            + self.slot_embed(action_slots.clamp(0, 9))
            + self.action_numeric(action_numeric)
        )
        attended, _ = self.cross_attention(actions, encoded, encoded, key_padding_mask=~state_mask.bool())
        target_positions = action_slots.clamp(0, 9)
        target = self.slot_embed(target_positions)
        logits = self.score(torch.cat([attended, pooled.unsqueeze(1).expand(-1, action_count, -1), target], -1)).squeeze(-1)
        return logits.masked_fill(~action_mask.bool(), -1e9)
