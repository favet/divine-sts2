"""
Empirical A10 Macro Policy for Slay the Spire 2.
Derived directly from the forensic analysis of 593 genuine Ascension 10 Ironclad runs
(213 late Act 3 / Won runs vs 224 early Act 1 deaths).
"""

from typing import Any, Dict, List, Optional
import json

A10_CARD_WEIGHTS: Dict[str, float] = {
    # Top Tier Winners (>+20% Net Advantage)
    "BLOODLETTING": 4.5,
    "OFFERING": 3.8,
    "UPPERCUT": 3.2,
    "TREMBLE": 3.0,
    "UNMOVABLE": 2.8,
    "TAUNT": 2.6,
    "COLOSSUS": 2.5,
    "DOMINATE": 2.4,
    "IMPERVIOUS": 2.2,
    "INFERNO": 2.0,
    "RUPTURE": 2.0,
    "STOKE": 1.8,
    "PYRE": 1.8,
    "VICIOUS": 1.7,
    "CRUELTY": 1.6,
    "HEADBUTT": 1.5,
    "ARMAMENTS": 1.4,
    "ANGER": 1.4,
    "BLUDGEON": 1.3,
    "WHIRLWIND": 1.3,
    "INFLAME": 1.2,
    "CASCADE": 1.2,
    "STAMPEDE": 1.1,
    "BRAND": 1.0,
    "BASH": 0.5,
    
    # Early Act 1 Traps (Negative Net Advantage in Act 1)
    "SHRUG_IT_OFF": -1.5,
    "GREED": -2.0,
    "MOLTEN_FIST": -1.2,
    "IRON_WAVE": -1.0,
    "TRUE_GRIT": -1.0,
    "BLOOD_WALL": -1.0,
    "STONE_ARMOR": -0.8,
    "FLAME_BARRIER": -0.5,
    "SECOND_WIND": -0.5,
    "BATTLE_TRANCE": -0.3,
}

CURSES = {"INJURY", "PAIN", "REGRET", "WRITHE", "NORMALITY", "DOUBT", "CLUMSY", "PARASITE", "DECAY"}


class EmpiricalA10MacroPolicy:
    """Evaluates macro decision surfaces (cards, maps, rests, events, shops) using empirical A10 rules."""

    def __init__(self, mode: str = "baseline"):
        self.mode = mode

    def select_card_reward(self, state: Dict[str, Any], actions: List[Dict[str, Any]]) -> Optional[str]:
        card_candidates = [
            a for a in actions
            if a.get("kind") == "choose_room_reward"
            and str((a.get("parameters") or {}).get("reward_kind", "")).lower() in {"card", "cardreward"}
        ]
        if not card_candidates:
            return None

        features = state.get("scoring_features") or {}
        deck = features.get("deck") or []
        deck_size = len(deck)
        floor = int(features.get("act_floor", 1)) + int(features.get("act_index", 0)) * 16

        attack_count = sum(1 for c in deck if "STRIKE" in str(c.get("model_id", "")).upper() or c.get("type") == "Attack")

        skip_action = next((a for a in card_candidates if int((a.get("parameters") or {}).get("option_index", 0)) < 0), None)
        take_actions = [a for a in card_candidates if int((a.get("parameters") or {}).get("option_index", -1)) >= 0]

        if not take_actions:
            return skip_action["action_id"] if skip_action else None

        scored: List[tuple[float, str]] = []
        for action in take_actions:
            params = action.get("parameters") or {}
            raw_id = str(params.get("model_id") or "").replace("CARD.", "").upper()
            score = A10_CARD_WEIGHTS.get(raw_id, 0.5)

            # Early game tempo attack boost
            if floor <= 6 and attack_count < 4:
                if score > 1.0:
                    score += 1.5
                elif score < 0.0:
                    score -= 1.0

            # B2 Macro Silver Bullet: Hard-Pool Attack Priority
            if self.mode == "b2_macro" and floor <= 6:
                if raw_id in {"ANGER", "UPPERCUT", "HEADBUTT", "TWIN_STRIKE", "CARNAGE", "CLEAVE", "CARVE", "BLUDGEON"}:
                    score += 3.0

            # Synergies with existing cards
            has_bloodletting = any("BLOODLETTING" in str(c.get("model_id", "")).upper() for c in deck)
            if has_bloodletting and raw_id in {"RUPTURE", "INFERNO", "OFFERING", "COLOSSUS"}:
                score += 1.5

            scored.append((score, action["action_id"]))

        scored.sort(key=lambda x: x[0], reverse=True)
        best_score, best_action_id = scored[0]

        # Skip discipline: if deck is already mature (>= 18 cards) and best card is mediocre
        if deck_size >= 18 and best_score < 1.0 and skip_action is not None:
            return skip_action["action_id"]

        # If best score is negative (trap cards only) and deck >= 12, skip
        if deck_size >= 12 and best_score < 0.0 and skip_action is not None:
            return skip_action["action_id"]

        return best_action_id

    def select_map_choice(self, state: Dict[str, Any], actions: List[Dict[str, Any]]) -> Optional[str]:
        map_actions = [a for a in actions if a.get("kind") == "choose_map"]
        if not map_actions:
            return None

        features = state.get("scoring_features") or {}
        deck = features.get("deck") or []
        hp = float(features.get("current_hp", 80))
        max_hp = max(1.0, float(features.get("max_hp", 80)))
        hp_pct = hp / max_hp
        floor = int(features.get("act_floor", 1))

        scored: List[tuple[float, str]] = []
        for action in map_actions:
            params = action.get("parameters") or {}
            ptype = str(params.get("point_type", "Monster"))

            score = 1.0
            if ptype == "Treasure":
                score = 100.0  # Always grab treasure chests
            elif ptype == "RestSite":
                if hp_pct < 0.50:
                    score = 50.0  # Urgent healing
                elif floor >= 14:
                    score = 40.0  # Pre-boss rest/upgrade
                elif self.mode == "b2_macro" and floor <= 8:
                    score = 30.0  # Pre-elite upgrade priority
                else:
                    score = 10.0
            elif ptype == "Elite":
                if self.mode == "b2_macro":
                    has_burst = any(k in str(deck).upper() for k in ["UPPERCUT", "DOMINATE", "BLOODLETTING", "ANGER", "CARNAGE", "BLUDGEON", "BASH+"])
                    has_potion = int(features.get("potion_count", 0)) > 0
                    if floor < 8:
                        score = -100.0  # Absolute veto on suicidal floor 6-7 elites
                    elif hp_pct >= 0.75 and (has_burst or has_potion):
                        score = 35.0   # Snowball when prepared
                    elif hp_pct < 0.60:
                        score = -50.0  # Avoid death at medium/low HP
                    else:
                        score = 2.0
                else:
                    if hp_pct >= 0.70 and floor >= 6:
                        score = 30.0  # Snowball relics
                    elif hp_pct < 0.50:
                        score = -50.0 # Dangerously low HP, avoid elite death
                    else:
                        score = 5.0
            elif ptype == "Shop":
                gold = int(features.get("gold", 0))
                score = 25.0 if gold >= 200 else 5.0
            elif ptype == "Event":
                score = 15.0
            elif ptype == "Monster":
                score = 12.0 if floor <= 3 else 8.0

            scored.append((score, action["action_id"]))

        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1]

    def select_rest_choice(self, state: Dict[str, Any], actions: List[Dict[str, Any]]) -> Optional[str]:
        features = state.get("scoring_features") or {}
        hp = float(features.get("current_hp", 80))
        max_hp = max(1.0, float(features.get("max_hp", 80)))
        hp_pct = hp / max_hp
        floor = int(features.get("act_floor", 1))

        # B2 Macro Silver Bullet: Pre-Elite Upgrade Priority
        if self.mode == "b2_macro" and floor <= 8 and hp_pct >= 0.40:
            smith_act = next((a for a in actions if "SMITH" in str(a.get("action_id", "")).upper()), None)
            if smith_act:
                return smith_act["action_id"]

        # If HP is low, Heal (REST)
        if hp_pct < 0.50:
            rest_act = next((a for a in actions if "REST" in str(a.get("action_id", "")).upper()), None)
            if rest_act:
                return rest_act["action_id"]

        # If HP is safe, Smith (UPGRADE)
        smith_act = next((a for a in actions if "SMITH" in str(a.get("action_id", "")).upper()), None)
        if smith_act:
            return smith_act["action_id"]

        # Fallback to rest if smith is not available
        rest_act = next((a for a in actions if "REST" in str(a.get("action_id", "")).upper()), None)
        if rest_act:
            return rest_act["action_id"]

        return actions[0]["action_id"] if actions else None

    def select_event_choice(self, state: Dict[str, Any], actions: List[Dict[str, Any]]) -> Optional[str]:
        safe_actions = []
        for a in actions:
            aid = str(a.get("action_id", "")).upper()
            params = str(a.get("parameters", "")).upper()
            if any(curse in aid or curse in params for curse in CURSES):
                continue
            safe_actions.append(a)

        chosen = safe_actions or actions
        return chosen[0]["action_id"] if chosen else None

    def select_card_choice(self, state: Dict[str, Any], actions: List[Dict[str, Any]]) -> Optional[str]:
        observation = state.get("observation") or {}
        prompt = json.dumps(observation.get("outstanding_choice") or {}).upper()
        operation = "upgrade" if "UPGRADE" in prompt else "remove" if "REMOVE" in prompt else "select"

        scored: List[tuple[float, str]] = []
        for a in actions:
            aid = str(a.get("action_id", "")).upper()
            if operation == "remove":
                # Prioritize removing Strike, then Defend
                score = 10.0 if "STRIKE" in aid else 5.0 if "DEFEND" in aid else 1.0
            elif operation == "upgrade":
                # Prioritize upgrading high value cards
                score = 10.0 if "BLOODLETTING" in aid else 8.0 if "UPPERCUT" in aid else 6.0 if "BASH" in aid else 2.0
            else:
                score = 5.0 if "STRIKE" in aid else 1.0
            scored.append((score, a["action_id"]))

        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1] if scored else None

    def select_shop_choice(self, state: Dict[str, Any], actions: List[Dict[str, Any]]) -> Optional[str]:
        # If leave_shop is an option, default to leaving unless there's an essential purchase
        leave_act = next((a for a in actions if a.get("kind") == "leave_shop"), None)
        buy_acts = [a for a in actions if a.get("kind") == "buy_shop"]

        for a in buy_acts:
            aid = str(a.get("action_id", "")).upper()
            # Buy Bloodletting or Offering if stocked
            if any(key in aid for key in ["BLOODLETTING", "OFFERING", "UPPERCUT", "PURGE"]):
                return a["action_id"]

        if leave_act:
            return leave_act["action_id"]
        return actions[0]["action_id"] if actions else None
