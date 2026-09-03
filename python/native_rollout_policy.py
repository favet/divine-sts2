"""Learned policy adapter for the shipped-native persistent environment.

Strategic choices use option-aligned human evidence or the promoted V10 combat
checkpoint. Unknown choice surfaces are sampled deterministically and reported;
they never silently collapse to the first legal option.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import torch

from agentic_macro_prior import AgenticMacroPrior
from native_outcome_macro_prior import NativeOutcomeMacroPrior
from empirical_a10_macro import EmpiricalA10MacroPolicy
from sts2_native_sim import extract_agent_observation
from combat_v1.vocab import EntityVocabulary
from combat_v1.encoder import CombatV1StateEncoder, CombatV1ActionEncoder
from combat_v5.model import CombatV5PolicyNet
from train_v10_combat_policy import (
    ACTION_TYPE_MAP,
    CHAR_TO_IDX,
    V10CombatPolicyNet,
    normalize_id,
)
from v12_combat_model import V12CombatPolicyNet
from expert_combat_retriever import ExpertCombatRetriever


REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class PolicyDecision:
    action_id: str
    source: str


class NativeLearnedPolicy:
    """Scores the native schema without introducing route/HP/card hard gates."""

    def __init__(self, exploration: float = 0.05, require_combat_checkpoint: bool = True,
                 combat_checkpoint: str | Path | None = None,
                 native_macro_corpus: str | Path | None = None,
                 card_database: str | Path | None = None,
                 alteration: str | None = None):
        if not 0.0 <= exploration <= 1.0:
            raise ValueError("exploration must be between zero and one")
        self.exploration = exploration
        self.alteration = alteration
        self._inference_lock = threading.Lock()
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.empirical_macro = EmpiricalA10MacroPolicy(mode=alteration or "baseline")
        self.vocab = EntityVocabulary()
        self.state_enc = CombatV1StateEncoder(self.vocab)
        self.action_enc = CombatV1ActionEncoder(self.vocab)
        torch.set_num_threads(1)
        try:
            torch.set_num_interop_threads(1)
        except RuntimeError:
            pass

        self.macro = AgenticMacroPrior(
            REPO_ROOT / "artifacts" / "agentic_macro_decisions.jsonl",
            REPO_ROOT / "artifacts" / "community_route_prior.json",
            REPO_ROOT / "game_database" / "compiled_macro_decisions.json",
            REPO_ROOT / "artifacts" / "community_tier_stats.json",
        )
        self.native_macro = NativeOutcomeMacroPrior(Path(native_macro_corpus)) if native_macro_corpus else None
        self.combat_model: V10CombatPolicyNet | None = None
        self.combat_architecture = "v10"
        self.entity_to_idx: dict[str, int] = {}
        card_database_path = Path(card_database) if card_database else REPO_ROOT / "game_database" / "compiled_cards.json"
        self.card_database = json.loads(card_database_path.read_text(encoding="utf-8")) if card_database_path.exists() else {}
        self.expert_combat_retriever: ExpertCombatRetriever | None = None
        self.card_to_idx: dict[str, int] = {}
        checkpoint_path = Path(combat_checkpoint) if combat_checkpoint else REPO_ROOT / "models" / "v10_combat_policy.pt"
        if checkpoint_path.exists():
            checkpoint = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
            if "total_steps" in checkpoint or checkpoint.get("architecture") == "v5":
                self.combat_architecture = "v5"
                vocab_sz = checkpoint.get("vocab_size", max(500, len(self.vocab.entity_to_idx)))
                self.combat_model = CombatV5PolicyNet(
                    vocab_size=vocab_sz,
                    embed_dim=256,
                    num_heads=8,
                    num_encoder_layers=6,
                    dim_feedforward=1024,
                    dropout=0.05,
                )
                self.combat_model.load_state_dict(checkpoint["model_state_dict"])
                self.combat_model.to(self.device)
                self.combat_model.eval()
            elif checkpoint.get("architecture") == "expert_retriever_v1":
                self.combat_architecture = "expert_retriever_v1"
                self.expert_combat_retriever = ExpertCombatRetriever(checkpoint)
                fallback = torch.load(REPO_ROOT / "models" / "v10_combat_policy.pt", map_location="cpu", weights_only=False)
                self.card_to_idx = fallback.get("card_to_idx", {})
                self.combat_model = V10CombatPolicyNet(vocab_size=len(self.card_to_idx), embed_dim=128, num_heads=4, max_actions=16)
                self.combat_model.load_state_dict(fallback["model_state_dict"])
                self.combat_model.to(self.device)
                self.combat_model.eval()
            elif checkpoint.get("architecture") == "v12":
                self.combat_architecture = "v12"
                self.entity_to_idx = checkpoint["entity_to_idx"]
                self.combat_model = V12CombatPolicyNet(len(self.entity_to_idx))
                self.combat_model.load_state_dict(checkpoint["model_state_dict"])
                self.combat_model.to(self.device)
                self.combat_model.eval()
            else:
                self.combat_architecture = "v10"
                self.card_to_idx = checkpoint.get("card_to_idx", {})
                self.combat_model = V10CombatPolicyNet(
                    vocab_size=len(self.card_to_idx), embed_dim=128, num_heads=4, max_actions=16
                )
                self.combat_model.load_state_dict(checkpoint["model_state_dict"])
                self.combat_model.to(self.device)
                self.combat_model.eval()
        elif require_combat_checkpoint:
            raise FileNotFoundError(f"required combat checkpoint is missing: {checkpoint_path}")

    def select(self, state: dict[str, Any]) -> PolicyDecision:
        actions = state.get("legal_actions") or []
        observation = state.get("observation") or {}
        decision_kind = (observation.get("decision") or {}).get("kind", "unknown")
        if not actions:
            raise RuntimeError(f"no legal action for nonterminal decision {decision_kind}")
        if len(actions) == 1:
            return PolicyDecision(actions[0]["action_id"], "forced_native")

        macro_obs = self._macro_observation(state)
        kinds = {action.get("kind") for action in actions}

        if decision_kind == "combat_action":
            adapted = [self._adapt_action(action, state) for action in actions]
            if self.combat_architecture != "v12":
                potion = self.macro.select_potion(macro_obs, adapted)
                if potion is not None:
                    return PolicyDecision(potion, "agentic_potion_prior")

            # BASE ESSENTIAL 1: Strategic Potion Engine (Tier-List Calibrated)
            strategic_potion = self._select_strategic_potion(state, actions)
            if strategic_potion is not None:
                return PolicyDecision(strategic_potion, "strategic_potion_engine")

            # BASE ESSENTIAL 2: Zero Wasted Energy Guard
            # Never waste energy ending turn early when playable useful cards exist in hand.
            playable_cards = [a for a in actions if a.get("kind") == "play_card"]
            features = state.get("scoring_features") or {}
            current_energy = int((features.get("combat") or {}).get("energy", 0))

            candidate_kinds = {"play_card", "use_potion", "end_turn"}
            candidates = [action for action in actions if action.get("kind") in candidate_kinds]

            # If energy remains and we have playable cards, strictly forbid premature end_turn
            if current_energy > 0 and playable_cards:
                candidates = [action for action in candidates if action.get("kind") != "end_turn"]

            if not candidates:
                return self._sample(state, actions, "exploration_no_combat_candidate")
            if self._draw(state, "combat-explore") < self.exploration:
                return self._sample(state, candidates, "combat_exploration")
            return PolicyDecision(self._score_combat(state, candidates), f"{self.combat_architecture}_combat_policy")

        if decision_kind == "card_choice" and (observation.get("combat") or {}) and self.combat_architecture == "v12":
            return PolicyDecision(self._score_combat(state, actions), "v12_combat_card_choice")

        if decision_kind == "room_reward_choice":
            # Gold/relic acquisition and full-potion-belt skips are inventory
            # mechanics, not card-quality labels. Card/skip decisions remain
            # entirely option-aligned and learned.
            non_card = [
                action for action in actions
                if action.get("kind") == "choose_room_reward"
                and int((action.get("parameters") or {}).get("option_index", -1)) >= 0
                and str((action.get("parameters") or {}).get("reward_kind", "")).lower() not in {"card", "potion"}
            ]
            if non_card:
                return PolicyDecision(non_card[0]["action_id"], "native_inventory_acquisition")
            potion_takes = [
                action for action in actions
                if action.get("kind") == "choose_room_reward"
                and int((action.get("parameters") or {}).get("option_index", -1)) >= 0
                and str((action.get("parameters") or {}).get("reward_kind", "")).lower() == "potion"
            ]
            if potion_takes:
                features = state.get("scoring_features") or {}
                count = int(features.get("potion_count", 0))
                capacity = int(features.get("potion_capacity", 0))
                if capacity <= 0 or count < capacity:
                    return PolicyDecision(potion_takes[0]["action_id"], "native_inventory_acquisition")
                reward_index = int((potion_takes[0].get("parameters") or {}).get("reward_index", -1))
                skip = next(
                    (
                        action for action in actions
                        if action.get("kind") == "choose_room_reward"
                        and int((action.get("parameters") or {}).get("reward_index", -2)) == reward_index
                        and int((action.get("parameters") or {}).get("option_index", 0)) < 0
                    ),
                    None,
                )
                if skip is not None:
                    return PolicyDecision(skip["action_id"], "native_full_potion_belt_skip")
            card_actions = [
                self._adapt_action(action, state) for action in actions
                if action.get("kind") == "choose_room_reward"
            ]
            native_learned = self.native_macro.select(
                state, [action for action in actions if action.get("kind") == "choose_room_reward"]
            ) if self.native_macro is not None else None
            if native_learned is not None:
                return PolicyDecision(native_learned, "native_outcome_card_prior")
            learned = self.macro.select_card(macro_obs, card_actions)
            if learned is not None:
                return PolicyDecision(learned, "agentic_card_prior")
            empirical = self.empirical_macro.select_card_reward(state, actions)
            if empirical is not None:
                return PolicyDecision(empirical, "empirical_a10_card_prior")

        if decision_kind == "map_choice":
            native_learned = self.native_macro.select(state, actions) if self.native_macro is not None else None
            if native_learned is not None:
                return PolicyDecision(native_learned, "native_outcome_route_prior")
            learned = self.macro.select_map(macro_obs, [self._adapt_action(a, state) for a in actions])
            if learned is not None:
                return PolicyDecision(learned, "agentic_route_prior")
            empirical = self.empirical_macro.select_map_choice(state, actions)
            if empirical is not None:
                return PolicyDecision(empirical, "empirical_a10_route_prior")

        if decision_kind == "rest_choice":
            native_learned = self.native_macro.select(state, actions) if self.native_macro is not None else None
            if native_learned is not None:
                return PolicyDecision(native_learned, "native_outcome_rest_prior")
            learned = self.macro.select_rest(macro_obs, [self._adapt_action(a, state) for a in actions])
            if learned is not None:
                return PolicyDecision(learned, "agentic_rest_prior")
            empirical = self.empirical_macro.select_rest_choice(state, actions)
            if empirical is not None:
                return PolicyDecision(empirical, "empirical_a10_rest_prior")

        if decision_kind == "event_choice":
            native_learned = self.native_macro.select(state, actions) if self.native_macro is not None else None
            if native_learned is not None:
                return PolicyDecision(native_learned, "native_outcome_event_prior")
            learned = self.macro.select_event(macro_obs, [self._adapt_action(a, state) for a in actions])
            if learned is not None:
                return PolicyDecision(learned, "agentic_event_prior")
            empirical = self.empirical_macro.select_event_choice(state, actions)
            if empirical is not None:
                return PolicyDecision(empirical, "empirical_a10_event_prior")

        if decision_kind == "shop_choice":
            features = state.get("scoring_features") or {}
            belt_full = int(features.get("potion_capacity", 0)) > 0 and int(features.get("potion_count", 0)) >= int(features.get("potion_capacity", 0))
            usable = [
                a for a in actions
                if not (
                    belt_full and a.get("kind") == "buy_shop"
                    and str((a.get("parameters") or {}).get("entry_kind", "")).lower() == "potion"
                )
            ]
            native_learned = self.native_macro.select(state, usable) if self.native_macro is not None else None
            if native_learned is not None:
                return PolicyDecision(native_learned, "native_outcome_shop_prior")
            learned = self.macro.select_shop([self._adapt_action(a, state) for a in usable])
            if learned is not None:
                return PolicyDecision(learned, "agentic_shop_prior")
            empirical = self.empirical_macro.select_shop_choice(state, usable)
            if empirical is not None:
                return PolicyDecision(empirical, "empirical_a10_shop_prior")

        if decision_kind == "custom_reward_choice":
            features = state.get("scoring_features") or {}
            belt_full = int(features.get("potion_capacity", 0)) > 0 and int(features.get("potion_count", 0)) >= int(features.get("potion_capacity", 0))
            choices = [
                a for a in actions
                if a.get("kind") == "choose_custom_reward"
                and not (belt_full and str((a.get("parameters") or {}).get("reward_kind", "")).lower() == "potion")
            ]
            if choices:
                return self._sample(state, choices, "custom_reward_exploration")
            skip = next((a for a in actions if a.get("kind") == "skip_custom_rewards"), None)
            if skip is not None:
                return PolicyDecision(skip["action_id"], "native_full_potion_belt_skip")

        if decision_kind == "treasure_relic_choice":
            relics = [a for a in actions if a.get("kind") == "choose_treasure"]
            if relics:
                native_learned = self.native_macro.select(state, relics) if self.native_macro is not None else None
                if native_learned is not None:
                    return PolicyDecision(native_learned, "native_outcome_relic_prior")
                return self._sample(state, relics, "relic_exploration")

        if decision_kind == "card_choice":
            adapted = [self._adapt_action(a, state) for a in actions]
            native_learned = self.native_macro.select(state, actions) if self.native_macro is not None else None
            if native_learned is not None:
                return PolicyDecision(native_learned, "native_outcome_card_operation_prior")
            prompt = json.dumps(observation.get("outstanding_choice") or {}).upper()
            operation = "upgrade" if "UPGRADE" in prompt else "remove" if "REMOVE" in prompt else "select"
            learned = self.macro.select_card_operation(operation, adapted)
            if learned is not None:
                return PolicyDecision(learned, f"agentic_{operation}_prior")
            empirical = self.empirical_macro.select_card_choice(state, actions)
            if empirical is not None:
                return PolicyDecision(empirical, "empirical_a10_card_choice_prior")

        progression = [
            action for action in actions
            if action.get("kind") in {
                "generate_room_rewards", "leave_room_rewards", "leave_event", "leave_rest",
                "open_treasure", "leave_treasure", "leave_shop", "advance_act",
            }
        ]
        if len(progression) == 1 and len(actions) == 1:
            return PolicyDecision(progression[0]["action_id"], "forced_progression")
        return self._sample(state, actions, f"unlearned_{decision_kind}")

    def _select_strategic_potion(self, state: dict[str, Any], actions: list[dict[str, Any]]) -> str | None:
        potion_actions = [a for a in actions if a.get("kind") == "use_potion"]
        if not potion_actions:
            return None

        obs = state.get("observation") or {}
        feat = state.get("scoring_features") or {}
        combat = obs.get("combat") or {}
        creatures = combat.get("creatures") or []
        room = obs.get("room") or {}
        room_type = str(room.get("room_type") or feat.get("room_type", "Monster")).lower()
        is_elite = "elite" in room_type
        is_boss = "boss" in room_type
        is_elite_or_boss = is_elite or is_boss
        turn = int(combat.get("turn", 1))

        # Potion Belt Capacity Ratio (Smooth Quadratic Capacity Pressure)
        potions = feat.get("potions") or []
        count = int(feat.get("potion_count", len(potions)))
        capacity = max(1, int(feat.get("potion_capacity", 3)))
        capacity_ratio = count / capacity

        enemies = [c for c in creatures if str(c.get("side", "")).lower() == "enemy"]
        incoming_damage = 0.0
        for e in enemies:
            for intent in ((e.get("next_move") or {}).get("intents") or []):
                incoming_damage += float(intent.get("damage") or 0.0) * max(1.0, float(intent.get("repeats") or 1.0))
        player_block = float((feat.get("combat") or {}).get("block", 0))
        player_hp = float(feat.get("current_hp", 80))
        unblocked = incoming_damage - player_block

        for action in potion_actions:
            params = action.get("parameters") or {}
            model_id = str(params.get("model_id") or action.get("action_id") or "").upper()

            is_boss_vault = any(k in model_id for k in ["CULTIST", "MAZALETH", "STRENGTH", "DEXTERITY", "GHOST_IN_A_JAR", "DUPLICATOR"])

            # Invariant 1: Turn 1 Scaling Potions on Bosses / Elites (Boss Vault Deployed)
            if is_elite_or_boss and turn == 1:
                if is_boss_vault:
                    return action["action_id"]

            # Invariant 2: Targeted Snipe / AoE against Swarms or High-Threat Monsters
            if any(k in model_id for k in ["FIRE_POTION", "EXPLOSIVE"]):
                # Always use against swarms (Sentries, Gremlins) or Elites
                if len(enemies) > 1 or is_elite_or_boss:
                    return action["action_id"]
                # Capacity pressure: if holding >= 2 potions, burn Fire Potion on tough single enemy to preserve HP
                if capacity_ratio >= 0.67 and any(float(e.get("current_hp", 0)) >= 25 for e in enemies):
                    return action["action_id"]

            # Invariant 3: Tactical Energy / Draw Acceleration
            if any(k in model_id for k in ["ENERGY_POTION", "SWIFT_POTION", "POWER_POTION"]):
                if is_elite_or_boss and turn <= 3:
                    return action["action_id"]
                if capacity_ratio >= 0.67 and unblocked >= 10:
                    return action["action_id"]

            # Invariant 4: True Lethal & Emergency Damage Negation
            if unblocked >= player_hp or (unblocked >= 18 and player_hp <= 35):
                if any(k in model_id for k in ["GHOST_IN_A_JAR", "LUCKY_TONIC", "BLOCK_POTION", "SPEED_POTION", "HEART_OF_IRON"]):
                    return action["action_id"]

            # Invariant 5: High-Capacity Flow (Burn utility potions to prevent chip damage and unlock drops)
            # If belt is near full (2/3 or 3/3), DO NOT HOARD utility potions. Prevent 8+ damage immediately!
            if capacity_ratio >= 0.67 and not is_boss_vault:
                if unblocked >= 8 and any(k in model_id for k in ["BLOCK_POTION", "SPEED_POTION", "FLEX_POTION", "WEAK_POTION"]):
                    return action["action_id"]

        return None

    def _macro_observation(self, state: dict[str, Any]) -> dict[str, Any]:
        features = state.get("scoring_features") or {}
        act_index = int(features.get("act_index", 0))
        act_floor = int(features.get("act_floor", 0))
        floor = act_index * 16 + max(1, act_floor)
        combat = features.get("combat") or {}
        enemies = []
        for creature in combat.get("creatures") or []:
            if str(creature.get("side", "")).lower() != "enemy":
                continue
            damage = repeats = 0.0
            for intent in ((creature.get("next_move") or {}).get("intents") or []):
                damage = max(damage, float(intent.get("damage") or 0.0))
                repeats = max(repeats, float(intent.get("repeats") or 1.0))
            enemies.append({"damage": damage, "repeats": repeats})
        return {
            "character": features.get("character"),
            "player_hp": features.get("current_hp", 0),
            "player_max_hp": features.get("max_hp", 1),
            "floor": floor,
            "seed": (state.get("observation", {}).get("run") or {}).get("seed"),
            "state_hash": state.get("state_hash"),
            "combat": {"enemies": enemies},
            "deck_cards": [card.get("model_id") for card in features.get("deck") or []],
        }

    def _adapt_action(self, action: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        kind = action.get("kind", "")
        params = action.get("parameters") or {}
        action_type = kind
        metadata: dict[str, Any] = {}
        if kind == "choose_map":
            metadata["room_type"] = params.get("point_type")
        elif kind == "choose_rest":
            metadata["option_key"] = params.get("option_id")
        elif kind == "choose_event":
            metadata["option_key"] = params.get("text_key")
        elif kind == "buy_shop":
            action_type = "shop_buy"
            metadata.update({
                "item_id": params.get("model_id") or params.get("entry_kind"),
                "affordable": True, "stocked": True, "price": params.get("cost"),
            })
        elif kind == "leave_shop":
            action_type = "shop_leave"
        elif kind == "choose_room_reward":
            action_type = "choose_card" if str(params.get("reward_kind", "")).lower() in {"card", "cardreward"} else "choose_reward"
            metadata.update({"card_id": params.get("model_id"), "skip": int(params.get("option_index", -1)) < 0})
        elif kind in {"choose_cards", "choose_option"}:
            selected = params.get("option_ids") or []
            option_lookup = {
                option.get("option_id"): option.get("model_id")
                for option in ((state.get("observation", {}).get("outstanding_choice") or {}).get("options") or [])
            }
            metadata["card_id"] = option_lookup.get(selected[0]) if len(selected) == 1 else "+".join(selected)
        elif kind == "use_potion":
            metadata["potion_id"] = params.get("model_id")
        return {
            "action_id": action["action_id"],
            "action_type": action_type,
            "metadata": metadata,
        }

    def _score_combat(self, state: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
        if self.expert_combat_retriever is not None:
            retrieved = self.expert_combat_retriever.select(state, candidates)
            if retrieved is not None:
                return retrieved
        if self.combat_model is None:
            raise RuntimeError("combat checkpoint is unavailable")
        if self.combat_architecture == "v12":
            return self._score_combat_v12(state, candidates)
        if self.combat_architecture == "v5":
            return self._score_combat_v5(state, candidates)
        features = state.get("scoring_features") or {}
        observation = state.get("observation") or {}
        combat = observation.get("combat") or {}
        piles = {pile.get("name"): pile.get("cards") or [] for pile in combat.get("piles") or []}
        hand = piles.get("Hand", [])[:10]
        hand_by_instance = {card.get("instance_id"): card for card in hand}
        char_idx = CHAR_TO_IDX.get(str(features.get("character", "IRONCLAD")).upper(), 0)
        hp = float(features.get("current_hp", 1))
        max_hp = max(1.0, float(features.get("max_hp", 1)))
        act_index = int(features.get("act_index", 0))
        floor = act_index * 16 + max(1, int(features.get("act_floor", 0)))
        context = [
            hp / max_hp,
            float(features.get("block", 0)) / 50.0,
            float(combat.get("energy", 3)) / 5.0,
            float(combat.get("turn", 1)) / 15.0,
            float(floor) / 50.0,
        ]
        hand_tokens = [
            [
                self.card_to_idx.get(normalize_id(card.get("model_id", "UNKNOWN")), 0),
                min(5, max(0, int(card.get("energy_cost", 1)))),
                1 if int(card.get("upgrades", 0)) > 0 else 0,
            ]
            for card in hand
        ]
        hand_tokens.extend([[0, 0, 0]] * (10 - len(hand_tokens)))

        creatures = combat.get("creatures") or []
        target_slots = {int(c.get("combat_id", -1)): min(9, i + 1) for i, c in enumerate(creatures)}
        enemy_tokens = []
        for creature in [c for c in creatures if str(c.get("side", "")).lower() == "enemy"][:5]:
            damage = 0.0
            for intent in ((creature.get("next_move") or {}).get("intents") or []):
                damage += float(intent.get("damage") or 0.0) * max(1.0, float(intent.get("repeats") or 1.0))
            enemy_tokens.append([
                float(creature.get("hp", 0)) / max(1.0, float(creature.get("max_hp", 1))),
                float(creature.get("block", 0)) / 50.0,
                damage / 30.0,
                1.0 if creature.get("alive", True) else 0.0,
            ])
        enemy_tokens.extend([[0.0, 0.0, 0.0, 0.0]] * (5 - len(enemy_tokens)))

        action_tokens = []
        for action in candidates:
            params = action.get("parameters") or {}
            card = hand_by_instance.get(params.get("instance_id"), {})
            action_tokens.append([
                ACTION_TYPE_MAP.get(action.get("kind", ""), 1),
                self.card_to_idx.get(normalize_id(card.get("model_id", "UNKNOWN")), 0),
                target_slots.get(int(params.get("target_id") or -1), 0),
            ])
        with self._inference_lock, torch.inference_mode():
            logits = self.combat_model(
                torch.as_tensor([char_idx], dtype=torch.long, device=self.device),
                torch.as_tensor([context], dtype=torch.float32, device=self.device),
                torch.as_tensor([hand_tokens], dtype=torch.long, device=self.device),
                torch.as_tensor([enemy_tokens], dtype=torch.float32, device=self.device),
                torch.as_tensor([action_tokens], dtype=torch.long, device=self.device),
                torch.ones((1, len(action_tokens)), dtype=torch.float32, device=self.device),
            ).squeeze(0)
        return candidates[int(logits[:len(candidates)].argmax().item())]["action_id"]

    def _score_combat_v12(self, state: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
        features = state.get("scoring_features") or {}
        observation = state.get("observation") or {}
        combat = observation.get("combat") or {}
        scoring_combat = features.get("combat") or {}
        piles = {pile.get("name"): pile.get("cards") or [] for pile in combat.get("piles") or []}
        scoring_piles = scoring_combat.get("piles") or {}
        hand = piles.get("Hand", [])[:10]
        hand_by_instance = {card.get("instance_id"): card for card in hand}
        entity_idx = lambda value: self.entity_to_idx.get(normalize_id(str(value or "UNKNOWN")), 0)
        hp = float(features.get("current_hp", 1)); max_hp = max(1.0, float(features.get("max_hp", 1)))
        enemies = [creature for creature in (combat.get("creatures") or []) if str(creature.get("side", "")).lower() == "enemy"][:5]
        incoming = 0.0
        for enemy in enemies:
            incoming += sum(float(intent.get("damage") or 0) * max(1, int(intent.get("repeats") or 1)) for intent in ((enemy.get("next_move") or {}).get("intents") or []))
        act_index = int(features.get("act_index", 0)); floor = act_index * 16 + max(1, int(features.get("act_floor", 0)))
        context = [
            hp / max_hp, float(features.get("block", 0)) / 50.0, float(combat.get("energy", 0)) / 5.0,
            float(combat.get("turn", 1)) / 20.0, floor / 50.0,
            len(scoring_piles.get("draw", [])) / 40.0, len(scoring_piles.get("discard", [])) / 40.0,
            len(scoring_piles.get("exhaust", [])) / 40.0, float(combat.get("stars", 0)) / 10.0,
            incoming / 50.0, len(enemies) / 5.0, len(hand) / 10.0,
        ]
        hand_ids, hand_numeric = [], []
        numeric_by_instance = {}
        for card in hand:
            model_id = str(card.get("model_id", "UNKNOWN")); upgrades = int(card.get("upgrades", 0))
            definition = self.card_database.get(model_id, {}); base = definition.get("base_vars") or {}; upgrade = definition.get("upgrades") or {}
            damage = float(base.get("damage", 0)) + upgrades * float(upgrade.get("damage", 0))
            block = float(base.get("block", 0)) + upgrades * float(upgrade.get("block", 0))
            numeric = [min(5, max(-1, int(card.get("energy_cost", 0)))) / 5.0, float(upgrades > 0), damage / 40.0, block / 40.0, 1.0]
            hand_ids.append(entity_idx(model_id)); hand_numeric.append(numeric); numeric_by_instance[card.get("instance_id")] = numeric
        creatures = combat.get("creatures") or []
        target_slots = {int(creature.get("combat_id", -1)): min(9, slot) for slot, creature in enumerate(enemies, 1)}
        enemy_ids, enemy_numeric = [], []
        for slot, enemy in enumerate(enemies, 1):
            intents = (enemy.get("next_move") or {}).get("intents") or []
            damage = max([float(intent.get("damage") or 0) for intent in intents] or [0.0])
            enemy_ids.append(entity_idx(enemy.get("model_id")))
            repeats = max(
                [float(intent.get("repeats") or 1) for intent in intents]
                or [1.0]
            )
            enemy_numeric.append([float(enemy.get("hp", 0)) / max(1.0, float(enemy.get("max_hp", 1))), float(enemy.get("block", 0)) / 50.0, damage / 40.0, repeats / 5.0, float(enemy.get("alive", True)), float(slot)])
        aux_ids, aux_numeric = [], []
        player = next((creature for creature in creatures if str(creature.get("side", "")).lower() == "player"), {})
        for power in player.get("powers") or []:
            aux_ids.append(entity_idx(power.get("model_id"))); aux_numeric.append([1 / 3, float(power.get("amount") or 1) / 10.0])
        for relic in features.get("relics") or []:
            aux_ids.append(entity_idx(relic)); aux_numeric.append([2 / 3, 0.1])
        potions = (observation.get("inventory") or {}).get("potions") or []
        for potion in potions:
            if not potion:
                continue
            aux_ids.append(entity_idx(potion.get("model_id"))); aux_numeric.append([1.0, 0.1])
        orbs = combat.get("orbs") or {}
        orb_entries = orbs.get("entries") or []
        aux_ids.append(entity_idx("ORB_CAPACITY")); aux_numeric.append([
            float(orbs.get("capacity", 0)) / 10.0, len(orb_entries) / 10.0,
        ])
        for orb in orb_entries:
            aux_ids.append(entity_idx(orb.get("model_id"))); aux_numeric.append([
                float(orb.get("passive", 0)) / 20.0, float(orb.get("evoke", 0)) / 30.0,
            ])
        aux_ids, aux_numeric = aux_ids[:24], aux_numeric[:24]
        action_types, action_ids, action_slots, action_numeric = [], [], [], []
        choice_options = {
            option.get("option_id"): option.get("model_id")
            for option in (observation.get("outstanding_choice") or {}).get("options") or []
        }
        for action in candidates:
            params = action.get("parameters") or {}; card = hand_by_instance.get(params.get("instance_id"), {})
            option_ids = params.get("option_ids") or []
            choice_card_id = choice_options.get(option_ids[0]) if len(option_ids) == 1 else None
            model_id = choice_card_id or card.get("model_id") or params.get("model_id")
            action_types.append(1 if choice_card_id else min(3, ACTION_TYPE_MAP.get(action.get("kind", ""), 0)))
            action_ids.append(entity_idx(model_id))
            action_slots.append(target_slots.get(int(params.get("target_id") or -1), 0))
            if choice_card_id:
                definition = self.card_database.get(choice_card_id, {})
                base = definition.get("base_vars") or {}
                action_numeric.append([
                    min(5, max(-1, int(definition.get("cost", 0)))) / 5.0, 0.0,
                    float(base.get("damage", 0)) / 40.0, float(base.get("block", 0)) / 40.0, 1.0,
                ])
            else:
                action_numeric.append(numeric_by_instance.get(params.get("instance_id"), [0.0] * 5))
        def padded(values, size, fill): return values[:size] + [fill] * max(0, size - len(values))
        state_mask = [1.0, 1.0] + [1.0] * len(hand_ids) + [0.0] * (10-len(hand_ids)) + [1.0] * len(enemy_ids) + [0.0] * (5-len(enemy_ids)) + [1.0] * len(aux_ids) + [0.0] * (24-len(aux_ids))
        inputs = {
            "char_idx": torch.tensor([CHAR_TO_IDX.get(str(features.get("character", "SILENT")).upper(), 1)]),
            "context": torch.tensor([context]), "hand_ids": torch.tensor([padded(hand_ids, 10, 0)]),
            "hand_numeric": torch.tensor([padded(hand_numeric, 10, [0.0]*5)]), "enemy_ids": torch.tensor([padded(enemy_ids, 5, 0)]),
            "enemy_numeric": torch.tensor([padded(enemy_numeric, 5, [0.0]*6)]), "aux_ids": torch.tensor([padded(aux_ids, 24, 0)]),
            "aux_numeric": torch.tensor([padded(aux_numeric, 24, [0.0]*2)]), "state_mask": torch.tensor([state_mask]),
            "action_types": torch.tensor([action_types]), "action_ids": torch.tensor([action_ids]), "action_slots": torch.tensor([action_slots]),
            "action_numeric": torch.tensor([action_numeric]), "action_mask": torch.ones((1, len(candidates))),
        }
        with self._inference_lock, torch.inference_mode():
            logits = self.combat_model(**inputs).squeeze(0)
        return candidates[int(logits.argmax().item())]["action_id"]

    def _score_combat_v5(self, state: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
        obs = state.get("observation") or {}
        agent_obs = extract_agent_observation(obs)
        s_t = self.state_enc.encode(agent_obs)
        a_t, _ = self.action_enc.encode(agent_obs, candidates[:64])
        s_b = {k: v.unsqueeze(0).to(self.device, non_blocking=True) for k, v in s_t.items()}
        a_b = {k: v.unsqueeze(0).to(self.device, non_blocking=True) for k, v in a_t.items()}
        with self._inference_lock, torch.inference_mode():
            out = self.combat_model(s_b, a_b)
            logits = out["action_logits"].squeeze(0)[:len(candidates)]
            best_idx = int(logits.argmax().item())

            # B1 Tactical Silver Bullet: Blunder Aversion Guard
            if self.alteration == "b1_tactical":
                chosen = candidates[best_idx]
                if chosen.get("kind") == "end_turn" and len(candidates) > 1:
                    features = state.get("scoring_features") or {}
                    combat = obs.get("combat") or {}
                    creatures = combat.get("creatures") or []
                    incoming = sum(
                        float(intent.get("damage") or 0.0) * max(1.0, float(intent.get("repeats") or 1.0))
                        for c in creatures if str(c.get("side", "")).lower() == "enemy"
                        for intent in ((c.get("next_move") or {}).get("intents") or [])
                    )
                    player_block = float((features.get("combat") or {}).get("block", 0))
                    energy = int((features.get("combat") or {}).get("energy", 0))
                    if incoming > player_block and energy > 0:
                        alt_idxs = [i for i, c in enumerate(candidates) if c.get("kind") == "play_card"]
                        if alt_idxs:
                            best_alt = max(alt_idxs, key=lambda i: logits[i].item())
                            if logits[best_alt].item() > logits[best_idx].item() - 2.5:
                                best_idx = best_alt
        return candidates[best_idx]["action_id"]

    def _sample(self, state: dict[str, Any], actions: list[dict[str, Any]], source: str) -> PolicyDecision:
        draw = self._draw(state, source)
        index = min(len(actions) - 1, int(draw * len(actions)))
        return PolicyDecision(actions[index]["action_id"], source)

    @staticmethod
    def _draw(state: dict[str, Any], salt: str) -> float:
        payload = f"{state.get('state_hash')}:{salt}".encode("utf-8")
        return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") / float(2**64)
