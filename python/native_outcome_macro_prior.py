"""Outcome-weighted macro action scorer learned from shipped-native episodes."""
from __future__ import annotations

import gzip
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any


def _norm(value: Any) -> str:
    return str(value or "").upper().replace(" ", "_")


def _token(action: dict[str, Any]) -> str:
    kind = str(action.get("kind") or action.get("action_type") or "")
    params = action.get("parameters") or action.get("metadata") or {}
    if kind == "choose_map":
        return "MAP:" + _norm(params.get("point_type") or params.get("room_type"))
    if kind == "choose_rest":
        return "REST:" + _norm(params.get("option_id") or params.get("option_key"))
    if kind == "choose_event":
        return "EVENT:" + _norm(params.get("text_key") or params.get("option_key"))
    if kind in {"buy_shop", "shop_buy"}:
        return "SHOP:" + _norm(params.get("model_id") or params.get("item_id") or params.get("entry_kind"))
    if kind in {"leave_shop", "shop_leave"}:
        return "SHOP:LEAVE"
    if kind == "choose_room_reward":
        if int(params.get("option_index", 0)) < 0 or params.get("skip"):
            return "REWARD:SKIP"
        return "REWARD:" + _norm(params.get("model_id") or params.get("card_id") or params.get("reward_kind"))
    if kind in {"choose_cards", "choose_option"}:
        values = params.get("option_ids") or []
        return "CHOICE:" + _norm("+".join(values) or params.get("card_id"))
    if kind in {"skip_custom_rewards", "skip_treasure"}:
        return _norm(kind)
    return _norm(kind) + ":" + _norm(params.get("model_id") or params.get("option_id"))


class NativeOutcomeMacroPrior:
    """A small contextual bandit table using AWR weights and episode return.

    Every score comes from an action that was legal in an actual shipped-DLL
    transition.  Context backoff is statistical; it contains no card, route,
    elite, shop, rest, or HP decision rule.
    """

    def __init__(self, corpus: Path):
        self._selected: dict[tuple[str, ...], dict[str, list[float]]] = defaultdict(
            lambda: defaultdict(lambda: [0.0, 0.0, 0.0])
        )
        self._offered: dict[tuple[str, ...], dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self.rows = 0
        if corpus.exists():
            self._load(corpus)

    def _contexts(self, features: dict[str, Any], kind: str) -> list[tuple[str, ...]]:
        character = _norm(features.get("character"))
        act = str(int(features.get("act_index", 0)))
        hp_ratio = float(features.get("current_hp", 0)) / max(1.0, float(features.get("max_hp", 1)))
        hp_bin = str(min(4, int(hp_ratio * 5.0)))
        return [
            (kind, character, act, hp_bin),
            (kind, character, act),
            (kind, character),
            (kind,),
        ]

    def _load(self, corpus: Path) -> None:
        opener = gzip.open if corpus.suffix == ".gz" else open
        with opener(corpus, "rt", encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                kind = str(row.get("decision_kind") or "")
                if kind == "combat_action":
                    continue
                legal = row.get("legal_actions") or []
                selected = next((action for action in legal if action.get("action_id") == row.get("action")), None)
                if selected is None:
                    continue
                token = _token(selected)
                weight = max(0.05, float(row.get("advantage_weight", 1.0)))
                outcome = float(row.get("episode_return", 0.0))
                for context in self._contexts(row.get("scoring_features") or {}, kind):
                    stat = self._selected[context][token]
                    stat[0] += weight * outcome
                    stat[1] += weight
                    stat[2] += 1.0
                    for action in legal:
                        self._offered[context][_token(action)] += 1.0
                self.rows += 1

    def select(self, state: dict[str, Any], actions: list[dict[str, Any]]) -> str | None:
        features = state.get("scoring_features") or {}
        kind = str(((state.get("observation") or {}).get("decision") or {}).get("kind") or "")
        for context in self._contexts(features, kind):
            scored = []
            for action in actions:
                token = _token(action)
                reward_sum, weight_sum, selected_count = self._selected.get(context, {}).get(token, (0.0, 0.0, 0.0))
                offered_count = self._offered.get(context, {}).get(token, 0.0)
                if selected_count < 3 or offered_count < 5:
                    continue
                mean_return = reward_sum / max(weight_sum, 1e-9)
                propensity = (selected_count + 1.0) / (offered_count + 2.0)
                confidence = selected_count / (selected_count + 12.0)
                score = confidence * mean_return + 0.08 * math.log(propensity)
                scored.append((score, action["action_id"]))
            if scored:
                return max(scored)[1]
        return None
