"""Offline tests for observation projection, permutation invariance, and fog-of-war masking."""
import json
import pytest
from sts2_native_sim.observations import (
    extract_agent_observation,
    project_player_visible_card_state,
    to_agent_observation,
)

def test_draw_pile_permutation_invariance():
    """Verify that shuffle/order changes in raw draw pile produce identical agent observations."""
    card_a = {"model_id": "STRIKE_IRONCLAD", "upgrades": 0, "energy_cost": 1, "card_type": "Attack"}
    card_b = {"model_id": "DEFEND_IRONCLAD", "upgrades": 0, "energy_cost": 1, "card_type": "Skill"}
    card_c = {"model_id": "BASH", "upgrades": 1, "energy_cost": 2, "card_type": "Attack"}

    obs_order_1 = {
        "combat": {
            "piles": [
                {"name": "DrawPile", "cards": [card_a, card_b, card_c]},
                {"name": "Hand", "cards": []},
            ],
            "creatures": [],
        },
        "run": {
            "seed": "SECRET_SEED_123",
            "rng_counters": {"Shuffle": 42},
        },
    }

    obs_order_2 = {
        "combat": {
            "piles": [
                {"name": "DrawPile", "cards": [card_c, card_a, card_b]},
                {"name": "Hand", "cards": []},
            ],
            "creatures": [],
        },
        "run": {
            "seed": "SECRET_SEED_123",
            "rng_counters": {"Shuffle": 42},
        },
    }

    agent_obs_1 = extract_agent_observation({"observation": obs_order_1})
    agent_obs_2 = extract_agent_observation({"observation": obs_order_2})

    draw_1 = agent_obs_1["combat"]["draw_pile_summary"]
    draw_2 = agent_obs_2["combat"]["draw_pile_summary"]

    assert draw_1["count"] == 3
    assert draw_1["count"] == draw_2["count"]
    assert draw_1["histogram"] == draw_2["histogram"]
    assert draw_1["cards_unordered"] == draw_2["cards_unordered"]

def test_seed_and_rng_masking():
    """Verify that seeds and RNG counters are masked."""
    raw_state = {
        "observation": {
            "run": {
                "seed": "RAW_SUPER_SECRET_SEED",
                "rng_counters": {"Combat": 10, "CardRng": 99},
            },
            "combat": {"piles": []},
        },
    }
    agent_obs = extract_agent_observation(raw_state)
    assert agent_obs["run"]["seed"] == "MASKED"
    assert agent_obs["run"]["rng_counters"] == {}

def test_evolving_card_state_whitelisting():
    """Verify that player-visible dynamic state is kept while hidden flags are stripped."""
    raw_native_state = {
        "Damage": 15,
        "Block": 22,
        "MagicNumber": 3,
        "InternalPointer": 0xDEADBEEF,
        "PrivateRngStream": "HIDDEN",
        "_cachedHash": "ABC",
    }
    projected = project_player_visible_card_state(raw_native_state)
    assert "Damage" in projected
    assert "Block" in projected
    assert "MagicNumber" in projected
    assert "InternalPointer" not in projected
    assert "PrivateRngStream" not in projected
    assert "_cachedHash" not in projected
