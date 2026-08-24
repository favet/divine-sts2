"""Empirical macro/potion prior compiled from exact agentic state/action pairs."""

from __future__ import annotations

import json
import math
import hashlib
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


def normalize_token(value: Any) -> str:
    return str(value or "").upper().replace("+", "").replace(" ", "_").replace("'", "").strip("_")


def candidate_token(candidate: Dict[str, Any]) -> str:
    for key in ("name", "node_type", "option_id", "title", "label"):
        if candidate.get(key):
            return normalize_token(candidate[key])
    return "UNKNOWN"


def context_key(example: Dict[str, Any]) -> str:
    state_type = example["state_type"]
    state = example["state"]
    floor = int(example.get("floor", 1))
    act = min(3, max(1, ((floor - 1) // 16) + 1))
    if state_type == "map":
        return f"map:act{act}"
    if state_type == "card_select":
        prompt = normalize_token((state.get("selection_details") or {}).get("prompt", ""))
        kind = "upgrade" if "UPGRADE" in prompt else "remove" if "REMOVE" in prompt else "select"
        return f"card_select:{kind}"
    return state_type


def stable_softmax_choice(scored: List[Tuple[float, str]], state_key: str) -> Optional[str]:
    finite = [(score, action_id) for score, action_id in scored if math.isfinite(score)]
    if not finite:
        return None
    peak = max(score for score, _ in finite)
    weights = [math.exp(score - peak) for score, _ in finite]
    total = sum(weights)
    digest = hashlib.sha256(state_key.encode("utf-8")).digest()
    draw = int.from_bytes(digest[:8], "big") / float(2**64) * total
    cumulative = 0.0
    for weight, (_, action_id) in zip(weights, finite):
        cumulative += weight
        if draw <= cumulative:
            return action_id
    return finite[-1][1]


class AgenticMacroPrior:
    def __init__(self, examples_path: Path, route_prior_path: Optional[Path] = None,
                 choice_corpus_path: Optional[Path] = None,
                 outcome_stats_path: Optional[Path] = None):
        self.stats: Dict[str, Dict[str, List[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
        self.rest_examples: List[Tuple[float, float, str]] = []
        self.map_examples: List[Tuple[float, float, Tuple[str, ...], str]] = []
        self.potion_examples: List[Tuple[float, float, int, float, bool, str]] = []
        self.route_prior: Dict[str, Any] = {}
        self.card_outcomes: Dict[str, Dict[str, Any]] = {}
        self.card_synergies: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.example_count = 0
        if examples_path.exists():
            self._load(examples_path)
        if route_prior_path is not None and route_prior_path.exists():
            self.route_prior = json.loads(route_prior_path.read_text(encoding="utf-8"))
        if choice_corpus_path is not None and choice_corpus_path.exists():
            self._load_choice_corpus(choice_corpus_path)
        if outcome_stats_path is not None and outcome_stats_path.exists():
            outcomes = json.loads(outcome_stats_path.read_text(encoding="utf-8"))
            self.card_outcomes = outcomes.get("character_tier_rankings") or {}
            for pair in outcomes.get("top_synergies") or []:
                left, right = normalize_token(pair.get("card_1")), normalize_token(pair.get("card_2"))
                self.card_synergies[(left, right)] = pair
                self.card_synergies[(right, left)] = pair

    @property
    def available(self) -> bool:
        return self.example_count > 0

    def _load(self, path: Path) -> None:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                example = json.loads(line)
                self.example_count += 1
                state_type = example["state_type"]
                action_name = example["action_name"]
                candidates = example.get("candidates") or []
                selected_index = example.get("selected_index")
                key = context_key(example)

                for candidate in candidates:
                    token = candidate_token(candidate)
                    stat = self.stats[key][token]
                    stat[0] += 1
                    same_action = candidate.get("_action_name") == action_name
                    candidate_index = int(candidate.get("index", -1))
                    selected = same_action and (
                        selected_index is None or candidate_index == int(selected_index)
                    )
                    if selected:
                        stat[1] += 1

                state = example.get("state") or {}
                hp_pct = float(state.get("hp", 0)) / max(1.0, float(state.get("hp_max", 1)))
                floor_norm = float(example.get("floor", 1)) / 50.0
                if state_type == "rest_site":
                    selected = next(
                        (candidate_token(c) for c in candidates
                         if c.get("_action_name") == action_name
                         and (selected_index is None or int(c.get("index", -1)) == int(selected_index))),
                        "",
                    )
                    if selected:
                        self.rest_examples.append((hp_pct, floor_norm, selected))

                if state_type == "map":
                    selected = next(
                        (candidate_token(c) for c in candidates
                         if c.get("_action_name") == action_name
                         and (selected_index is None or int(c.get("index", -1)) == int(selected_index))),
                        "",
                    )
                    if selected:
                        self.map_examples.append(
                            (hp_pct, floor_norm, tuple(candidate_token(c) for c in candidates), selected)
                        )

                if state_type in {"monster", "elite", "boss"}:
                    combat = state.get("combat") or {}
                    enemies = combat.get("enemies") or []
                    incoming = sum(
                        max((float(intent.get("total_damage") or 0) for intent in enemy.get("intents") or []), default=0.0)
                        for enemy in enemies
                    ) / max(1.0, float(state.get("hp_max", 1)))
                    potion_name = ""
                    if action_name == "use_potion" and selected_index is not None:
                        potions = ((combat.get("player") or {}).get("potions") or [])
                        potion_name = next(
                            (normalize_token(p.get("name")) for p in potions if int(p.get("index", -1)) == int(selected_index)),
                            "",
                        )
                    self.potion_examples.append(
                        (hp_pct, incoming, {"monster": 0, "elite": 1, "boss": 2}[state_type], floor_norm,
                         action_name == "use_potion", potion_name)
                    )

    def _load_choice_corpus(self, path: Path) -> None:
        for decision in json.loads(path.read_text(encoding="utf-8")):
            if decision.get("decision_type") != "card_reward":
                continue
            # Agentic examples are already represented with their complete state.
            if decision.get("source") == "agentic_exact_state_action":
                continue
            offered = [normalize_token(card) for card in decision.get("offered") or []]
            picked = normalize_token(decision.get("picked") or "SKIP")
            character = normalize_token(decision.get("character") or "UNKNOWN")
            for key in ("card_reward", f"card_reward:{character}"):
                for token in offered:
                    self.stats[key][token][0] += 1
                    if token == picked:
                        self.stats[key][token][1] += 1
                self.stats[key]["SKIP"][0] += 1
                if picked == "SKIP":
                    self.stats[key]["SKIP"][1] += 1

    def _rate(self, key: str, token: str) -> float:
        offered, selected = self.stats.get(key, {}).get(normalize_token(token), (0, 0))
        if offered == 0:
            return float("-inf")
        return math.log((selected + 2.0) / (offered + 4.0))

    def select_card(self, obs: Dict[str, Any], legal_actions: List[Dict[str, Any]]) -> Optional[str]:
        character_key = f"card_reward:{normalize_token(obs.get('character') or 'UNKNOWN')}"
        for key in (character_key, "card_reward"):
            scored = []
            character = normalize_token(obs.get("character") or "UNKNOWN")
            deck = {normalize_token(card) for card in obs.get("deck_cards") or []}
            for action in legal_actions:
                if action.get("action_type") == "choose_card":
                    token = normalize_token((action.get("metadata") or {}).get("card_id"))
                elif action.get("action_id") == "skip_card" or (action.get("metadata") or {}).get("skip"):
                    token = "SKIP"
                else:
                    continue
                behavior = self._rate(key, token)
                outcome_bonus = 0.0
                if token != "SKIP":
                    stats = self.card_outcomes.get(character, {}).get(token) or {}
                    samples = max(0, int(stats.get("sample_runs", 0)))
                    reliability = samples / (samples + 75.0)
                    outcome_bonus = 4.0 * float(stats.get("delta_win_rate", 0.0)) * reliability
                    for owned in deck:
                        pair = self.card_synergies.get((token, owned))
                        if pair:
                            count = max(0, int(pair.get("co_occurrence_count", 0)))
                            outcome_bonus += 2.0 * float(pair.get("synergy_lift", 0.0)) * count / (count + 25.0)
                    if stats and not math.isfinite(behavior):
                        behavior = math.log(0.5)
                scored.append((behavior + outcome_bonus, action["action_id"]))
            known = [item for item in scored if math.isfinite(item[0])]
            if known:
                return max(known)[1]
        return None

    def select_map(self, obs: Dict[str, Any], legal_actions: List[Dict[str, Any]]) -> Optional[str]:
        floor = int(obs.get("floor", 1))
        act = min(3, max(1, ((floor - 1) // 16) + 1))
        key = f"map:act{act}"
        act_stats = (self.route_prior.get("acts") or {}).get(str(act), {})
        target_shares = act_stats.get("node_visit_share") or {}
        total_selected = sum(selected for _, selected in self.stats.get(key, {}).values())
        hp_pct = float(obs.get("player_hp", 0)) / max(1.0, float(obs.get("player_max_hp", 1)))
        floor_norm = floor / 50.0
        neighbors = sorted(
            self.map_examples,
            key=lambda item: abs(item[0] - hp_pct) + 0.2 * abs(item[1] - floor_norm),
        )[:61]
        scored = []
        for action in legal_actions:
            if action.get("action_type") != "choose_map":
                continue
            token = normalize_token((action.get("metadata") or {}).get("room_type"))
            local_offered = sum(token in offered for _, _, offered, _ in neighbors)
            local_selected = sum(selected == token for _, _, _, selected in neighbors)
            score = (
                math.log((local_selected + 2.0) / (local_offered + 4.0))
                if local_offered else self._rate(key, token)
            )
            if math.isfinite(score) and target_shares:
                selected = self.stats[key].get(token, (0, 0))[1]
                behavior_share = selected / max(1.0, total_selected)
                target_share = float(target_shares.get(token, 0.0))
                # Soft occupancy calibration: exact choices provide option
                # alignment; 742 A10 wins correct their route-frequency bias.
                score += 0.5 * math.log((target_share + 0.01) / (behavior_share + 0.01))
            scored.append((score, action["action_id"]))
        state_key = str(obs.get("state_hash") or f"{obs.get('seed')}:{floor}:{hp_pct}:{[a[1] for a in scored]}")
        return stable_softmax_choice(scored, state_key)

    def select_rest(self, obs: Dict[str, Any], legal_actions: List[Dict[str, Any]]) -> Optional[str]:
        if not self.rest_examples:
            return None
        hp_pct = float(obs.get("player_hp", 0)) / max(1.0, float(obs.get("player_max_hp", 1)))
        floor_norm = float(obs.get("floor", 1)) / 50.0
        neighbors = sorted(
            self.rest_examples,
            key=lambda item: abs(item[0] - hp_pct) + 0.35 * abs(item[1] - floor_norm),
        )[:21]
        votes: Dict[str, float] = defaultdict(float)
        for neighbor_hp, neighbor_floor, token in neighbors:
            distance = abs(neighbor_hp - hp_pct) + 0.35 * abs(neighbor_floor - floor_norm)
            votes[token] += 1.0 / (0.05 + distance)
        scored = []
        for action in legal_actions:
            token = normalize_token((action.get("metadata") or {}).get("option_key"))
            scored.append((math.log(votes.get(token, 0.0) + 1e-6), action["action_id"]))
        state_key = str(obs.get("state_hash") or f"rest:{obs.get('seed')}:{obs.get('floor')}:{hp_pct}")
        return stable_softmax_choice(scored, state_key)

    def select_event(self, obs: Dict[str, Any], legal_actions: List[Dict[str, Any]]) -> Optional[str]:
        scored = []
        for action in legal_actions:
            if action.get("action_type") != "choose_event":
                continue
            meta = action.get("metadata") or {}
            token = meta.get("option_key") or meta.get("text_key") or action.get("description")
            scored.append((self._rate("event", normalize_token(token)), action["action_id"]))
        known = [item for item in scored if math.isfinite(item[0])]
        if not known:
            return None
        state_key = str(obs.get("state_hash") or f"event:{obs.get('seed')}:{obs.get('floor')}:{[a[1] for a in known]}")
        return stable_softmax_choice(known, state_key)

    def select_card_operation(self, operation: str, legal_actions: List[Dict[str, Any]]) -> Optional[str]:
        key = f"card_select:{operation}"
        scored = [
            (self._rate(key, (action.get("metadata") or {}).get("card_id")), action["action_id"])
            for action in legal_actions
        ]
        known = [item for item in scored if math.isfinite(item[0])]
        return max(known, default=(0.0, None))[1]

    def select_shop(self, legal_actions: List[Dict[str, Any]]) -> Optional[str]:
        scored = []
        for action in legal_actions:
            if action.get("action_id") in {"shop_leave", "leave_shop"}:
                token = "LEAVE"
                score = self._rate("shop", token)
            elif action.get("action_type") == "shop_buy":
                meta = action.get("metadata") or {}
                if not meta.get("affordable", True) or not meta.get("stocked", True):
                    continue
                token = normalize_token(meta.get("item_id"))
                score = self._rate("shop", token)
            else:
                continue
            scored.append((score, action["action_id"]))
        known = [item for item in scored if math.isfinite(item[0])]
        return max(known, default=(0.0, None))[1]

    def select_potion(self, obs: Dict[str, Any], legal_actions: List[Dict[str, Any]]) -> Optional[str]:
        potion_actions = [a for a in legal_actions if a.get("action_type") == "use_potion"]
        if not potion_actions or not self.potion_examples:
            return None
        hp_pct = float(obs.get("player_hp", 0)) / max(1.0, float(obs.get("player_max_hp", 1)))
        enemies = (obs.get("combat") or {}).get("enemies") or []
        incoming = sum(float(enemy.get("damage", 0)) * max(1.0, float(enemy.get("repeats", 1))) for enemy in enemies)
        incoming /= max(1.0, float(obs.get("player_max_hp", 1)))
        phase_kind = 2 if int(obs.get("floor", 1)) in (16, 33, 50) else 0
        floor_norm = float(obs.get("floor", 1)) / 50.0
        neighbors = sorted(
            self.potion_examples,
            key=lambda item: abs(item[0] - hp_pct) + abs(item[1] - incoming)
            + 0.25 * abs(item[2] - phase_kind) + 0.2 * abs(item[3] - floor_norm),
        )[:61]
        weighted_use = weighted_total = 0.0
        potion_votes: Dict[str, float] = defaultdict(float)
        for n_hp, n_incoming, n_kind, n_floor, used, potion_name in neighbors:
            distance = abs(n_hp - hp_pct) + abs(n_incoming - incoming) + 0.25 * abs(n_kind - phase_kind) + 0.2 * abs(n_floor - floor_norm)
            weight = 1.0 / (0.05 + distance)
            weighted_total += weight
            weighted_use += weight * float(used)
            if used and potion_name:
                potion_votes[potion_name] += weight
        use_probability = weighted_use / max(1e-9, weighted_total)
        state_key = str(obs.get("state_hash") or f"potion:{obs.get('seed')}:{obs.get('floor')}:{hp_pct}:{incoming}")
        draw = int.from_bytes(hashlib.sha256(state_key.encode("utf-8")).digest()[:8], "big") / float(2**64)
        if draw >= use_probability:
            return None
        return max(
            potion_actions,
            key=lambda action: potion_votes.get(normalize_token((action.get("metadata") or {}).get("potion_id")), 0.0),
        )["action_id"]
