"""
STS2 Native Gymnasium Vector Environment.
Wraps the 20-Worker Native .NET 9 Godot Engine (divine-sts2 / native_sim) into standard Gymnasium API.
Supports context injection from .run files, action masking, in-engine branch state restore, and high-throughput RL rollouts.
"""

from __future__ import annotations
import copy
from typing import Dict, Any, List, Tuple, Optional
import numpy as np

from .client import NativeWorkerPool, NativeWorker
from .observations import extract_agent_observation

DEFAULT_SCENARIO = {
    "game_build": {},
    "seed": "RL_TRAIN_SEED",
    "rng_counters": {},
    "character": "IRONCLAD",
    "ascension": 0,
    "encounter": "first",
    "current_hp": 80,
    "max_hp": 80,
    "gold": 99,
    "deck": [
        {"instance_id": f"strike-{i}", "model_id": "STRIKE_IRONCLAD"} for i in range(5)
    ] + [
        {"instance_id": f"defend-{i}", "model_id": "DEFEND_IRONCLAD"} for i in range(4)
    ] + [
        {"instance_id": "bash-0", "model_id": "BASH"}
    ],
    "initial_hand": [],
    "relics": [],
    "potions": []
}


class Sts2NativeVectorEnv:
    """Vectorized multi-worker RL environment backed by isolated native Godot / .NET 9 processes."""

    def __init__(
        self,
        num_workers: int = 20,
        workers: Optional[int] = None,
        default_scenario: Optional[Dict[str, Any]] = None,
        ascension: int = 0,
        seed: Optional[int] = None,
    ):
        self.num_workers = workers if workers is not None else num_workers
        self.default_scenario = copy.deepcopy(default_scenario or DEFAULT_SCENARIO)
        if ascension != 0:
            self.default_scenario["ascension"] = ascension
        self._rng = np.random.default_rng(seed)
        self.pool = NativeWorkerPool(self.num_workers)
        self.current_states: List[Dict[str, Any]] = [{}] * self.num_workers
        self.previous_hp: List[int] = [80] * self.num_workers
        self.previous_enemy_hp: List[int] = [100] * self.num_workers

    def reset(
        self,
        seed: Optional[int] = None,
        options: Optional[Dict[str, Any]] = None,
        scenarios: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """
        Resets all workers, conforming to standard Gymnasium VectorEnv reset interface.
        Returns:
            observations: List of player-visible observation dicts.
            infos: Dict containing legal_actions, masks, legal_action_ids, and state hashes/handles.
        """
        if seed is not None:
            self._rng = np.random.default_rng(seed)

        scenarios = scenarios or [copy.deepcopy(self.default_scenario) for _ in range(self.num_workers)]
        for i, s in enumerate(scenarios):
            if "seed" not in s or s["seed"] == "RL_TRAIN_SEED":
                rand_id = int(self._rng.integers(1000, 999999))
                s["seed"] = f"RL_SEED_{i}_{rand_id}"

        self.current_states = self.pool.map(lambda w, s: w.reset(s), scenarios)

        for i, state in enumerate(self.current_states):
            obs = state.get("observation", {})
            self.previous_hp[i] = self._extract_player_hp(state)
            self.previous_enemy_hp[i] = self._extract_total_enemy_hp(obs)

        agent_observations = [extract_agent_observation(s) for s in self.current_states]
        legal_actions = [s.get("legal_actions", []) for s in self.current_states]
        masks = [bool(len(legals) > 0) for legals in legal_actions]
        legal_action_ids = [[a["action_id"] for a in legals] for legals in legal_actions]

        infos = {
            "legal_actions": legal_actions,
            "masks": masks,
            "legal_action_ids": legal_action_ids,
            "state_hashes": [s.get("state_hash", "") for s in self.current_states],
            "state_handles": [s.get("state_handle", "") for s in self.current_states],
        }
        return agent_observations, infos

    def reset_all(
        self,
        scenarios: Optional[List[Dict[str, Any]]] = None,
        seed: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Alias for reset() with optional explicit scenario overrides."""
        return self.reset(seed=seed, scenarios=scenarios)

    def step(
        self,
        action_ids: List[str],
    ) -> Tuple[List[Dict[str, Any]], List[float], List[bool], List[bool], Dict[str, Any]]:
        """
        Steps all workers simultaneously with the chosen action_id for each worker.
        Returns standard Gymnasium 5-tuple:
            (observations, rewards, terminations, truncations, infos)
        """
        assert len(action_ids) == self.num_workers

        self.current_states = self.pool.map(lambda w, act: w.step(act), action_ids)

        rewards = [0.0] * self.num_workers
        terminations = [False] * self.num_workers
        truncations = [False] * self.num_workers
        agent_observations = []
        legal_actions = []

        for i, state in enumerate(self.current_states):
            obs = state.get("observation", {})
            agent_obs = extract_agent_observation(state)
            agent_observations.append(agent_obs)
            legals = state.get("legal_actions", [])
            legal_actions.append(legals)

            terminated = bool(state.get("terminated", False))
            victory = bool(state.get("victory", False))

            if not terminated and not legals:
                raise RuntimeError(
                    f"Worker {i} invariant violation / deadlock: non-terminal state {state.get('state_hash')} has zero legal actions."
                )

            cur_hp = self._extract_player_hp(state)
            cur_enemy_hp = self._extract_total_enemy_hp(obs)

            # Dense signed reward shaping: enemy damage dealt + player HP progress
            enemy_progress = self.previous_enemy_hp[i] - cur_enemy_hp
            player_progress = cur_hp - self.previous_hp[i]
            reward = float(enemy_progress + 1.5 * player_progress)

            if victory:
                reward += 100.0  # Victory bonus
            elif terminated:
                reward -= 50.0   # Defeat penalty

            terminations[i] = terminated
            self.previous_hp[i] = cur_hp
            self.previous_enemy_hp[i] = cur_enemy_hp
            rewards[i] = reward

        masks = [bool(len(legals) > 0) for legals in legal_actions]
        legal_action_ids = [[a["action_id"] for a in legals] for legals in legal_actions]

        infos = {
            "legal_actions": legal_actions,
            "masks": masks,
            "legal_action_ids": legal_action_ids,
            "state_hashes": [s.get("state_hash", "") for s in self.current_states],
            "state_handles": [s.get("state_handle", "") for s in self.current_states],
        }
        return agent_observations, rewards, terminations, truncations, infos

    def _extract_player_hp(self, state: Dict[str, Any]) -> int:
        scoring = state.get("scoring_features")
        if isinstance(scoring, dict) and "current_hp" in scoring:
            return int(scoring["current_hp"])
        player = state.get("player") or state.get("observation", {}).get("player")
        if isinstance(player, dict) and "current_hp" in player:
            return int(player["current_hp"])
        creatures = state.get("observation", {}).get("combat", {}).get("creatures", [])
        player_creature = next((c for c in creatures if c.get("side") == "Player"), None)
        if player_creature:
            return int(player_creature.get("hp", 0))
        return 0

    def _extract_total_enemy_hp(self, obs: Dict[str, Any]) -> int:
        creatures = obs.get("combat", {}).get("creatures", [])
        return sum(c.get("hp", 0) for c in creatures if c.get("side") == "Enemy" and c.get("alive", False))

    def close(self):
        self.pool.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
