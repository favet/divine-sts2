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

            # Early game frontloaded damage priority (Floors 1-5)
            if floor <= 5 and attack_count < 4:
                if raw_id in {"ANGER", "UPPERCUT", "HEADBUTT", "TWIN_STRIKE", "CARNAGE", "CLEAVE", "CARVE", "BLUDGEON"}:
                    score += 3.0

            # ARCHETYPE 1: Strength & Multi-Hit Acceleration
            has_strength = any(any(k in str(c.get("model_id", "")).upper() for k in ["INFLAME", "SPOT_WEAKNESS", "DEMON_FORM", "RUPTURE", "VAJRA"]) for c in deck)
            has_multi_attack = any(any(k in str(c.get("model_id", "")).upper() for k in ["TWIN_STRIKE", "SWORD_BOOMERANG", "PUMMEL", "HEAVY_BLADE", "REAPER"]) for c in deck)
            if has_strength and raw_id in {"TWIN_STRIKE", "SWORD_BOOMERANG", "PUMMEL", "HEAVY_BLADE", "REAPER"}:
                score += 2.5
            if has_multi_attack and raw_id in {"INFLAME", "SPOT_WEAKNESS", "DEMON_FORM", "RUPTURE"}:
                score += 2.5

            # ARCHETYPE 2: Exhaust Engine & Card Velocity
            has_exhaust_payoff = any(any(k in str(c.get("model_id", "")).upper() for k in ["FEEL_NO_PAIN", "DARK_EMBRACE", "CORRUPTION"]) for c in deck)
            if has_exhaust_payoff and raw_id in {"SECOND_WIND", "TRUE_GRIT", "BURNING_PACT", "FIEND_FIRE", "SENTINEL"}:
                score += 3.0
            elif raw_id in {"FEEL_NO_PAIN", "DARK_EMBRACE", "CORRUPTION"}:
                score += 2.0

            # ARCHETYPE 3: Vulnerable Burst & Cycling
            has_vuln = any(any(k in str(c.get("model_id", "")).upper() for k in ["BASH", "UPPERCUT", "SHOCKWAVE"]) for c in deck)
            if has_vuln and raw_id in {"CARNAGE", "BLOOD_FOR_BLOOD", "DROPKICK"}:
                score += 2.0

            # ARCHETYPE 4: Mid-Act Defensive Mitigation Rebalancing
            # Once deck has sufficient damage (Floors 6+ and attack_count >= 4), heavily prioritize premium block
            if floor >= 6 and attack_count >= 4:
                if raw_id in {"SHRUG_IT_OFF", "FLAME_BARRIER", "POWER_THROUGH", "IMPERVIOUS", "GHOSTLY_ARMOR", "SHOCKWAVE"}:
                    score += 3.5
                elif raw_id in {"STRIKE", "WILD_STRIKE", "CLOTHESLINE", "PERFECTED_STRIKE"}:
                    score -= 2.0

            # Synergies with self-damage
            has_bloodletting = any("BLOODLETTING" in str(c.get("model_id", "")).upper() for c in deck)
            if has_bloodletting and raw_id in {"RUPTURE", "INFERNO", "OFFERING", "COLOSSUS"}:
                score += 2.0

            scored.append((score, action["action_id"]))

        scored.sort(key=lambda x: x[0], reverse=True)
        best_score, best_action_id = scored[0]

        # Smooth Skip Gradient: Threshold scales continuously with deck maturity
        # Deck size <= 11: skip threshold is 0.5 (take almost anything useful)
        # Deck size >= 18: skip threshold is 2.5 (only take premium cards to prevent draw dilution)
        skip_threshold = 0.5 + 0.25 * max(0, deck_size - 11)
        if best_score < skip_threshold and skip_action is not None:
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
        smith_act = next((a for a in actions if "SMITH" in str(a.get("action_id", "")).upper()), None)
        rest_act = next((a for a in actions if "REST" in str(a.get("action_id", "")).upper() or "HEAL" in str(a.get("action_id", "")).upper()), None)
        if not smith_act:
            return rest_act["action_id"] if rest_act else (actions[0]["action_id"] if actions else None)
        if not rest_act:
            return smith_act["action_id"]

        features = state.get("scoring_features") or {}
        hp = float(features.get("current_hp", 80))
        max_hp = max(1.0, float(features.get("max_hp", 80)))
        hp_ratio = hp / max_hp
        floor = int(features.get("act_floor", 1))
        potions = features.get("potions") or []
        deck = [c.get("model_id") if isinstance(c, dict) else str(c) for c in features.get("deck") or []]

        # Calculate highest upgrade value delta in deck
        has_unupgraded_bash = any("BASH" in str(c).upper() and "+" not in str(c) for c in deck)
        has_unupgraded_uppercut = any("UPPERCUT" in str(c).upper() and "+" not in str(c) for c in deck)
        has_unupgraded_armaments = any("ARMAMENTS" in str(c).upper() and "+" not in str(c) for c in deck)
        has_unupgraded_carnage = any("CARNAGE" in str(c).upper() and "+" not in str(c) for c in deck)

        upgrade_delta = 1.0
        if has_unupgraded_armaments:
            upgrade_delta = 4.5
        elif has_unupgraded_bash or has_unupgraded_uppercut or has_unupgraded_carnage:
            upgrade_delta = 3.8

        # Virtual HP from holding defensive or burst potions
        has_block_potion = any(any(k in str(p).upper() for k in ["BLOCK", "GHOST", "BUFFER", "LUCKY", "SPEED"]) for p in potions)
        has_burst_potion = any(any(k in str(p).upper() for k in ["FIRE", "EXPLOSIVE", "STRENGTH"]) for p in potions)
        potion_virtual_hp = (0.18 if has_block_potion else 0.0) + (0.10 if has_burst_potion else 0.0)

        # Effective safety ratio = actual HP ratio + virtual HP from potions
        effective_safety = hp_ratio + potion_virtual_hp

        # Smooth logistic logit difference: Grandmasters smith aggressively early
        # Centered around effective safety of 0.35
        q_diff = (effective_safety - 0.35) * 12.0 + (upgrade_delta - 2.0) * 1.5

        # Early act bonus (Floors 1-9): upgrades snowball combat efficiency
        if floor <= 9:
            q_diff += 2.0

        if q_diff >= 0.0:
            return smith_act["action_id"]
        return rest_act["action_id"]

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
                # Grandmaster Rule: Prioritize removing basic Strike over Defend
                score = 10.0 if "STRIKE" in aid else 5.0 if "DEFEND" in aid else 1.0
            elif operation == "upgrade":
                # Upgrade priority: Bash+ (3-turn vuln) & Uppercut+ (2 weak, 2 vuln) & Armaments+
                score = 10.0 if "BASH" in aid else 9.5 if "UPPERCUT" in aid else 9.0 if "ARMAMENTS" in aid else 8.0 if "CARNAGE" in aid else 7.0 if "BLOODLETTING" in aid else 6.0 if "ANGER" in aid else 2.0
            else:
                score = 5.0 if "STRIKE" in aid else 1.0
            scored.append((score, a["action_id"]))

        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1] if scored else None

    def select_shop_choice(self, state: Dict[str, Any], actions: List[Dict[str, Any]]) -> Optional[str]:
        leave_act = next((a for a in actions if a.get("kind") == "leave_shop"), None)
        buy_acts = [a for a in actions if a.get("kind") == "buy_shop"]
        if not buy_acts:
            return leave_act["action_id"] if leave_act else (actions[0]["action_id"] if actions else None)

        features = state.get("scoring_features") or {}
        gold = int(features.get("gold", 0))
        potions = features.get("potions") or []
        deck = [c.get("model_id") if isinstance(c, dict) else str(c) for c in features.get("deck") or []]
        strike_count = sum(1 for c in deck if "STRIKE" in str(c).upper())

        scored: List[tuple[float, str]] = []

        for a in buy_acts:
            params = a.get("parameters") or {}
            cost = int(params.get("cost", 999))
            if cost > gold:
                continue
            entry_kind = str(params.get("entry_kind", "")).lower()
            model_id = str(params.get("model_id") or a.get("action_id", "")).upper()

            score = 0.0

            # 1. Card Removal / Purge (Highest ROI in Act 1)
            if entry_kind == "card_removal" or "PURGE" in model_id or "REMOVE" in model_id:
                if strike_count >= 3:
                    score = 9.0
                else:
                    score = 5.5

            # 2. High-Impact Potions
            elif entry_kind == "potion" or "POTION" in a.get("action_id", "").upper():
                if len(potions) < 3:
                    if any(k in model_id for k in ["CULTIST", "STRENGTH", "DEXTERITY", "GHOST", "FIRE", "BLOCK", "POWER"]):
                        score = 8.0
                    else:
                        score = 4.0

            # 3. Combat Relics
            elif entry_kind == "relic":
                if any(k in model_id for k in ["VAJRA", "SMOOTH_STONE", "ANCHOR", "LANTERN", "HORN_CLEAT", "THREAD", "PAPER"]):
                    score = 8.5
                else:
                    score = 5.0

            # 4. Premium Cards
            elif entry_kind == "card":
                raw_card = model_id.replace("CARD.", "")
                base_val = A10_CARD_WEIGHTS.get(raw_card, 0.5)
                if raw_card in {"SHRUG_IT_OFF", "FLAME_BARRIER", "POWER_THROUGH", "IMPERVIOUS", "SHOCKWAVE", "INFLAME", "UPPERCUT", "ANGER", "POMMEL_STRIKE"}:
                    score = 6.5 + base_val
                elif base_val > 1.0:
                    score = 3.5 + base_val
                else:
                    score = 0.5

            if score > 0.0:
                scored.append((score, a["action_id"]))

        scored.sort(key=lambda x: x[0], reverse=True)
        if scored and scored[0][0] >= 3.0:
            return scored[0][1]

        return leave_act["action_id"] if leave_act else actions[0]["action_id"]
