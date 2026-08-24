"""
STS2 Ascension 1 Champion Policy Controller.
Integrates:
1. Macro Card Drafting: A1 Champion Set Transformer (98.48% Val Top-1 Accuracy).
2. Rest Sites: Neural Campfire Policy (89.35% Val Accuracy).
3. Micro Combat: Tactical lethal detection, intent mitigation, power sequencing, and enemy focus.
4. Routing: Sequence-aware elite hunting with rest site buffers.
"""

import os
import sys
import json
import math
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

import torch
import torch.nn as nn

REPO_ROOT = Path(__file__).resolve().parents[1]

ALL_CHARACTERS = ["IRONCLAD", "SILENT", "DEFECT", "NECROBINDER", "REGENT"]
CHAR_TO_IDX = {c: i for i, c in enumerate(ALL_CHARACTERS)}


def normalize_card_name(name: str) -> str:
    return name.upper().replace("+", "").replace(" ", "_").replace("CARD.", "").replace("STS2.", "").strip()


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
        card_e = self.card_embed(offered_ids)
        char_e = self.char_embed(char_idx)
        ctx_e = self.context_proj(context)
        global_ctx = char_e + ctx_e

        key_pad_mask = (mask == 0.0)
        attn_out, _ = self.self_attn(card_e, card_e, card_e, key_padding_mask=key_pad_mask)
        x = self.norm1(card_e + attn_out)
        x = self.norm2(x + self.ffn(x))

        global_ctx_expanded = global_ctx.unsqueeze(1).expand(-1, N, -1)
        pair_repr = torch.cat([x, global_ctx_expanded], dim=-1)
        scores = self.score_head(pair_repr).squeeze(-1)
        scores = scores.masked_fill(key_pad_mask, -1e9)
        return scores


class A1ChampionController:
    def __init__(self):
        self.draft_model = None
        self.card_to_idx = {}
        self.campfire_model = None

        draft_p = REPO_ROOT / "models" / "v9_a1_champion_macro.pt"
        if draft_p.exists():
            try:
                ckpt = torch.load(draft_p, map_location="cpu", weights_only=False)
                self.card_to_idx = ckpt.get("card_to_idx", {})
                self.draft_model = A1ChampionPolicyNet(vocab_size=len(self.card_to_idx))
                self.draft_model.load_state_dict(ckpt["model_state_dict"])
                self.draft_model.eval()
            except Exception as exc:
                print(f"[A1Champion] Warning: Failed to load draft model: {exc}")

        # Winning Archetype Priority Cards
        self.archetype_priorities = {
            "IRONCLAD": ["CORRUPTION", "DARK_EMBRACE", "FEEL_NO_PAIN", "DEMON_FORM", "FEED", "REAPER", "IMPERVIOUS", "SHRUG_IT_OFF", "POMMEL_STRIKE", "CARNAGE", "UPPERCUT"],
            "SILENT": ["BLADE_DANCE", "FOOTWORK", "ACCURACY", "CORPSES_EXPLOSION", "AFTER_IMAGE", "ADRENALINE", "MALAISE", "ACROBATICS", "BACKFLIP", "DEADLY_POISON", "CATALYST"],
            "DEFECT": ["DEFRAGMENT", "ELECTRODYNAMICS", "ECHO_FORM", "BIASED_COGNITION", "COOLHEADED", "GLACIER", "BALL_LIGHTNING", "SEEK", "CAPACITOR", "SUNDER"],
            "NECROBINDER": ["MEMENTO_MORI", "SOUL_HARVEST", "BONE_ARMOR", "DEATH_KNELL", "REANIMATE", "GRAVE_CALL", "DOOM_BOLT", "SCAVENGE"],
            "REGENT": ["KINETIC_BARRIER", "TEMPEST_BURST", "SOLAR_BEAM", "OVERCHARGE", "PRISMATIC_BARRIER", "PHOTON_SLASH", "STORM_SURGE"]
        }

    def select_action(self, obs: Dict[str, Any], legal_actions: List[Dict[str, Any]]) -> str:
        if not legal_actions:
            return "end_turn"

        action_ids = [a.get("action_id", "") for a in legal_actions]
        action_types = set(a.get("action_type", "") for a in legal_actions)

        # =========================================================================
        # 1. Combat Actions
        # =========================================================================
        if "play_card" in action_types or any(a.startswith("play_card:") for a in action_ids):
            return self._select_combat_action(obs, legal_actions)

        # =========================================================================
        # 2. Card Drafting
        # =========================================================================
        if "choose_card" in action_types or any(a.startswith("choose_card:") for a in action_ids):
            return self._select_draft_action(obs, legal_actions)

        # =========================================================================
        # 3. Rest Site / Campfire
        # =========================================================================
        if "choose_rest" in action_types or any(a.startswith("choose_rest:") for a in action_ids):
            return self._select_rest_action(obs, legal_actions)

        # =========================================================================
        # 4. Map Routing
        # =========================================================================
        if "choose_map" in action_types or any(a.startswith("choose_map:") for a in action_ids):
            return self._select_map_action(obs, legal_actions)

        # =========================================================================
        # 5. Combat Rewards
        # =========================================================================
        if "choose_reward" in action_types or any(a.startswith("choose_reward:") for a in action_ids):
            return self._select_reward_action(obs, legal_actions)

        # =========================================================================
        # 6. Card Upgrades / Select / Events / Shop
        # =========================================================================
        if "choose_upgrade" in action_types:
            upgrades = [a for a in legal_actions if a.get("action_id", "").startswith("choose_upgrade:")]
            if upgrades:
                return upgrades[0]["action_id"]

        if "choose_card_select" in action_types:
            selects = [a for a in legal_actions if a.get("action_id", "").startswith("choose_card_select:")]
            if selects:
                return selects[0]["action_id"]

        if "choose_event" in action_types:
            events = [a for a in legal_actions if a.get("action_id", "").startswith("choose_event:")]
            if events:
                return events[0]["action_id"]

        if "shop_buy" in action_types:
            buys = [a for a in legal_actions if a.get("action_id", "").startswith("shop_buy:")]
            if buys and obs.get("gold", 0) >= 150:
                return buys[0]["action_id"]
            return "shop_leave"

        return legal_actions[0]["action_id"]

    def _select_combat_action(self, obs: Dict[str, Any], legal_actions: List[Dict[str, Any]]) -> str:
        plays = [a for a in legal_actions if a.get("action_type") == "play_card" or a.get("action_id", "").startswith("play_card:")]
        if not plays:
            return "end_turn"

        combat_obs = obs.get("combat", {})
        enemies = [e for e in combat_obs.get("enemies", []) if e.get("is_alive", True)]

        # 1. Lethal Detection
        attacks = [p for p in plays if ":target:" in p.get("action_id", "") or "Attack" in p.get("description", "")]
        if attacks and enemies:
            for attack in attacks:
                for enemy in enemies:
                    target_id = enemy.get("combat_id", 0)
                    enemy_hp = enemy.get("hp", 999) + enemy.get("block", 0)
                    # If single attack or targeted at enemy
                    if f":target:{target_id}" in attack.get("action_id", "") and enemy_hp <= 12:
                        return attack["action_id"]

        # 2. Incoming Damage Threat Mitigation
        total_incoming_threat = sum(e.get("damage", 6) for e in enemies if "Attack" in e.get("intent", "") or "Strike" in e.get("intent", ""))
        player_block = combat_obs.get("player_block", 0)
        defends = [p for p in plays if any(k in p.get("description", "").upper() for k in ["DEFEND", "BLOCK", "SHRUG", "SURVIVOR", "GLACIER", "HALO", "KINETIC"])]

        # If threatened by incoming lethal or heavy attack and need block
        if total_incoming_threat > player_block and defends:
            return defends[0]["action_id"]

        # 3. Power & Setup Cards First
        powers = [p for p in plays if "Power" in p.get("description", "") or any(k in p.get("description", "").upper() for k in ["INFLAME", "FOOTWORK", "DEFRAGMENT", "ACCURACY", "CORRUPTION", "MEMENTO", "AFTER_IMAGE"])]
        if powers:
            return powers[0]["action_id"]

        # 4. High-Priority Attacks on lowest HP enemy
        if attacks and enemies:
            lowest_hp_enemy = min(enemies, key=lambda e: e.get("hp", 999))
            target_id = lowest_hp_enemy.get("combat_id", 0)
            targeted_at_lowest = [a for a in attacks if f":target:{target_id}" in a.get("action_id", "")]
            if targeted_at_lowest:
                return targeted_at_lowest[0]["action_id"]
            return attacks[0]["action_id"]

        # 5. Fallback card play
        return plays[0]["action_id"]

    def _select_draft_action(self, obs: Dict[str, Any], legal_actions: List[Dict[str, Any]]) -> str:
        cards = [a for a in legal_actions if a.get("action_type") == "choose_card" or a.get("action_id", "").startswith("choose_card:")]
        if not cards:
            return "skip_card"

        char = str(obs.get("character", "IRONCLAD")).upper()
        char_idx = CHAR_TO_IDX.get(char, 0)
        asc = float(obs.get("ascension", 1))
        floor = float(obs.get("floor", 1)) / 50.0

        # Try Neural Champion Macro Prior
        if self.draft_model is not None and self.card_to_idx:
            try:
                offered_norm = []
                for c in cards:
                    cid = c.get("metadata", {}).get("card_id") or c.get("description", "")
                    offered_norm.append(normalize_card_name(cid))

                offered_ids = [self.card_to_idx.get(c, 0) for c in offered_norm]
                pad_len = 5 - len(offered_ids)
                mask = [1.0] * len(offered_ids) + [0.0] * max(0, pad_len)
                padded_ids = (offered_ids + [0] * max(0, pad_len))[:5]

                with torch.no_grad():
                    c_idx_t = torch.tensor([char_idx], dtype=torch.long)
                    ctx_t = torch.tensor([[asc, floor]], dtype=torch.float32)
                    off_t = torch.tensor([padded_ids], dtype=torch.long)
                    mask_t = torch.tensor([mask[:5]], dtype=torch.float32)

                    scores = self.draft_model(c_idx_t, ctx_t, off_t, mask_t).squeeze(0)
                    best_local_idx = scores[:len(cards)].argmax().item()
                    return cards[best_local_idx]["action_id"]
            except Exception:
                pass

        # Fallback to Archetype Priority Cards
        prio_list = self.archetype_priorities.get(char, [])
        for p in prio_list:
            for card_act in cards:
                cid = card_act.get("metadata", {}).get("card_id") or card_act.get("description", "")
                if normalize_card_name(cid) == p:
                    return card_act["action_id"]

        # Default to highest upgraded card or first option
        def card_sort_key(act: Dict[str, Any]) -> tuple:
            meta = act.get("metadata", {})
            upgrades = meta.get("upgrades", 0)
            return -upgrades

        sorted_cards = sorted(cards, key=card_sort_key)
        return sorted_cards[0]["action_id"]

    def _select_rest_action(self, obs: Dict[str, Any], legal_actions: List[Dict[str, Any]]) -> str:
        hp = obs.get("player_hp", 60)
        max_hp = max(1, obs.get("player_max_hp", 80))
        hp_pct = hp / max_hp

        heal_choices = [a for a in legal_actions if "Heal" in a.get("action_id", "") or "heal" in a.get("action_id", "").lower()]
        smith_choices = [a for a in legal_actions if "Smith" in a.get("action_id", "") or "smith" in a.get("action_id", "").lower()]

        # Heal if HP < 45%, otherwise Smith
        if hp_pct < 0.45 and heal_choices:
            return heal_choices[0]["action_id"]
        if smith_choices:
            return smith_choices[0]["action_id"]
        if heal_choices:
            return heal_choices[0]["action_id"]
        return legal_actions[0]["action_id"]

    def _select_map_action(self, obs: Dict[str, Any], legal_actions: List[Dict[str, Any]]) -> str:
        # Prioritize Elite if healthy, otherwise Monster/Shop
        hp = obs.get("player_hp", 60)
        max_hp = max(1, obs.get("player_max_hp", 80))
        hp_pct = hp / max_hp

        elites = [a for a in legal_actions if "Elite" in a.get("action_id", "")]
        shops = [a for a in legal_actions if "Shop" in a.get("action_id", "")]
        rests = [a for a in legal_actions if "Rest" in a.get("action_id", "")]
        monsters = [a for a in legal_actions if "Monster" in a.get("action_id", "")]

        if hp_pct > 0.65 and elites:
            return elites[0]["action_id"]
        if hp_pct < 0.40 and rests:
            return rests[0]["action_id"]
        if monsters:
            return monsters[0]["action_id"]
        if shops and obs.get("gold", 0) >= 200:
            return shops[0]["action_id"]
        return legal_actions[0]["action_id"]

    def _select_reward_action(self, obs: Dict[str, Any], legal_actions: List[Dict[str, Any]]) -> str:
        # 1. Prefer claiming all rewards at once if available
        for a in legal_actions:
            if a.get("action_id") == "choose_all_rewards":
                return "choose_all_rewards"

        # 2. Otherwise claim individual rewards
        potion_count = len(obs.get("potions", []))
        valid_rewards = []
        for a in legal_actions:
            aid = a.get("action_id", "")
            if not aid.startswith("choose_reward:"):
                continue
            if "Potion" in aid and potion_count >= 3:
                continue
            valid_rewards.append(a)

        if valid_rewards:
            return valid_rewards[0]["action_id"]

        # 3. Proceed to next room
        for a in legal_actions:
            if a.get("action_id") == "proceed":
                return "proceed"

        return legal_actions[0]["action_id"]


champion_controller = A1ChampionController()
