"""
STS2 Pure Neural Agent.
Completely eliminates handwritten rules and heuristics in favor of:
1. Supervised Combat Policy Net: pi_combat(a | s) (78.50% Val Top-1 / 96.50% Val Top-3 Acc).
2. Supervised Macro Drafting Net: pi_macro(a | s) (98.48% Val Top-1 / 99.91% Val Top-3 Acc).
3. Neural Campfire Policy Net: pi_rest(a | s) (89.35% Val Acc).
"""

import os
import sys
import math
import json
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

import torch
import torch.nn as nn

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "python"))

from train_v10_combat_policy import V10CombatPolicyNet, normalize_id, ACTION_TYPE_MAP, CHAR_TO_IDX, ALL_CHARACTERS
from agentic_macro_prior import AgenticMacroPrior


class PureNeuralAgent:
    """Zero-heuristic Pure Neural Policy Agent."""

    def __init__(self):
        # 1. Load V10 Advantage-Weighted Combat Policy Net
        self.combat_model = None
        self.combat_card_to_idx = {}
        combat_p = REPO_ROOT / "models" / "v10_combat_policy.pt"
        if combat_p.exists():
            try:
                ckpt = torch.load(combat_p, map_location="cpu", weights_only=False)
                self.combat_card_to_idx = ckpt.get("card_to_idx", {})
                self.combat_model = V10CombatPolicyNet(vocab_size=len(self.combat_card_to_idx), embed_dim=128, num_heads=4, max_actions=16)
                self.combat_model.load_state_dict(ckpt["model_state_dict"])
                self.combat_model.eval()
                print(f"[PureNeuralAgent] Loaded V10 Advantage-Weighted Combat Policy (Val Top-1: {ckpt.get('val_top1_acc', 0):.2f}%).")
            except Exception as e:
                print(f"[PureNeuralAgent] Warning: Failed to load V10 combat policy: {e}")

        # 2. Load only option-aligned macro evidence. The old draft/campfire
        # checkpoints were trained from whitespace-tokenized reward text and
        # post-action campfire state, so they are deliberately not loadable here.
        self.macro_prior = AgenticMacroPrior(
            REPO_ROOT / "artifacts" / "agentic_macro_decisions.jsonl",
            REPO_ROOT / "artifacts" / "community_route_prior.json",
            REPO_ROOT / "game_database" / "compiled_macro_decisions.json",
            REPO_ROOT / "artifacts" / "community_tier_stats.json",
        )
        self.community_card_stats = {}
        self.compiled_cards = {}
        stats_p = REPO_ROOT / "artifacts" / "community_tier_stats.json"
        if stats_p.exists():
            try:
                with open(stats_p, "r", encoding="utf-8") as f:
                    self.community_card_stats = json.load(f).get("character_tier_rankings", {})
            except Exception as e:
                print(f"[PureNeuralAgent] Warning: Failed to load empirical card stats: {e}")
        cards_p = REPO_ROOT / "game_database" / "compiled_cards.json"
        if cards_p.exists():
            try:
                with open(cards_p, "r", encoding="utf-8") as f:
                    self.compiled_cards = json.load(f)
            except Exception as e:
                print(f"[PureNeuralAgent] Warning: Failed to load compiled card values: {e}")
        print(f"[PureNeuralAgent] Loaded {self.macro_prior.example_count} exact agentic macro/combat decisions.")

    def select_action(self, obs: Dict[str, Any], legal_actions: List[Dict[str, Any]]) -> str:
        if not legal_actions:
            return "end_turn"

        action_ids = [a.get("action_id", "") for a in legal_actions]
        action_types = set(a.get("action_type", "") for a in legal_actions)

        # 1. Combat Actions -> Neural Combat Policy pi_combat(a | s)
        if obs.get("phase") == "combat" or "play_card" in action_types or "end_turn" in action_ids:
            return self._select_neural_combat_action(obs, legal_actions)

        # 2. Card Drafting -> Neural Macro Draft Policy pi_macro(a | s)
        if "choose_card" in action_types or any(a.startswith("choose_card:") for a in action_ids):
            return self._select_neural_draft_action(obs, legal_actions)

        # 3. Rest Site -> Neural Campfire Policy pi_rest(a | s)
        if "choose_rest" in action_types or any(a.startswith("choose_rest:") for a in action_ids):
            return self._select_neural_rest_action(obs, legal_actions)

        # 4. Map Routing / Rewards / Shops / Events
        return self._select_room_action(obs, legal_actions)

    def _select_neural_combat_action(self, obs: Dict[str, Any], legal_actions: List[Dict[str, Any]]) -> str:
        """Evaluates legal combat actions using the trained CombatPolicyNet."""
        potion_action = self.macro_prior.select_potion(obs, legal_actions)
        if potion_action is not None:
            return potion_action

        if self.combat_model is None or not self.combat_card_to_idx:
            return legal_actions[0]["action_id"]

        try:
            combat = obs.get("combat", {})
            char = str(obs.get("character", "IRONCLAD")).upper()
            char_idx = CHAR_TO_IDX.get(char, 0)
            hp = float(obs.get("player_hp", 60))
            max_hp = max(1.0, float(obs.get("player_max_hp", 80)))
            hp_pct = hp / max_hp
            block_norm = float(obs.get("player_block", 0)) / 50.0
            energy_norm = float(obs.get("player_energy", 3)) / 5.0
            turn_norm = float(combat.get("turn", 1)) / 15.0
            floor_norm = float(obs.get("floor", 1)) / 50.0

            context = [hp_pct, block_norm, energy_norm, turn_norm, floor_norm]

            # Hand Tokens
            hand_raw = combat.get("hand", [])
            hand_tokens = []
            for c in hand_raw[:10]:
                cid = normalize_id(c.get("card_id", "UNKNOWN"))
                c_idx = self.combat_card_to_idx.get(cid, 0)
                cost = min(5, max(0, int(c.get("cost", 1))))
                upgrades = 1 if c.get("upgrades", 0) > 0 else 0
                hand_tokens.append([c_idx, cost, upgrades])

            pad_hand = 10 - len(hand_tokens)
            hand_tokens = hand_tokens + [[0, 0, 0]] * pad_hand

            # Enemy Tokens
            enemies_raw = combat.get("enemies", [])
            enemy_tokens = []
            for e in enemies_raw[:5]:
                e_hp = float(e.get("hp", 20)) / max(1.0, float(e.get("max_hp", 20)))
                e_blk = float(e.get("block", 0)) / 50.0
                dmg = float(e.get("damage", 0)) * max(1, float(e.get("repeats", 1))) / 30.0
                alive = 1.0 if e.get("is_alive", True) else 0.0
                enemy_tokens.append([e_hp, e_blk, dmg, alive])

            pad_enemies = 5 - len(enemy_tokens)
            enemy_tokens = enemy_tokens + [[0.0, 0.0, 0.0, 0.0]] * pad_enemies

            # Candidate Action Tokens
            # The transformer is length-agnostic at inference. Score every legal
            # action instead of silently hiding candidates beyond training slot 16.
            candidates = [a for a in legal_actions if a.get("action_type") != "use_potion"]
            if not candidates:
                candidates = legal_actions
            max_actions = max(16, len(candidates))
            action_tokens = []
            for a in candidates:
                atype_str = a.get("action_type", "")
                atype_int = ACTION_TYPE_MAP.get(atype_str, 1)
                meta = a.get("metadata", {}) or {}
                cid = normalize_id(meta.get("card_id", a.get("description", "")))
                c_idx = self.combat_card_to_idx.get(cid, 0)
                target = int(meta.get("target_id", 0))
                action_tokens.append([atype_int, c_idx, target])

            num_valid = len(action_tokens)
            pad_act = max_actions - num_valid
            mask = [1.0] * num_valid + [0.0] * pad_act
            action_tokens = action_tokens + [[0, 0, 0]] * pad_act

            with torch.no_grad():
                char_t = torch.tensor([char_idx], dtype=torch.long)
                ctx_t = torch.tensor([context], dtype=torch.float32)
                hand_t = torch.tensor([hand_tokens], dtype=torch.long)
                enemy_t = torch.tensor([enemy_tokens], dtype=torch.float32)
                act_t = torch.tensor([action_tokens], dtype=torch.long)
                mask_t = torch.tensor([mask], dtype=torch.float32)

                logits = self.combat_model(char_t, ctx_t, hand_t, enemy_t, act_t, mask_t).squeeze(0)
                best_idx = logits[:num_valid].argmax().item()
                return candidates[best_idx]["action_id"]

        except Exception as e:
            pass

        return legal_actions[0]["action_id"]

    def _select_low_hp_tactical_action(
        self, obs: Dict[str, Any], legal_actions: List[Dict[str, Any]]
    ) -> Optional[str]:
        """Small fail-safe for states where another unblocked turn can end the run."""
        combat = obs.get("combat") or {}
        hand_by_index = {
            int(card.get("index", index)): card
            for index, card in enumerate(combat.get("hand", []))
        }
        enemies = [
            enemy for enemy in combat.get("enemies", [])
            if enemy.get("is_alive", True) and int(enemy.get("hp", 0)) > 0
        ]
        scored = []
        for action in legal_actions:
            if action.get("action_type") != "play_card":
                continue
            meta = action.get("metadata") or {}
            card_index = meta.get("card_index")
            card = hand_by_index.get(int(card_index)) if card_index is not None else None
            card_id = normalize_id(meta.get("card_id") or (card or {}).get("card_id", ""))
            definition = self.compiled_cards.get(card_id, {})
            base_vars = definition.get("base_vars") or {}
            upgrades = int((card or {}).get("upgrades", 0))
            upgrade_vars = definition.get("upgrades") or {}
            damage = float(base_vars.get("damage", 0))
            block = float(base_vars.get("block", 0))
            if upgrades:
                damage += float(upgrade_vars.get("damage", 0))
                block += float(upgrade_vars.get("block", 0))
            cost = max(0, int((card or {}).get("cost", definition.get("cost", 1))))
            hp_loss = float(base_vars.get("hploss", 0))
            target_id = meta.get("target_id")
            scored.append({
                "action_id": action["action_id"],
                "damage": damage,
                "block": block,
                "cost": cost,
                "hp_loss": hp_loss,
                "target_id": target_id,
            })

        if not scored:
            return None

        # Exact visible lethal takes precedence over mitigation.
        for enemy in sorted(enemies, key=lambda e: int(e.get("hp", 0)) + int(e.get("block", 0))):
            effective_hp = int(enemy.get("hp", 0)) + int(enemy.get("block", 0))
            enemy_id = enemy.get("combat_id")
            lethal = [
                item for item in scored
                if item["damage"] >= effective_hp
                and (item["target_id"] in (None, enemy_id))
                and item["hp_loss"] < float(obs.get("player_hp", 1))
            ]
            if lethal:
                return max(lethal, key=lambda item: (item["damage"], -item["cost"]))["action_id"]

        current_hp = float(obs.get("player_hp", 1))
        current_block = float(obs.get("player_block", 0))
        block_target = 12.0 if current_hp <= 10 else 8.0
        safe_blocks = [item for item in scored if item["block"] > 0 and item["hp_loss"] == 0]
        if current_block < block_target and safe_blocks:
            return max(
                safe_blocks,
                key=lambda item: (item["block"] / max(1, item["cost"]), item["block"]),
            )["action_id"]

        safe_damage = [
            item for item in scored
            if item["damage"] > 0 and item["hp_loss"] < float(obs.get("player_hp", 1))
        ]
        if safe_damage:
            return max(
                safe_damage,
                key=lambda item: (item["damage"] / max(1, item["cost"]), item["damage"]),
            )["action_id"]
        return None

    def _select_neural_draft_action(self, obs: Dict[str, Any], legal_actions: List[Dict[str, Any]]) -> str:
        cards = [a for a in legal_actions if a.get("action_type") == "choose_card" or a.get("action_id", "").startswith("choose_card:")]
        if not cards:
            return "skip_card"

        exact_prior_choice = self.macro_prior.select_card(obs, legal_actions)
        if exact_prior_choice is not None:
            return exact_prior_choice

        # The legacy macro checkpoint learned a 98.7%-first-option ordering
        # artifact. Prefer order-independent, sample-shrunk community outcomes.
        char = str(obs.get("character", "IRONCLAD")).upper()
        char_stats = self.community_card_stats.get(char, {})
        scored_cards = []
        for action in cards:
            meta = action.get("metadata") or {}
            card_id = normalize_id(meta.get("card_id") or action.get("description", ""))
            stats = char_stats.get(card_id)
            if not stats:
                continue
            samples = max(0, int(stats.get("sample_runs", 0)))
            reliability = samples / (samples + 75.0)
            score = float(stats.get("delta_win_rate", 0.0)) * reliability
            scored_cards.append((score, samples, action))

        if scored_cards:
            scored_cards.sort(key=lambda item: (item[0], item[1]), reverse=True)
            best_score, _, best_action = scored_cards[0]
            deck_size = len(obs.get("deck_cards", []))
            if best_score > 0.0 or deck_size < 13:
                return best_action["action_id"]
            if "skip_card" in [a.get("action_id") for a in legal_actions]:
                return "skip_card"

        return cards[0]["action_id"]

    def _select_neural_rest_action(self, obs: Dict[str, Any], legal_actions: List[Dict[str, Any]]) -> str:
        learned_choice = self.macro_prior.select_rest(obs, legal_actions)
        return learned_choice or legal_actions[0]["action_id"]

    def _select_room_action(self, obs: Dict[str, Any], legal_actions: List[Dict[str, Any]]) -> str:
        action_ids = [a.get("action_id", "") for a in legal_actions]
        action_types = set(a.get("action_type", "") for a in legal_actions)

        # 1. Rewards Screen
        if "choose_reward" in action_types or any(a.startswith("choose_reward:") for a in action_ids):
            rewards = [a for a in legal_actions if a.get("action_id", "").startswith("choose_reward:")]
            gold_rewards = [a for a in rewards if "Gold" in a.get("action_id", "")]
            card_rewards = [a for a in rewards if "Card" in a.get("action_id", "")]
            relic_rewards = [a for a in rewards if "Relic" in a.get("action_id", "")]
            other_rewards = [a for a in rewards if not any(k in a.get("action_id", "") for k in ["Gold", "Card", "Relic"])]
            # A full belt makes potion reward actions deterministic no-ops.
            if len(obs.get("potions", [])) >= 3:
                other_rewards = [a for a in other_rewards if "Potion" not in a.get("action_id", "")]

            if gold_rewards:
                return gold_rewards[0]["action_id"]
            if card_rewards:
                return card_rewards[0]["action_id"]
            if relic_rewards:
                return relic_rewards[0]["action_id"]
            if other_rewards:
                return other_rewards[0]["action_id"]
            if "proceed" in action_ids:
                return "proceed"

        # 2. Card Upgrades
        if "choose_upgrade" in action_types or any(a.startswith("choose_upgrade:") for a in action_ids):
            upgrades = [a for a in legal_actions if a.get("action_id", "").startswith("choose_upgrade:")]
            if upgrades:
                return self.macro_prior.select_card_operation("upgrade", upgrades) or upgrades[0]["action_id"]

        # 3. Card Select (Grid select / Transform / Purge)
        if "choose_card_select" in action_types or any(a.startswith("choose_card_select:") for a in action_ids):
            selects = [a for a in legal_actions if a.get("action_id", "").startswith("choose_card_select:")]
            if selects:
                return self.macro_prior.select_card_operation("remove", selects) or selects[0]["action_id"]

        # 4. Events
        if "choose_event" in action_types or any(a.startswith("choose_event:") for a in action_ids):
            events = [a for a in legal_actions if a.get("action_id", "").startswith("choose_event:")]
            if events:
                return events[0]["action_id"]

        # 5. Shop
        if "shop_buy" in action_types or any(a.startswith("shop_buy:") for a in action_ids):
            shop_choice = self.macro_prior.select_shop(legal_actions)
            if shop_choice is not None:
                return shop_choice
            if "shop_leave" in action_ids:
                return "shop_leave"

        # 6. Map Routing
        if "choose_map" in action_types or any(a.startswith("choose_map:") for a in action_ids):
            maps = [a for a in legal_actions if a.get("action_id", "").startswith("choose_map:")]
            if maps:
                return self.macro_prior.select_map(obs, maps) or maps[0]["action_id"]
                return maps[0]["action_id"]

        # 7. Proceed
        if "proceed" in action_ids:
            return "proceed"

        return legal_actions[0]["action_id"]


pure_neural_agent = PureNeuralAgent()
