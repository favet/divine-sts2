"""Nonparametric combat policy over exact decisions from victorious runs."""

from __future__ import annotations

import heapq
from collections import Counter, defaultdict
from typing import Any


def _multiset_distance(left: tuple[str, ...], right: tuple[str, ...]) -> float:
    a, b = Counter(left), Counter(right)
    overlap = sum((a & b).values()); union = sum((a | b).values())
    return 1.0 - overlap / max(1, union)


class ExpertCombatRetriever:
    def __init__(self, checkpoint: dict[str, Any]):
        self.examples = checkpoint["examples"]
        self.by_encounter: dict[tuple[str, ...], list[int]] = defaultdict(list)
        self.by_count: dict[int, list[int]] = defaultdict(list)
        for index, example in enumerate(self.examples):
            signature = tuple(example["enemy_ids"])
            self.by_encounter[signature].append(index)
            self.by_count[len(signature)].append(index)

    @staticmethod
    def _query(state: dict[str, Any]) -> dict[str, Any]:
        features = state.get("scoring_features") or {}; combat = (state.get("observation") or {}).get("combat") or {}
        piles = {pile.get("name"): pile.get("cards") or [] for pile in combat.get("piles") or []}
        hand = piles.get("Hand", [])
        enemies = [item for item in combat.get("creatures") or [] if str(item.get("side", "")).lower() == "enemy"]
        incoming = []
        for enemy in enemies:
            incoming.append(sum(float(intent.get("damage") or 0)*max(1,float(intent.get("repeats") or 1)) for intent in ((enemy.get("next_move") or {}).get("intents") or [])))
        return {
            "hp": float(features.get("current_hp", 0))/max(1.0,float(features.get("max_hp", 1))),
            "energy": float(combat.get("energy", 0)), "turn": float(combat.get("turn", 1)),
            "floor": int(features.get("act_index", 0))*16+int(features.get("act_floor", 0)),
            "hand": tuple(sorted(str(card.get("model_id", "UNKNOWN")) for card in hand)),
            "hand_by_instance": {str(card.get("instance_id")): str(card.get("model_id", "UNKNOWN")) for card in hand},
            "enemy_ids": tuple(str(enemy.get("model_id", "UNKNOWN")) for enemy in enemies),
            "enemy_hp": tuple(float(enemy.get("hp",0))/max(1.0,float(enemy.get("max_hp",1))) for enemy in enemies),
            "incoming": tuple(value/40.0 for value in incoming),
            "combat_to_slot": {int(enemy.get("combat_id",-1)): slot for slot,enemy in enumerate(enemies)},
        }

    @staticmethod
    def _distance(query: dict[str, Any], example: dict[str, Any]) -> float:
        distance = 2.5*_multiset_distance(query["hand"], tuple(example["hand"]))
        distance += 0.7*abs(query["hp"]-example["hp"]) + 0.12*abs(query["energy"]-example["energy"])
        distance += 0.04*abs(query["turn"]-example["turn"]) + 0.01*abs(query["floor"]-example["floor"])
        for left,right in zip(query["enemy_hp"],example["enemy_hp"]): distance += 0.35*abs(left-right)
        for left,right in zip(query["incoming"],example["incoming"]): distance += 0.2*abs(left-right)
        return distance

    def select(self, state: dict[str, Any], candidates: list[dict[str, Any]]) -> str | None:
        query = self._query(state); signature = query["enemy_ids"]
        pool = self.by_encounter.get(signature) or self.by_count.get(len(signature), [])
        if not pool: return None
        nearest = heapq.nsmallest(41, pool, key=lambda index: self._distance(query,self.examples[index]))
        votes: dict[tuple[str,str,str],float] = defaultdict(float)
        for index in nearest:
            example = self.examples[index]; distance = self._distance(query,example)
            votes[tuple(example["choice"])] += 1.0/(0.04+distance)
        scored = []
        for candidate in candidates:
            kind = str(candidate.get("kind")); params = candidate.get("parameters") or {}
            card_id = query["hand_by_instance"].get(str(params.get("instance_id")), "")
            target_slot = query["combat_to_slot"].get(int(params.get("target_id") or -1), -1)
            target_id = query["enemy_ids"][target_slot] if 0 <= target_slot < len(query["enemy_ids"]) else ""
            semantic = (kind,card_id,target_id)
            score = votes.get(semantic,0.0)
            if score: scored.append((score,candidate["action_id"]))
        return max(scored,default=(0.0,None))[1]


def compile_examples(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    examples=[]
    for row in rows:
        obs=row.get("observation") or {}; combat=obs.get("combat") or {}; enemies=combat.get("enemies") or []
        legal={action.get("action_id"):action for action in row.get("legal_actions") or []}; chosen=legal.get(row.get("action"))
        if chosen is None: continue
        meta=chosen.get("metadata") or {}; target=int(meta.get("target_id",0)); target_id=str(enemies[target-1].get("enemy_id","")) if 0<target<=len(enemies) else ""
        examples.append({
            "hp":float(obs.get("player_hp",0))/max(1.0,float(obs.get("player_max_hp",1))), "energy":float(obs.get("player_energy",0)),
            "turn":float(combat.get("turn",1)), "floor":int(row.get("floor",1)),
            "hand":tuple(sorted(str(card.get("card_id","UNKNOWN")) for card in combat.get("hand") or [])),
            "enemy_ids":tuple(str(enemy.get("enemy_id","UNKNOWN")) for enemy in enemies),
            "enemy_hp":tuple(float(enemy.get("hp",0))/max(1.0,float(enemy.get("max_hp",1))) for enemy in enemies),
            "incoming":tuple(float(enemy.get("damage",0))*max(1,float(enemy.get("repeats",1)))/40.0 for enemy in enemies),
            "choice":(str(chosen.get("action_type")),str(meta.get("card_id","")),target_id),
        })
    return examples
