"""Train and promotion-gate a broad critic from shipped-native terminal rollouts."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import torch
import torch.nn as nn

sys.path.insert(0, str(Path(__file__).parent))
from sts2_native_sim import FEATURE_NAMES, NativeSimError, NativeTorchValueScorer, NativeWorkerPool, encode_scoring_features


ARCHETYPES = {
    "strength": ("IRONCLAD", ["INFLAME", "DEMON_FORM", "RUPTURE", "SWORD_BOOMERANG", "STRIKE_IRONCLAD", "DEFEND_IRONCLAD"]),
    "exhaust": ("IRONCLAD", ["FIEND_FIRE", "BURNING_PACT", "TRUE_GRIT", "FEEL_NO_PAIN", "DARK_EMBRACE", "STRIKE_IRONCLAD", "DEFEND_IRONCLAD"]),
    "poison": ("SILENT", ["DEADLY_POISON", "POISONED_STAB", "NOXIOUS_FUMES", "BOUNCING_FLASK", "STRIKE_SILENT", "DEFEND_SILENT"]),
    "shiv": ("SILENT", ["BLADE_DANCE", "CLOAK_AND_DAGGER", "ACCURACY", "SHIV", "STRIKE_SILENT", "DEFEND_SILENT"]),
    "orbs": ("DEFECT", ["ZAP", "DUALCAST", "BALL_LIGHTNING", "COLD_SNAP", "DEFRAGMENT", "STRIKE_DEFECT", "DEFEND_DEFECT"]),
    "summon": ("NECROBINDER", ["SUMMON_FORTH", "NECRO_MASTERY", "SOUL_STORM", "LEGION_OF_BONE", "STRIKE_NECROBINDER", "DEFEND_NECROBINDER"]),
    "stars": ("REGENT", ["SOVEREIGN_BLADE", "STARDUST", "FALLING_STAR", "GUIDING_STAR", "SEVEN_STARS", "STRIKE_REGENT", "DEFEND_REGENT"]),
}
HP_RATIOS = (1.0, 0.4, 0.15)
DECK_SIZES = (10, 35)
POLICIES = ("greedy", "heuristic", "epsilon")
DATASET_VERSION = "v8-breadth"


def stable_int(text: str) -> int:
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "little")


def evenly_spaced(values: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    if count >= len(values):
        return values
    if count == 1:
        return [values[len(values) // 2]]
    return [values[round(index * (len(values) - 1) / (count - 1))] for index in range(count)]


def validate_archetypes(cards: list[dict[str, Any]]) -> dict[str, str]:
    card_types = {card["model_id"]: str(card.get("card_type", "unknown")).lower() for card in cards}
    missing = sorted({card for _, pool in ARCHETYPES.values() for card in pool if card not in card_types})
    if missing:
        raise RuntimeError(f"native archetype matrix references missing cards: {missing}")
    return card_types


def profile_spec(encounter: str, archetype: str, deck_size: int, hp_ratio: float, seed: str, card_types: dict[str, str]) -> dict[str, Any]:
    character, pool = ARCHETYPES[archetype]
    strike = next(card for card in pool if card.startswith("STRIKE_"))
    defend = next(card for card in pool if card.startswith("DEFEND_"))
    engine = [card for card in pool if card not in (strike, defend)]
    attack_count = max(3, round(deck_size * 0.35))
    defend_count = max(2, round(deck_size * 0.25))
    engine_count = deck_size - attack_count - defend_count
    groups = [[engine[index % len(engine)] for index in range(engine_count)], [strike] * attack_count, [defend] * defend_count]
    models = []
    while any(groups):
        for group in groups:
            if group:
                models.append(group.pop(0))
    cards = []
    for ordinal, model_id in enumerate(models):
        kind = card_types[model_id]
        cards.append({"instance_id": f"{kind}-{model_id.lower()}-{ordinal}", "model_id": model_id, "upgrades": int(ordinal % 7 == 0)})
    return {
        "game_build": {}, "seed": seed, "rng_counters": {}, "character": character, "ascension": 0,
        "encounter": encounter, "current_hp": max(1, round(80 * hp_ratio)), "max_hp": 80, "gold": 99,
        "deck": cards, "initial_hand": [card["instance_id"] for card in cards[:5]],
        "relics": [], "potions": [], "energy": 3,
    }


def choose_action(state: dict[str, Any], policy: str, rng: random.Random) -> str:
    legal = state["legal_actions"]
    if not legal:
        raise RuntimeError(f"nonterminal state {state['state_hash']} exposed no legal actions")
    combat = state["scoring_features"].get("combat") or {}
    piles = combat.get("piles", {})
    non_exhausted = [card for name in ("hand", "draw", "discard", "play") for card in piles.get(name, [])]
    repeatable_attacks = sum(str(card.get("model_id", "")).startswith("STRIKE_") for card in non_exhausted)
    if repeatable_attacks <= 8:
        exhaustors = ("fiend_fire", "true_grit", "burning_pact")
        safe = [action for action in legal if not (action["kind"] == "play_card" and any(name in str(action["parameters"].get("instance_id", "")) for name in exhaustors))]
        if safe:
            legal = safe
    if policy == "epsilon" and rng.random() < 0.30:
        return rng.choice(legal)["action_id"]
    choices = [action for action in legal if action["kind"] == "choose_cards"]
    if choices:
        non_attacks = [action for action in choices if "strike_" not in action["action_id"].lower()]
        choices = non_attacks or choices
        return rng.choice(choices)["action_id"] if policy == "epsilon" else choices[0]["action_id"]
    plays = [action for action in legal if action["kind"] == "play_card"]
    if plays:
        hp_ratio = state["scoring_features"]["current_hp"] / max(1, state["scoring_features"]["max_hp"])
        if policy == "greedy":
            priorities = ("attack-", "power-", "skill-")
        elif hp_ratio <= 0.25:
            priorities = ("skill-defend_", "power-", "skill-", "attack-")
        else:
            priorities = ("power-", "attack-", "skill-")
        for prefix in priorities:
            selected = next((action for action in plays if str(action["parameters"].get("instance_id", "")).startswith(prefix)), None)
            if selected is not None:
                return selected["action_id"]
        return plays[0]["action_id"]
    return next((action["action_id"] for action in legal if action["kind"] == "end_turn"), legal[0]["action_id"])


def terminal_return(state: dict[str, Any]) -> float:
    features = state["scoring_features"]
    if state["victory"]:
        return 0.75 + 0.25 * features["current_hp"] / max(1, features["max_hp"])
    combat = features.get("combat") or {}
    enemies = [creature for creature in combat.get("creatures", []) if creature.get("side") == "Enemy"]
    remaining = sum(max(0, creature["hp"]) for creature in enemies)
    maximum = max(1, sum(max(0, creature["max_hp"]) for creature in enemies))
    return 0.25 * (1.0 - remaining / maximum)


def rollout(worker: Any, task: dict[str, Any]) -> dict[str, Any]:
    state = worker.reset(task["spec"])
    for action_id in task.get("prefix_actions", []):
        state = worker.step(action_id)
    if task.get("first_action") is not None:
        state = worker.step(task["first_action"])
    child_features = encode_scoring_features(state)
    samples = []
    rng = random.Random(stable_int(task["rollout_seed"]))
    attempted_at_state: set[tuple[str, str]] = set()
    for step in range(500):
        if state["terminated"]:
            stride = max(1, math.ceil(len(samples) / 25))
            return {
                "task_id": task["task_id"], "root_id": task.get("root_id"), "action_id": task.get("first_action"),
                "policy": task["policy"], "return": terminal_return(state), "victory": state["victory"],
                "steps": step, "terminal_hash": state["state_hash"], "child_features": child_features,
                "samples": samples[::stride][-25:],
            }
        samples.append(encode_scoring_features(state))
        try:
            action_id = choose_action(state, task["policy"], rng)
        except RuntimeError as error:
            raise RuntimeError(json.dumps({"error": str(error), "task_id": task["task_id"], "spec": task["spec"], "step": step, "observation": state["observation"], "scoring_features": state["scoring_features"]}, default=str)) from error
        cycle_key = (state["state_hash"], action_id)
        if cycle_key in attempted_at_state:
            alternatives = [action["action_id"] for action in state["legal_actions"] if (state["state_hash"], action["action_id"]) not in attempted_at_state]
            action_id = next((candidate for candidate in alternatives if candidate == "end_turn"), alternatives[0] if alternatives else action_id)
            cycle_key = (state["state_hash"], action_id)
        attempted_at_state.add(cycle_key)
        try:
            state = worker.step(action_id)
        except NativeSimError as error:
            raise RuntimeError(json.dumps({"task": task, "step": step, "action_id": action_id, "details": error.details, "diagnostics": worker.diagnostics()}, default=str)) from error
    raise RuntimeError(json.dumps({"error": "native rollout exceeded 500 decisions", "task_id": task["task_id"], "state_hash": state["state_hash"], "scoring_features": state["scoring_features"], "legal_actions": state["legal_actions"]}, default=str))


def write_cache(path: Path, cache: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(cache, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


def run_tasks(pool: NativeWorkerPool, tasks: list[dict[str, Any]], cache: dict[str, dict[str, Any]], cache_path: Path) -> list[dict[str, Any]]:
    pending = [task for task in tasks if task["task_id"] not in cache]
    for batch_index, offset in enumerate(range(0, len(pending), len(pool.workers))):
        batch = pending[offset:offset + len(pool.workers)]
        try:
            completed = pool.map(rollout, batch)
        except Exception:
            write_cache(cache_path, cache)
            raise
        cache.update({result["task_id"]: result for result in completed})
        if batch_index % 10 == 9:
            write_cache(cache_path, cache)
    write_cache(cache_path, cache)
    return [cache[task["task_id"]] for task in tasks]


def ablate_enemy_identity(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    columns = [index for index, name in enumerate(FEATURE_NAMES) if name.startswith("enemy_hash_")]
    def transform(values: list[float]) -> list[float]:
        result = list(values)
        for index in columns:
            result[index] = 0.0
        return result
    return [{
        **row,
        **({"child_features": transform(row["child_features"])} if "child_features" in row else {}),
        **({"samples": [transform(sample) for sample in row["samples"]]} if "samples" in row else {}),
    } for row in rows]


def select_encounter_matrix(encounters: list[dict[str, Any]], per_stratum: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    strata: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for encounter in encounters:
        if "TEST" in encounter["model_id"] or "DEBUG" in encounter["model_id"]:
            continue
        tier = {"Monster": "hallway", "Elite": "elite", "Boss": "boss"}.get(encounter.get("room_type"))
        if tier is None:
            continue
        for act in encounter.get("act_indices", []):
            if act in (1, 2, 3):
                strata[(act, tier)].append(encounter)
    missing = sorted({(act, tier) for act in (1, 2, 3) for tier in ("hallway", "elite", "boss")} - set(strata))
    if missing:
        raise RuntimeError(f"native catalog lacks encounter strata: {missing}")
    training, validation, promotion_test = [], [], []
    for key in sorted(strata):
        values = sorted(strata[key], key=lambda entry: entry["model_id"])
        selected = values if per_stratum == 0 else evenly_spaced(values, min(per_stratum, len(values)))
        validation.append(selected[-1])
        promotion_test.append(selected[-2])
        training.extend(selected[:-2])
    unique = lambda values: list({value["model_id"]: value for value in values}.values())
    training, validation, promotion_test = unique(training), unique(validation), unique(promotion_test)
    reserved_ids = {entry["model_id"] for entry in validation + promotion_test}
    return [entry for entry in training if entry["model_id"] not in reserved_ids], validation, promotion_test


def candidate_action_ids(state: dict[str, Any], count: int) -> list[str]:
    plays = [action["action_id"] for action in state["legal_actions"] if action["kind"] == "play_card"]
    ends = [action["action_id"] for action in state["legal_actions"] if action["kind"] == "end_turn"]
    play_budget = max(0, count - len(ends))
    selected_plays = [entry["action_id"] for entry in evenly_spaced([{"action_id": action_id} for action_id in plays], min(play_budget, len(plays)))]
    return (selected_plays + ends)[:count]


def trajectory_roots(worker: Any, spec: dict[str, Any], seed: str, count: int) -> list[dict[str, Any]]:
    state = worker.reset(spec)
    rng = random.Random(stable_int(seed))
    history: list[str] = []
    roots = []
    for _ in range(500):
        if state["terminated"]:
            break
        roots.append({"prefix_actions": list(history), "state": state})
        action_id = choose_action(state, "heuristic", rng)
        history.append(action_id)
        state = worker.step(action_id)
    else:
        raise RuntimeError(f"ranking trajectory exceeded 500 decisions: {seed}")
    return evenly_spaced(roots, min(count, len(roots)))


def pairwise_gate(model: Any, results: list[dict[str, Any]], minimum_gap: float, minimum_pairs: int, threshold: float) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        grouped[(result["root_id"], result["action_id"])].append(result)
    roots: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with torch.no_grad():
        for (root_id, action_id), rollouts in grouped.items():
            features = torch.tensor([rollouts[0]["child_features"]], dtype=torch.float32)
            roots[root_id].append({"action_id": action_id, "target": sum(row["return"] for row in rollouts) / len(rollouts), "score": float(model(features).item())})
    pairs = correct = ties = 0
    root_rows = []
    for root_id, actions in sorted(roots.items()):
        root_pairs = root_correct = 0
        for left_index, left in enumerate(actions):
            for right in actions[left_index + 1:]:
                delta = left["target"] - right["target"]
                if abs(delta) < minimum_gap:
                    ties += 1
                    continue
                predicted = left["score"] - right["score"]
                root_pairs += 1; pairs += 1
                if predicted * delta > 0:
                    root_correct += 1; correct += 1
        root_rows.append({"root_id": root_id, "actions": actions, "comparable_pairs": root_pairs, "correct_pairs": root_correct})
    accuracy = correct / pairs if pairs else 0.0
    promoted = pairs >= minimum_pairs and accuracy >= threshold
    return {"promoted": promoted, "accuracy": accuracy, "correct_pairs": correct, "comparable_pairs": pairs, "excluded_near_ties": ties, "minimum_gap": minimum_gap, "minimum_pairs": minimum_pairs, "threshold": threshold, "roots": root_rows}


def ranking_examples(results: list[dict[str, Any]], minimum_gap: float = 0.01) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        grouped[(result["root_id"], result["action_id"])].append(result)
    roots: dict[str, list[tuple[list[float], float]]] = defaultdict(list)
    for (root_id, _), rollouts in grouped.items():
        roots[root_id].append((rollouts[0]["child_features"], sum(row["return"] for row in rollouts) / len(rollouts)))
    left, right, signs = [], [], []
    for actions in roots.values():
        for left_index, (left_features, left_target) in enumerate(actions):
            for right_features, right_target in actions[left_index + 1:]:
                if abs(left_target - right_target) < minimum_gap:
                    continue
                left.append(left_features); right.append(right_features); signs.append(1.0 if left_target > right_target else -1.0)
    if not left:
        raise RuntimeError("training sibling rollouts produced no comparable ranking pairs")
    return torch.tensor(left, dtype=torch.float32), torch.tensor(right, dtype=torch.float32), torch.tensor(signs, dtype=torch.float32).unsqueeze(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--encounters-per-stratum", type=int, default=0, help="Zero uses every encounter while reserving one validation and one promotion model per Act x tier.")
    parser.add_argument("--training-profiles-per-encounter", type=int, default=14)
    parser.add_argument("--holdout-profiles", type=int, default=7)
    parser.add_argument("--training-pair-profiles", type=int, default=1)
    parser.add_argument("--training-ranking-depths", type=int, default=3)
    parser.add_argument("--rollouts-per-action", type=int, default=9)
    parser.add_argument("--candidate-actions", type=int, default=4)
    parser.add_argument("--epochs", type=int, default=900)
    parser.add_argument("--ranking-weight", type=float, default=4.0)
    parser.add_argument("--pointwise-weight", type=float, default=0.1)
    parser.add_argument("--torch-seed", type=int, default=31)
    parser.add_argument("--hidden-sizes", default="128,64")
    parser.add_argument("--validation-only", action="store_true", help="Do not generate or inspect promotion labels.")
    parser.add_argument("--minimum-pairs", type=int, default=50)
    parser.add_argument("--promotion-threshold", type=float, default=0.90)
    parser.add_argument("--output", type=Path, default=Path("artifacts/native-value-matrix.pt"))
    parser.add_argument("--cache", type=Path, default=Path("artifacts/native-value-matrix-v8-breadth-rollouts.json"))
    args = parser.parse_args()
    if args.encounters_per_stratum not in (0,) and args.encounters_per_stratum < 3:
        raise ValueError("encounters-per-stratum must be zero or at least three to preserve validation and untouched promotion sets")
    if args.training_profiles_per_encounter < 1:
        raise ValueError("training-profiles-per-encounter must be positive")
    hidden_sizes = tuple(int(value) for value in args.hidden_sizes.split(",") if value.strip())
    if not hidden_sizes or any(value < 1 for value in hidden_sizes):
        raise ValueError("hidden-sizes must be a comma-separated list of positive integers")
    torch.manual_seed(args.torch_seed)
    cache = json.loads(args.cache.read_text(encoding="utf-8")) if args.cache.exists() else {}
    with NativeWorkerPool(4) as pool:
        catalog = pool.workers[0].catalog()
        card_types = validate_archetypes(catalog["cards"])
        train_encounters, validation_encounters, promotion_test_encounters = select_encounter_matrix(catalog["encounters"], args.encounters_per_stratum)
        variants = [(name, size, hp) for name in ARCHETYPES for size in DECK_SIZES for hp in HP_RATIOS]
        train_tasks = []
        for encounter in train_encounters:
            training_profiles = evenly_spaced(variants, min(args.training_profiles_per_encounter, len(variants)))
            for archetype, deck_size, hp_ratio in training_profiles:
                profile_id = f"{DATASET_VERSION}:train:{encounter['model_id']}:{archetype}:{deck_size}:{hp_ratio}"
                spec = profile_spec(encounter["model_id"], archetype, deck_size, hp_ratio, f"NATIVE-MATRIX-{profile_id}", card_types)
                for policy in POLICIES:
                    identity = f"{profile_id}:{policy}"
                    train_tasks.append({"task_id": identity, "spec": spec, "policy": policy, "rollout_seed": identity})
        training = ablate_enemy_identity(run_tasks(pool, train_tasks, cache, args.cache))

        ranking_tasks = []
        archetypes = list(ARCHETYPES)
        for encounter_index, encounter in enumerate(train_encounters):
            for profile_index in range(args.training_pair_profiles):
                archetype = archetypes[(encounter_index + profile_index) % len(archetypes)]
                deck_size = DECK_SIZES[(encounter_index + profile_index) % len(DECK_SIZES)]
                hp_ratio = HP_RATIOS[(encounter_index + profile_index) % len(HP_RATIOS)]
                root_id = f"{DATASET_VERSION}:ranking-train:{encounter['model_id']}:{archetype}:{deck_size}:{hp_ratio}"
                spec = profile_spec(encounter["model_id"], archetype, deck_size, hp_ratio, f"NATIVE-MATRIX-{root_id}", card_types)
                for depth_index, trajectory_root in enumerate(trajectory_roots(pool.workers[0], spec, root_id, args.training_ranking_depths)):
                    depth_root_id = f"{root_id}:depth-{depth_index}"
                    for action_id in candidate_action_ids(trajectory_root["state"], args.candidate_actions):
                        for rollout_index in range(args.rollouts_per_action):
                            policy = POLICIES[rollout_index % len(POLICIES)]
                            identity = f"{depth_root_id}:{action_id}:{policy}:{rollout_index}"
                            ranking_tasks.append({"task_id": identity, "root_id": depth_root_id, "prefix_actions": trajectory_root["prefix_actions"], "first_action": action_id, "spec": spec, "policy": policy, "rollout_seed": identity})
        ranking_training = ablate_enemy_identity(run_tasks(pool, ranking_tasks, cache, args.cache))

        evaluation_tasks = []
        holdout_roots = []
        for encounter_index, encounter in enumerate(validation_encounters):
            for profile_index in range(args.holdout_profiles):
                archetype = archetypes[(encounter_index * args.holdout_profiles + profile_index) % len(archetypes)]
                deck_size = DECK_SIZES[(encounter_index + profile_index) % len(DECK_SIZES)]
                hp_ratio = HP_RATIOS[(encounter_index + 2 * profile_index) % len(HP_RATIOS)]
                root_id = f"{DATASET_VERSION}:holdout:{encounter['model_id']}:{archetype}:{deck_size}:{hp_ratio}"
                spec = profile_spec(encounter["model_id"], archetype, deck_size, hp_ratio, f"NATIVE-MATRIX-{root_id}", card_types)
                root = pool.workers[0].reset(spec)
                action_ids = candidate_action_ids(root, args.candidate_actions)
                if len(action_ids) < 2:
                    continue
                holdout_roots.append({"root_id": root_id, "encounter": encounter, "archetype": archetype, "deck_size": deck_size, "hp_ratio": hp_ratio, "actions": action_ids})
                for action_id in action_ids:
                    for rollout_index in range(args.rollouts_per_action):
                        policy = POLICIES[rollout_index % len(POLICIES)]
                        identity = f"{root_id}:{action_id}:{policy}:{rollout_index}"
                        evaluation_tasks.append({"task_id": identity, "root_id": root_id, "first_action": action_id, "spec": spec, "policy": policy, "rollout_seed": identity})
        evaluation = ablate_enemy_identity(run_tasks(pool, evaluation_tasks, cache, args.cache))

        promotion_tasks = []
        promotion_roots = []
        if not args.validation_only:
            for encounter_index, encounter in enumerate(promotion_test_encounters):
                for profile_index in range(args.holdout_profiles):
                    archetype = archetypes[(encounter_index * args.holdout_profiles + profile_index) % len(archetypes)]
                    deck_size = DECK_SIZES[(encounter_index + profile_index) % len(DECK_SIZES)]
                    hp_ratio = HP_RATIOS[(encounter_index + 2 * profile_index) % len(HP_RATIOS)]
                    root_id = f"{DATASET_VERSION}:promotion-test:{encounter['model_id']}:{archetype}:{deck_size}:{hp_ratio}"
                    spec = profile_spec(encounter["model_id"], archetype, deck_size, hp_ratio, f"NATIVE-MATRIX-{root_id}", card_types)
                    root = pool.workers[0].reset(spec)
                    action_ids = candidate_action_ids(root, args.candidate_actions)
                    if len(action_ids) < 2:
                        continue
                    promotion_roots.append({"root_id": root_id, "encounter": encounter, "archetype": archetype, "deck_size": deck_size, "hp_ratio": hp_ratio, "actions": action_ids})
                    for action_id in action_ids:
                        for rollout_index in range(args.rollouts_per_action):
                            policy = POLICIES[rollout_index % len(POLICIES)]
                            identity = f"{root_id}:{action_id}:{policy}:{rollout_index}"
                            promotion_tasks.append({"task_id": identity, "root_id": root_id, "first_action": action_id, "spec": spec, "policy": policy, "rollout_seed": identity})
            promotion_evaluation = ablate_enemy_identity(run_tasks(pool, promotion_tasks, cache, args.cache))
        else:
            promotion_evaluation = []
        build = pool.workers[0].build

    train_x = [sample for episode in training for sample in episode["samples"]]
    train_y = [episode["return"] for episode in training for _ in episode["samples"]]
    x = torch.tensor(train_x, dtype=torch.float32)
    y = torch.tensor(train_y, dtype=torch.float32).unsqueeze(1)
    rank_left, rank_right, rank_sign = ranking_examples(ranking_training)
    model = NativeTorchValueScorer.create_model(hidden_sizes)
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.003, weight_decay=1e-4)
    model.train()
    for _ in range(args.epochs):
        optimizer.zero_grad()
        prediction = torch.sigmoid(model(x))
        positive_weight = max(1.0, float((y < 0.5).sum()) / max(1.0, float((y >= 0.5).sum())))
        weights = torch.where(y >= 0.5, positive_weight, 1.0)
        value_loss = ((prediction - y).square() * weights).mean()
        ranking_loss = torch.nn.functional.softplus(-rank_sign * (model(rank_left) - model(rank_right))).mean()
        loss = args.pointwise_weight * value_loss + args.ranking_weight * ranking_loss
        loss.backward(); optimizer.step()
    model.eval()
    validation_gate = pairwise_gate(model, evaluation, minimum_gap=0.02, minimum_pairs=args.minimum_pairs, threshold=args.promotion_threshold)
    gate = pairwise_gate(model, promotion_evaluation, minimum_gap=0.02, minimum_pairs=args.minimum_pairs, threshold=args.promotion_threshold) if promotion_evaluation else {"promoted": False, "accuracy": None, "correct_pairs": 0, "comparable_pairs": 0, "excluded_near_ties": 0, "minimum_gap": 0.02, "minimum_pairs": args.minimum_pairs, "threshold": args.promotion_threshold, "roots": []}
    metadata = {
        "game_build": build, "intended_use": "native_search_value_critic" if gate["promoted"] else "native_matrix_candidate_not_promoted",
        "certifying": False, "promotion": {key: value for key, value in gate.items() if key != "roots"}, "validation_ranking": {key: value for key, value in validation_gate.items() if key != "roots"},
        "label_definition": "policy-mixture terminal return: victory survival value or loss enemy-damage progress, all transitions shipped-native",
        "distribution_matrix": {"archetypes": list(ARCHETYPES), "characters": sorted({value[0] for value in ARCHETYPES.values()}), "deck_sizes": list(DECK_SIZES), "hp_ratios": list(HP_RATIOS), "policies": list(POLICIES), "train_encounters": [entry["model_id"] for entry in train_encounters], "validation_encounters": [entry["model_id"] for entry in validation_encounters], "promotion_test_encounters": [entry["model_id"] for entry in promotion_test_encounters]},
        "dataset_version": DATASET_VERSION, "model_hidden_sizes": list(hidden_sizes), "episode_count": len(training), "training_samples": len(train_x), "training_ranking_rollouts": len(ranking_training), "training_ranking_pairs": len(rank_sign), "validation_rollouts": len(evaluation), "promotion_test_rollouts": len(promotion_evaluation), "torch_seed": args.torch_seed, "ranking_weight": args.ranking_weight, "pointwise_weight": args.pointwise_weight, "enemy_identity_features": "ablated_for_unseen_encounter_generalization",
    }
    NativeTorchValueScorer.save(args.output, model, metadata)
    print(json.dumps({"success": True, "output": str(args.output.resolve()), "promoted": gate["promoted"], "game_build": build, "training_episodes": len(training), "training_samples": len(train_x), "training_ranking_rollouts": len(ranking_training), "training_ranking_pairs": len(rank_sign), "training_outcomes": {"victories": sum(row["victory"] for row in training), "losses": sum(not row["victory"] for row in training)}, "validation_roots": len(holdout_roots), "validation_rollouts": len(evaluation), "validation_gate": validation_gate, "promotion_test_roots": len(promotion_roots), "promotion_test_rollouts": len(promotion_evaluation), "promotion_gate": gate, "distribution_matrix": metadata["distribution_matrix"]}, indent=2))


if __name__ == "__main__":
    main()
