"""
v9 Tokenizer for STS2 Action-Conditioned Set Transformer.
Encodes cards, creatures, intents, relics, and candidate actions into structured tensor bags.
Strictly POMDP-compliant: unrevealed draw pile is encoded as a permutation-invariant bag without sequential leakage.
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


class Sts2TokenEncoder:
    """Canonical tokenizer for STS2 full-game states and actions."""

    ACTION_TYPES = [
        "PAD", "play_card", "use_potion", "end_turn", "choose_card",
        "skip_card", "choose_rest", "choose_map", "choose_reward",
        "choose_all_rewards", "choose_event", "shop_buy", "shop_leave", "proceed"
    ]
    action_to_idx = {a: i for i, a in enumerate(ACTION_TYPES)}

    INTENT_TYPES = [
        "NONE", "Attack", "Defend", "Buff", "Debuff", "AttackDefend",
        "AttackBuff", "AttackDebuff", "Escape", "Special", "Unknown"
    ]
    intent_to_idx = {it: i for i, it in enumerate(INTENT_TYPES)}

    @classmethod
    def encode_action(cls, action_dict_or_str: Any) -> Tuple[int, int, int]:
        """Encodes an action into (action_type_idx, card_or_target_id, sub_index)."""
        if isinstance(action_dict_or_str, dict):
            aid = action_dict_or_str.get("action_id", "")
            atype = action_dict_or_str.get("action_type", "")
        else:
            aid = str(action_dict_or_str)
            atype = aid.split(":")[0] if ":" in aid else aid

        atype_idx = cls.action_to_idx.get(atype, cls.action_to_idx.get("proceed", 13))

        target_or_card = 0
        sub_idx = 0

        parts = aid.split(":")
        if atype == "play_card" and len(parts) >= 2:
            sub_idx = int(parts[1]) if parts[1].isdigit() else 0
            if ":target:" in aid and len(parts) >= 4:
                target_or_card = int(parts[3]) if parts[3].isdigit() else 0
        elif atype == "choose_card" and len(parts) >= 3:
            sub_idx = int(parts[1]) if parts[1].isdigit() else 0
            c_name = CardVocab.normalize_card_name(parts[2])
            target_or_card = CardVocab.card_to_idx.get(c_name, 1)
        elif atype == "choose_map" and len(parts) >= 2:
            sub_idx = int(parts[1]) if parts[1].isdigit() else 1

        return atype_idx, target_or_card, sub_idx

    @classmethod
    def tokenize_observation(
        cls,
        obs: Dict[str, Any],
        max_hand: int = 10,
        max_deck: int = 40,
        max_enemies: int = 5,
        max_relics: int = 15
    ) -> Dict[str, torch.Tensor]:
        """
        Transforms a JSON Observation snapshot into structured POMDP-compliant tensors.
        """
        # 1. Global Context Vector: [hp_norm, max_hp_norm, block_norm, energy_norm, gold_norm, floor_norm, asc_norm, char_onehot (5)] -> 12 dims
        hp = float(obs.get("player_hp", 70))
        max_hp = max(1.0, float(obs.get("player_max_hp", 80)))
        block = float(obs.get("player_block", 0))
        energy = float(obs.get("player_energy", 3))
        gold = float(obs.get("gold", 100))
        floor = float(obs.get("floor", 1))
        asc = float(obs.get("ascension", 0))
        char = str(obs.get("character", "IRONCLAD")).upper()

        char_idx = CardVocab.char_to_idx.get(char, 0)
        char_onehot = [1.0 if i == char_idx else 0.0 for i in range(len(CardVocab.CHARACTERS))]

        ctx_vec = [
            min(1.0, hp / max_hp),
            min(1.0, max_hp / 100.0),
            min(1.0, block / 50.0),
            min(1.0, energy / 5.0),
            min(1.0, gold / 500.0),
            min(1.0, floor / 50.0),
            min(1.0, asc / 20.0),
        ] + char_onehot

        # 2. Hand Cards: [max_hand, 3] (id, up, cost)
        combat = obs.get("combat", {}) or {}
        hand = combat.get("hand", [])
        hand_tokens = []
        for c in hand[:max_hand]:
            c_name = CardVocab.normalize_card_name(c.get("card_id", ""))
            c_idx = CardVocab.card_to_idx.get(c_name, 1)
            u_idx = 1 if c.get("upgrades", 0) > 0 else 0
            cost = max(0, min(5, int(c.get("cost", 1))))
            hand_tokens.append([c_idx, u_idx, cost])

        while len(hand_tokens) < max_hand:
            hand_tokens.append([0, 0, 0])

        # 3. Deck / Discard Bag (Permutation Invariant): [max_deck, 2] (id, up)
        deck_cards = obs.get("deck_cards", [])
        deck_tokens = []
        for c in deck_cards[:max_deck]:
            c_name = CardVocab.normalize_card_name(c)
            c_idx = CardVocab.card_to_idx.get(c_name, 1)
            u_idx = 1 if "+" in c else 0
            deck_tokens.append([c_idx, u_idx])

        while len(deck_tokens) < max_deck:
            deck_tokens.append([0, 0])

        # 4. Enemies: [max_enemies, 5] (model_id, hp_norm, block_norm, intent_type, intent_dmg)
        enemies = combat.get("enemies", [])
        enemy_tokens = []
        for e in enemies[:max_enemies]:
            e_hp = float(e.get("hp", 0))
            e_max_hp = max(1.0, float(e.get("max_hp", 30)))
            e_block = float(e.get("block", 0))
            intent_str = str(e.get("intent", "Unknown"))
            int_idx = cls.intent_to_idx.get(intent_str, cls.intent_to_idx["Unknown"])
            enemy_tokens.append([
                1,  # Creature active flag
                min(1.0, e_hp / e_max_hp),
                min(1.0, e_block / 50.0),
                int_idx,
                min(1.0, float(e.get("powers", {}).get("STRENGTH_POWER", 0)) / 10.0)
            ])

        while len(enemy_tokens) < max_enemies:
            enemy_tokens.append([0, 0.0, 0.0, 0, 0.0])

        # 5. Relics: [max_relics]
        relics = obs.get("relics", [])
        relic_tokens = [CardVocab.encode_relic(r) for r in relics[:max_relics]]
        while len(relic_tokens) < max_relics:
            relic_tokens.append(0)

        return {
            "context": torch.tensor(ctx_vec, dtype=torch.float32),
            "hand": torch.tensor(hand_tokens, dtype=torch.long),
            "deck": torch.tensor(deck_tokens, dtype=torch.long),
            "enemies": torch.tensor(enemy_tokens, dtype=torch.float32),
            "relics": torch.tensor(relic_tokens, dtype=torch.long)
        }
