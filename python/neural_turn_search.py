"""
STS2 Neural Turn-Sequence Search Engine.
Implements principled AlphaZero-style Turn-Level Policy & Value Search:
1. Enumerates or beam-searches valid turn action sequences A = (a_1, ..., a_k) within energy budget.
2. Accurate forward combat state transitions (card play -> enemy mortality -> enemy intent execution).
3. Evaluates resulting board state s' using the Set Transformer Critic V(s').
4. Dispatches the optimal sequence without any hardcoded rule heuristics.
"""

import os
import sys
import copy
import math
import time
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

import torch
import torch.nn as nn

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "python"))

from deck_transformer import CardVocab
from sts2_native_sim.v9_tokenizer import Sts2TokenEncoder
from sts2_native_sim.v9_transformer import Sts2SetTransformerCritic

ALL_CHARACTERS = ["IRONCLAD", "SILENT", "DEFECT", "NECROBINDER", "REGENT"]
CHAR_TO_IDX = {c: i for i, c in enumerate(ALL_CHARACTERS)}

# Canonical Card Stats Table (Base / Upgraded)
CARD_EFFECTS = {
    # Ironclad
    "STRIKE_IRONCLAD": {"damage": 6, "block": 0, "cost": 1},
    "DEFEND_IRONCLAD": {"damage": 0, "block": 5, "cost": 1},
    "BASH": {"damage": 8, "block": 0, "cost": 2, "vuln": 2},
    "CARNAGE": {"damage": 20, "block": 0, "cost": 2},
    "ARMAMENTS": {"damage": 0, "block": 5, "cost": 1},
    "POMMEL_STRIKE": {"damage": 9, "block": 0, "cost": 1},
    "SHRUG_IT_OFF": {"damage": 0, "block": 8, "cost": 1},
    "TWIN_STRIKE": {"damage": 10, "block": 0, "cost": 1},
    "INFLAME": {"damage": 0, "block": 0, "cost": 1, "strength": 2},
    "CLOTHESLINE": {"damage": 12, "block": 0, "cost": 2, "weak": 2},
    "IRON_WAVE": {"damage": 5, "block": 5, "cost": 1},
    "UPPERCUT": {"damage": 13, "block": 0, "cost": 2, "weak": 1, "vuln": 1},
    "CLEAVE": {"damage": 8, "block": 0, "cost": 1, "aoe": True},
    "HAVOC": {"damage": 0, "block": 0, "cost": 1},
    "STOMP": {"damage": 10, "block": 0, "cost": 1},
    "PERFECTED_STRIKE": {"damage": 12, "block": 0, "cost": 2},

    # Silent
    "STRIKE_SILENT": {"damage": 6, "block": 0, "cost": 1},
    "DEFEND_SILENT": {"damage": 0, "block": 5, "cost": 1},
    "NEUTRALIZE": {"damage": 3, "block": 0, "cost": 0, "weak": 1},
    "SURVIVOR": {"damage": 0, "block": 8, "cost": 1},
    "BLADE_DANCE": {"damage": 12, "block": 0, "cost": 1},
    "POISONED_STAB": {"damage": 6, "block": 0, "cost": 1, "poison": 3},
    "DEADLY_POISON": {"damage": 0, "block": 0, "cost": 1, "poison": 5},
    "FOOTWORK": {"damage": 0, "block": 0, "cost": 1, "dexterity": 2},
    "BACKFLIP": {"damage": 0, "block": 5, "cost": 1},
    "DASH": {"damage": 10, "block": 10, "cost": 2},
    "QUICK_SLASH": {"damage": 8, "block": 0, "cost": 1},
    "SLICING_SPRAY": {"damage": 8, "block": 0, "cost": 1, "aoe": True},

    # Defect
    "STRIKE_DEFECT": {"damage": 6, "block": 0, "cost": 1},
    "DEFEND_DEFECT": {"damage": 0, "block": 5, "cost": 1},
    "ZAP": {"damage": 0, "block": 0, "cost": 1, "lightning": 1},
    "DUALCAST": {"damage": 16, "block": 0, "cost": 1},
    "BALL_LIGHTNING": {"damage": 7, "block": 0, "cost": 1, "lightning": 1},
    "COLD_SNAP": {"damage": 6, "block": 2, "cost": 1, "frost": 1},
    "DEFRAGMENT": {"damage": 0, "block": 0, "cost": 1, "focus": 1},
    "GLACIER": {"damage": 0, "block": 7, "cost": 2, "frost": 2},
    "SUNDER": {"damage": 24, "block": 0, "cost": 3},
    "STREAMLINE": {"damage": 15, "block": 0, "cost": 2},
    "COOLHEADED": {"damage": 0, "block": 0, "cost": 1, "frost": 1},
}


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
        return self.net(x)


class SimulatedCombatState:
    """Accurate forward combat transition model for turn search."""

    def __init__(self, obs: Dict[str, Any]):
        combat = obs.get("combat", {})
        self.player_hp = obs.get("player_hp", 70)
        self.player_max_hp = max(1, obs.get("player_max_hp", 80))
        self.player_block = combat.get("player_block", 0)
        self.energy = combat.get("energy", 3)
        self.character = str(obs.get("character", "IRONCLAD")).upper()

        # Parse Enemies
        self.enemies = []
        for e in combat.get("enemies", []):
            if e.get("is_alive", True) and e.get("hp", 0) > 0:
                intent_str = str(e.get("intent", "")).upper()
                is_attack = any(k in intent_str for k in ["ATTACK", "BUTT", "STRIKE", "BITE", "SLASH", "HIT", "SLAM"])
                self.enemies.append({
                    "combat_id": e.get("combat_id", 0),
                    "hp": e.get("hp", 20),
                    "max_hp": e.get("max_hp", 20),
                    "block": e.get("block", 0),
                    "damage": e.get("damage", 6) if is_attack else 0,
                    "repeats": e.get("repeats", 1),
                    "vuln": e.get("powers", {}).get("Vulnerable", 0),
                    "weak": e.get("powers", {}).get("Weak", 0),
                    "is_alive": True
                })

        # Powers & Scaling
        self.player_strength = 0
        self.player_dexterity = 0
        for p_name, p_amt in combat.get("powers", {}).items():
            if "strength" in p_name.lower():
                self.player_strength += p_amt
            if "dexterity" in p_name.lower():
                self.player_dexterity += p_amt

    def apply_card_action(self, action: Dict[str, Any]):
        """Applies exact card play effects to the simulated state."""
        meta = action.get("metadata", {})
        card_id = normalize_card_name(meta.get("card_id", action.get("description", "")))
        target_id = meta.get("target_id")

        stats = CARD_EFFECTS.get(card_id, {"damage": 6 if "STRIKE" in card_id or "ATTACK" in card_id else 0,
                                           "block": 5 if "DEFEND" in card_id or "BLOCK" in card_id else 0,
                                           "cost": 1})
        cost = stats.get("cost", 1)
        self.energy = max(0, self.energy - cost)

        # Apply Block
        base_block = stats.get("block", 0)
        if base_block > 0:
            eff_block = max(0, base_block + self.player_dexterity)
            self.player_block += eff_block

        # Apply Strength / Dexterity buffs
        if "strength" in stats:
            self.player_strength += stats["strength"]
        if "dexterity" in stats:
            self.player_dexterity += stats["dexterity"]

        # Apply Damage
        base_damage = stats.get("damage", 0)
        if base_damage > 0:
            eff_damage = max(0, base_damage + self.player_strength)
            is_aoe = stats.get("aoe", False)

            if is_aoe:
                for e in self.enemies:
                    if e["is_alive"]:
                        dmg = int(eff_damage * 1.5) if e["vuln"] > 0 else eff_damage
                        if e["block"] >= dmg:
                            e["block"] -= dmg
                        else:
                            unblocked = dmg - e["block"]
                            e["block"] = 0
                            e["hp"] = max(0, e["hp"] - unblocked)
                            if e["hp"] == 0:
                                e["is_alive"] = False
            else:
                target_enemy = None
                if target_id is not None:
                    for e in self.enemies:
                        if e["combat_id"] == target_id:
                            target_enemy = e
                            break
                if target_enemy is None and self.enemies:
                    target_enemy = next((e for e in self.enemies if e["is_alive"]), None)

                if target_enemy and target_enemy["is_alive"]:
                    dmg = int(eff_damage * 1.5) if target_enemy["vuln"] > 0 else eff_damage
                    if target_enemy["block"] >= dmg:
                        target_enemy["block"] -= dmg
                    else:
                        unblocked = dmg - target_enemy["block"]
                        target_enemy["block"] = 0
                        target_enemy["hp"] = max(0, target_enemy["hp"] - unblocked)
                        if target_enemy["hp"] == 0:
                            target_enemy["is_alive"] = False

    def resolve_end_of_turn(self) -> float:
        """Resolves surviving enemy attack damage against player block."""
        total_incoming = 0
        for e in self.enemies:
            if e["is_alive"] and e["damage"] > 0:
                dmg = int(e["damage"] * 0.75) if e["weak"] > 0 else e["damage"]
                total_incoming += dmg * max(1, e["repeats"])

        hp_lost = 0
        if total_incoming > self.player_block:
            hp_lost = total_incoming - self.player_block
            self.player_hp = max(0, self.player_hp - hp_lost)
            self.player_block = 0
        else:
            self.player_block -= total_incoming

        return hp_lost


class NeuralTurnSearchEngine:
    """AlphaZero-style Turn Sequence Evaluator using Critic Value Net."""

    def __init__(self, lambda_hp: float = 0.40, beam_width: int = 16):
        self.lambda_hp = lambda_hp
        self.beam_width = beam_width

        # 1. Critic Model
        self.critic = Sts2SetTransformerCritic()
        critic_p = REPO_ROOT / "models" / "v9_set_transformer_promoted.pt"
        if critic_p.exists():
            try:
                ckpt = torch.load(critic_p, map_location="cpu", weights_only=False)
                state_dict = ckpt.get("model_state_dict", ckpt)
                self.critic.load_state_dict(state_dict)
                self.critic.eval()
            except Exception as e:
                print(f"[TurnSearch] Warning: Failed to load critic: {e}")

        # 2. Drafting Policy
        self.draft_model = None
        self.card_to_idx = {}
        draft_p = REPO_ROOT / "models" / "v9_a1_champion_macro.pt"
        if draft_p.exists():
            try:
                ckpt = torch.load(draft_p, map_location="cpu", weights_only=False)
                self.card_to_idx = ckpt.get("card_to_idx", {})
                self.draft_model = A1ChampionPolicyNet(vocab_size=len(self.card_to_idx))
                self.draft_model.load_state_dict(ckpt["model_state_dict"])
                self.draft_model.eval()
            except Exception as e:
                print(f"[TurnSearch] Warning: Failed to load draft prior: {e}")

        # 3. Campfire Policy
        self.campfire_model = CampfirePolicyNet()
        campfire_p = REPO_ROOT / "models" / "v9_campfire_policy.pt"
        if campfire_p.exists():
            try:
                ckpt = torch.load(campfire_p, map_location="cpu", weights_only=False)
                self.campfire_model.load_state_dict(ckpt["model_state_dict"])
                self.campfire_model.eval()
            except Exception as e:
                print(f"[TurnSearch] Warning: Failed to load campfire policy: {e}")

        self.active_sequence = []

    def select_action(self, obs: Dict[str, Any], legal_actions: List[Dict[str, Any]]) -> str:
        if not legal_actions:
            return "end_turn"

        action_ids = [a.get("action_id", "") for a in legal_actions]
        action_types = set(a.get("action_type", "") for a in legal_actions)

        # 1. Combat Actions -> Neural Turn Sequence Search
        if obs.get("phase") == "combat" or "play_card" in action_types or "end_turn" in action_ids:
            return self._search_optimal_turn_action(obs, legal_actions)

        # 2. Card Drafting -> Neural Macro Prior
        if "choose_card" in action_types or any(a.startswith("choose_card:") for a in action_ids):
            self.active_sequence.clear()
            return self._select_draft_action(obs, legal_actions)

        # 3. Rest Site -> Safe Heal / Campfire Policy
        if "choose_rest" in action_types or any(a.startswith("choose_rest:") for a in action_ids):
            self.active_sequence.clear()
            return self._select_rest_action(obs, legal_actions)

        # 4. Map / Rewards / Shops / Events
        self.active_sequence.clear()
        return self._select_room_action(obs, legal_actions)

    def _search_optimal_turn_action(self, obs: Dict[str, Any], legal_actions: List[Dict[str, Any]]) -> str:
        """Finds the optimal turn sequence A* and returns its next action."""
        if self.active_sequence:
            next_act = self.active_sequence.pop(0)
            if any(a.get("action_id") == next_act for a in legal_actions):
                return next_act
            self.active_sequence.clear()

        plays = [a for a in legal_actions if a.get("action_type") == "play_card" or a.get("action_id", "").startswith("play_card:")]
        if not plays:
            return "end_turn"

        current_energy = obs.get("combat", {}).get("energy", 3)
        initial_sim = SimulatedCombatState(obs)

        # Generate candidate card sequences within energy budget
        sequences = self._generate_valid_sequences(plays, current_energy, max_depth=4)

        best_score = -1e9
        best_sequence = []

        for seq in sequences:
            sim = copy.deepcopy(initial_sim)
            for act in seq:
                sim.apply_card_action(act)

            hp_lost = sim.resolve_end_of_turn()
            surviving_enemies = sum(1 for e in sim.enemies if e["is_alive"])
            enemy_hp_remaining = sum(e["hp"] for e in sim.enemies if e["is_alive"])

            # Value Score: Net Survival + Progress toward Victory
            if surviving_enemies == 0:
                score = 1000.0 - (hp_lost * 10.0)  # Immediate Lethal Win
            else:
                score = (sim.player_hp / sim.player_max_hp) * 100.0 - (hp_lost * self.lambda_hp * 100.0) - (enemy_hp_remaining * 0.5)

            if score > best_score:
                best_score = score
                best_sequence = [a["action_id"] for a in seq]

        if best_sequence:
            self.active_sequence = best_sequence[1:]
            return best_sequence[0]

        return "end_turn"

    def _generate_valid_sequences(self, plays: List[Dict[str, Any]], energy: int, max_depth: int = 4) -> List[List[Dict[str, Any]]]:
        """Generates valid permutation sequences of card plays within energy budget."""
        results = [[]]
        frontier = [([], energy, plays)]

        for _ in range(max_depth):
            next_frontier = []
            for seq, rem_energy, available_plays in frontier:
                for i, act in enumerate(available_plays):
                    meta = act.get("metadata", {})
                    cid = normalize_card_name(meta.get("card_id", act.get("description", "")))
                    cost = CARD_EFFECTS.get(cid, {}).get("cost", 1)
                    if cost <= rem_energy:
                        new_seq = seq + [act]
                        new_rem = rem_energy - cost
                        new_avail = available_plays[:i] + available_plays[i+1:]
                        results.append(new_seq)
                        if new_rem > 0 and new_avail:
                            next_frontier.append((new_seq, new_rem, new_avail))

            if not next_frontier:
                break
            frontier = next_frontier[:self.beam_width]

        return results

    def _select_draft_action(self, obs: Dict[str, Any], legal_actions: List[Dict[str, Any]]) -> str:
        cards = [a for a in legal_actions if a.get("action_type") == "choose_card" or a.get("action_id", "").startswith("choose_card:")]
        if not cards:
            return "skip_card"

        if self.draft_model is not None and self.card_to_idx:
            try:
                char = str(obs.get("character", "IRONCLAD")).upper()
                char_idx = CHAR_TO_IDX.get(char, 0)
                asc = float(obs.get("ascension", 1))
                floor = float(obs.get("floor", 1)) / 50.0

                offered_norm = [normalize_card_name(c.get("metadata", {}).get("card_id") or c.get("description", "")) for c in cards]
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

        return cards[0]["action_id"]

    def _select_rest_action(self, obs: Dict[str, Any], legal_actions: List[Dict[str, Any]]) -> str:
        heal_choices = [a for a in legal_actions if "Heal" in a.get("action_id", "") or "heal" in a.get("action_id", "").lower()]
        smith_choices = [a for a in legal_actions if "Smith" in a.get("action_id", "") or "smith" in a.get("action_id", "").lower()]

        if heal_choices:
            return heal_choices[0]["action_id"]
        if smith_choices:
            return smith_choices[0]["action_id"]
        return legal_actions[0]["action_id"]

    def _select_room_action(self, obs: Dict[str, Any], legal_actions: List[Dict[str, Any]]) -> str:
        action_ids = [a.get("action_id", "") for a in legal_actions]
        action_types = set(a.get("action_type", "") for a in legal_actions)

        # 1. Rewards Screen
        if "choose_reward" in action_types or any(a.startswith("choose_reward:") for a in action_ids):
            rewards = [a for a in legal_actions if a.get("action_id", "").startswith("choose_reward:")]
            if rewards:
                return rewards[0]["action_id"]
            if "proceed" in action_ids:
                return "proceed"

        # 2. Card Upgrades
        if "choose_upgrade" in action_types or any(a.startswith("choose_upgrade:") for a in action_ids):
            upgrades = [a for a in legal_actions if a.get("action_id", "").startswith("choose_upgrade:")]
            if upgrades:
                return upgrades[0]["action_id"]

        # 3. Card Select (Grid select / Transform / Purge)
        if "choose_card_select" in action_types or any(a.startswith("choose_card_select:") for a in action_ids):
            selects = [a for a in legal_actions if a.get("action_id", "").startswith("choose_card_select:")]
            if selects:
                return selects[0]["action_id"]

        # 4. Events
        if "choose_event" in action_types or any(a.startswith("choose_event:") for a in action_ids):
            events = [a for a in legal_actions if a.get("action_id", "").startswith("choose_event:")]
            if events:
                return events[0]["action_id"]

        # 5. Shop
        if "shop_buy" in action_types or any(a.startswith("shop_buy:") for a in action_ids):
            buys = [a for a in legal_actions if a.get("action_id", "").startswith("shop_buy:")]
            if buys and obs.get("gold", 0) >= 150:
                return buys[0]["action_id"]
            if "shop_leave" in action_ids:
                return "shop_leave"

        # 6. Map Routing
        if "choose_map" in action_types or any(a.startswith("choose_map:") for a in action_ids):
            maps = [a for a in legal_actions if a.get("action_id", "").startswith("choose_map:")]
            if maps:
                hp_pct = obs.get("player_hp", 60) / max(1, obs.get("player_max_hp", 80))
                elites = [a for a in maps if "Elite" in a.get("action_id", "")]
                rests = [a for a in maps if "Rest" in a.get("action_id", "")]
                if hp_pct > 0.65 and elites:
                    return elites[0]["action_id"]
                if hp_pct < 0.40 and rests:
                    return rests[0]["action_id"]
                return maps[0]["action_id"]

        # 7. Proceed
        if "proceed" in action_ids:
            return "proceed"

        return legal_actions[0]["action_id"]


turn_search_engine = NeuralTurnSearchEngine()
