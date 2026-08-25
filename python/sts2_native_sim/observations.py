"""
Dual Observation Pipeline for STS2.

Maintains the distinction between:
1. Canonical Simulator State (Full Replay State): Contains exact ordered draw pile,
   RNG stream counters, and full internal state. Used strictly for state hashing,
   deterministic branching, MCTS determinization, and differential replay verification.
2. Agent Policy Observation (Player-Visible State): Represents the true game-play
   observation with hidden information masked (unordered draw pile multiset/histogram,
   masked RNG counters/seeds, and preserved player-visible hand/discard/board state).
"""
from __future__ import annotations

import copy
import json
from typing import Any

_PLAYER_VISIBLE_CARD_EXACT_KEYS = {
    # Block modifiers & evolving block
    "currentblock", "increasedblock", "block", "baseblock", "bonusblock", "extrblock",
    # Damage modifiers & evolving damage
    "currentdamage", "increaseddamage", "damage", "basedamage", "bonusdamage", "extradamage",
    # Magic number modifiers & evolving magic
    "magicnumber", "currentmagicnumber", "increasedmagicnumber", "bonusmagic", "extramagic",
    # Counters, stacks, charges, counts
    "counter", "count", "uses", "stacks", "charges", "chargesleft", "turnsremaining", "duration",
    "timesupgraded", "calculationbase", "costmodifier", "specialvalue", "amount", "bonus",
    "permanentcost", "retainedcount", "storedvalue",
    # Play trackers
    "attacksplayed", "skillsplayed", "powersplayed", "cardsplayed",
}

_BLOCKED_CARD_METADATA_SUBSTRINGS = (
    "instance", "pointer", "address", "runtime", "reflection", "guid",
    "net_id", "netid", "debug", "rng", "seed", "hook", "hash", "typehandle",
    "internal", "private", "hidden", "raw",
)


def project_player_visible_card_state(native_state: Any) -> dict[str, Any]:
    """
    Projects a strictly player-visible dynamic card signature from raw card native_state.
    Retains only whitelisted scalar/list properties (evolving damage, block, magic numbers,
    counters, charges) and strips backend reflection metadata or internal identifiers.
    """
    if not isinstance(native_state, dict):
        return {}
    projected: dict[str, Any] = {}
    for key, val in native_state.items():
        if not isinstance(key, str) or key.startswith("_"):
            continue
        lower = key.lower()
        if any(bad in lower for bad in _BLOCKED_CARD_METADATA_SUBSTRINGS):
            continue
        # Check against exact whitelist or common visible prefix/suffix patterns
        is_visible = (
            lower in _PLAYER_VISIBLE_CARD_EXACT_KEYS
            or any(lower.startswith(p) for p in ("block", "damage", "magic", "bonus", "extra", "counter", "count", "stack", "charge", "uses"))
            or any(lower.endswith(s) for s in ("block", "damage", "magic", "counter", "count", "stacks", "bonus", "charges", "uses", "cost"))
        )
        if is_visible:
            if isinstance(val, (int, float, bool, str)):
                projected[key] = val
            elif isinstance(val, list) and all(isinstance(x, (int, float, bool, str)) for x in val):
                projected[key] = list(val)
    return dict(sorted(projected.items()))


def extract_agent_observation(state_or_obs: dict[str, Any]) -> dict[str, Any]:
    """
    Transforms a canonical STS2 simulation state or observation into a player-visible
    Agent Policy Observation with all hidden information masked:
    - Draw pile ordered sequence is transformed into an unordered card multiset/histogram + count.
    - Hidden RNG stream counters and raw seed are masked/stripped.
    - Player-visible state (hand, discard, exhaust, play piles, powers, relics, HP, block, energy, enemy stats, intents) is retained.
    """
    if not isinstance(state_or_obs, dict):
        return {}

    if "observation" in state_or_obs and isinstance(state_or_obs["observation"], dict):
        obs = state_or_obs["observation"]
        root_state = state_or_obs
    else:
        obs = state_or_obs
        root_state = {}

    agent_obs: dict[str, Any] = {
        "schema_version": obs.get("schema_version", 1),
        "game_build": copy.deepcopy(obs.get("game_build", {})),
    }

    # Run-level information
    if "run" in obs and isinstance(obs["run"], dict):
        run = obs["run"]
        agent_run: dict[str, Any] = {
            "ascension": run.get("ascension", 0),
            "gold": run.get("gold", 0),
            "deck": copy.deepcopy(run.get("deck", [])),
            "relics": copy.deepcopy(run.get("relics", [])),
            "potions": copy.deepcopy(run.get("potions", [])),
            # Hidden seeds and RNG stream counters are masked
            "seed": "MASKED",
            "rng_counters": {},
        }
        agent_obs["run"] = agent_run

    # Combat-level information
    if "combat" in obs and isinstance(obs["combat"], dict):
        combat = obs["combat"]
        agent_combat: dict[str, Any] = {
            "turn": combat.get("turn", 0),
            "phase": combat.get("phase", ""),
            "energy": combat.get("energy", 0),
            "max_energy": combat.get("max_energy", 0),
            "stars": combat.get("stars", 0),
            "creatures": copy.deepcopy(combat.get("creatures", [])),
        }
        if "orbs" in combat:
            agent_combat["orbs"] = copy.deepcopy(combat["orbs"])

        # Piles processing
        piles = combat.get("piles", [])
        agent_piles = []
        draw_pile_summary: dict[str, Any] = {"count": 0, "histogram": {}, "cards_unordered": []}

        for pile in piles:
            p_name = pile.get("name", "")
            if p_name == "DrawPile":
                cards = pile.get("cards", [])
                draw_count = len(cards)
                histogram: dict[str, int] = {}
                unordered_list: list[dict[str, Any]] = []
                for c in cards:
                    model = c.get("model_id", "UNKNOWN")
                    upgrades = c.get("upgrades", 0)
                    key = f"{model}+{upgrades}" if upgrades > 0 else model
                    histogram[key] = histogram.get(key, 0) + 1
                    # Project strictly player-visible dynamic card properties (e.g.
                    # Genetic Algorithm block counters, Searing Blow upgrades) while
                    # stripping backend reflection metadata or internal identifiers.
                    native_state = project_player_visible_card_state(c.get("native_state"))
                    unordered_list.append({
                        "model_id": model,
                        "card_type": c.get("card_type"),
                        "target_type": c.get("target_type"),
                        "energy_cost": c.get("energy_cost"),
                        "costs_x": c.get("costs_x"),
                        "upgrades": upgrades,
                        "enchantment": copy.deepcopy(c.get("enchantment")),
                        "rarity": c.get("rarity"),
                        "cost_modified": c.get("cost_modified"),
                        "is_locked": c.get("is_locked"),
                        "native_state": native_state,
                    })
                # Sort by all exposed public card properties so that permutation
                # order of the draw pile never leaks into the agent observation.
                # Ties on (model_id, upgrades) alone occur for multi-copy decks;
                # all remaining properties must also be compared to guarantee
                # 100% invariance and zero draw-order leakage.
                # native_state is included last to distinguish evolving card copies
                # (e.g. Genetic Algorithm with different block counters).
                unordered_list.sort(key=lambda x: (
                    str(x["model_id"]),
                    int(x.get("upgrades") or 0),
                    str(x.get("cost_modified") or ""),
                    str(x.get("rarity") or ""),
                    str(x.get("card_type") or ""),
                    str(x.get("target_type") or ""),
                    str(x.get("enchantment") or ""),
                    str(x.get("is_locked") or ""),
                    str(x.get("energy_cost") or ""),
                    json.dumps(x.get("native_state") or {}, sort_keys=True),
                ))
                draw_pile_summary = {
                    "count": draw_count,
                    "histogram": histogram,
                    "cards_unordered": unordered_list,
                }
                agent_piles.append({
                    "name": "DrawPile",
                    "type": pile.get("type", "Draw"),
                    "count": draw_count,
                    "histogram": histogram,
                    "cards_unordered": unordered_list,
                })
            else:
                agent_piles.append(copy.deepcopy(pile))

        agent_combat["piles"] = agent_piles
        agent_combat["draw_pile_summary"] = draw_pile_summary
        agent_obs["combat"] = agent_combat

    # Decision and legal actions
    if "decision" in obs and isinstance(obs["decision"], dict):
        agent_obs["decision"] = copy.deepcopy(obs["decision"])
    elif "legal_actions" in root_state:
        agent_obs["decision"] = {
            "kind": (obs.get("decision") or {}).get("kind", "combat_action"),
            "legal_actions": copy.deepcopy(root_state["legal_actions"]),
        }

    # Other player-visible structures
    for key in [
        "inventory",
        "outstanding_choice",
        "outstanding_rewards",
        "room_rewards",
        "card_reward",
        "reward",
        "rest",
        "event",
        "map",
        "shop",
        "treasure",
    ]:
        if key in obs and obs[key] is not None:
            agent_obs[key] = copy.deepcopy(obs[key])

    agent_obs["terminal"] = bool(obs.get("terminal", root_state.get("terminated", False)))
    agent_obs["victory"] = bool(obs.get("victory", root_state.get("victory", False)))

    return agent_obs


to_agent_observation = extract_agent_observation