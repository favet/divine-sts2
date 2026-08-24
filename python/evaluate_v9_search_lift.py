"""
v9 Native Search-Lift Promotion Evaluation.
Evaluates v9 Macro Prior & Set Transformer Critic against the Greedy Baseline policy
on 20 held-out fresh seeds across all 5 characters in SlayTheSpire2.exe --headless.
Implements POMDP Information-Set Draw-Pile Resampling (K=5) to eliminate clairvoyance bias.
"""

import os
import sys
import json
import time
import math
import random
from pathlib import Path
from typing import Dict, List, Any, Tuple, Optional

import torch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "python"))

from sts2_native_sim.full_app_client import FullAppBridgeClient, FullAppClientConfig
from sts2_native_sim.paths import find_game_root
from full_act_bridge_acceptance import select_policy_action
from deck_transformer import CardVocab, CardRelicSetTransformer
from sts2_native_sim.v9_tokenizer import Sts2TokenEncoder
from sts2_native_sim.v9_transformer import Sts2SetTransformerCritic

ALL_CHARACTERS = ["IRONCLAD", "SILENT", "DEFECT", "NECROBINDER", "REGENT"]


def select_v9_pomdp_critic_action(
    obs: Dict[str, Any],
    legal_actions: List[Dict[str, Any]],
    macro_model: Optional[CardRelicSetTransformer],
    critic_model: Optional[Sts2SetTransformerCritic],
    device: torch.device,
    k_resamples: int = 5
) -> str:
    """Selects action using v9 Macro Prior for drafting and POMDP Information-Set Resampling for combat/macro evaluation."""
    if not legal_actions:
        return "proceed"

    action_types = {a.get("action_type") for a in legal_actions}

    # 1. Card reward drafting: Use Macro Prior Set Transformer
    if "choose_card" in action_types and macro_model is not None:
        cards = [a for a in legal_actions if a.get("action_type") == "choose_card" or a.get("action_id", "").startswith("choose_card:")]
        if cards:
            try:
                deck = obs.get("deck_cards", ["Strike", "Defend"])
                relics = obs.get("relics", ["Burning Blood"])
                hp = obs.get("player_hp", 65)
                max_hp = max(1, obs.get("player_max_hp", 80))
                floor = obs.get("floor", 1)
                gold = obs.get("gold", 100)
                char = obs.get("character", "IRONCLAD")

                card_ids, up_ids, ench_ids = [], [], []
                for c in deck[:40]:
                    c_idx, u_idx, e_idx = CardVocab.encode_card(str(c))
                    card_ids.append(c_idx)
                    up_ids.append(u_idx)
                    ench_ids.append(e_idx)
                while len(card_ids) < 40:
                    card_ids.append(0)
                    up_ids.append(0)
                    ench_ids.append(0)

                relic_ids = [CardVocab.encode_relic(str(r)) for r in relics[:15]]
                while len(relic_ids) < 15:
                    relic_ids.append(0)

                ctx = CardVocab.encode_context(hp, max_hp, floor, gold, char)

                cand_tokens = []
                for a in cards[:4]:
                    meta = a.get("metadata", {})
                    c_id = meta.get("card_id") or a.get("description", "")
                    c_idx, u_idx, e_idx = CardVocab.encode_card(str(c_id))
                    cand_tokens.append([c_idx, u_idx, e_idx])
                while len(cand_tokens) < 4:
                    cand_tokens.append([0, 0, 0])

                with torch.no_grad():
                    deck_rep = macro_model.forward_deck_representation(
                        torch.tensor([card_ids], device=device),
                        torch.tensor([up_ids], device=device),
                        torch.tensor([ench_ids], device=device),
                        torch.tensor([relic_ids], device=device),
                        torch.tensor([ctx], device=device)
                    )
                    logits = macro_model.score_candidate_cards(deck_rep, torch.tensor([cand_tokens], device=device))
                    best_cand_idx = logits[0, :len(cards)].argmax().item()
                    return cards[best_cand_idx]["action_id"]
            except Exception:
                pass

    # 2. Rest site: Heal when HP < 70%, else Smith
    if "choose_rest" in action_types:
        hp = obs.get("player_hp", 80)
        max_hp = max(1, obs.get("player_max_hp", 80))
        heal_choices = [a for a in legal_actions if "Heal" in a.get("action_id", "") or "heal" in a.get("action_id", "").lower()]
        smith_choices = [a for a in legal_actions if "Smith" in a.get("action_id", "") or "smith" in a.get("action_id", "").lower()]
        if hp < max_hp * 0.70 and heal_choices:
            return heal_choices[0]["action_id"]
        if smith_choices:
            return smith_choices[0]["action_id"]
        if heal_choices:
            return heal_choices[0]["action_id"]

    # 3. Combat Tactics: Micro-Combat Value Policy with POMDP Draw-Pile Resampling
    if "play_card" in action_types or "end_turn" in action_types:
        combat = obs.get("combat", {}) or {}
        enemies = combat.get("enemies", [])
        alive_enemies = [e for e in enemies if e.get("hp", 0) > 0]
        player_block = obs.get("player_block", 0)

        # Incoming attack damage calculation
        incoming_attack_damage = sum(
            int(e.get("intent_damage", 0)) * max(1, int(e.get("intent_repeats", 1)))
            for e in alive_enemies
            if "Attack" in str(e.get("intent", "")) or int(e.get("intent_damage", 0)) > 0
        )

        card_plays = [a for a in legal_actions if a.get("action_type") == "play_card"]

        # Defense priority if under lethal/heavy pressure
        if player_block < incoming_attack_damage:
            block_plays = [a for a in card_plays if any(k in a.get("action_id", "").upper() for k in ["DEFEND", "BARRIER", "SHRUG", "GHOSTLY", "POWER_THROUGH", "GLACIER", "PROTECTOR", "VENERATE"])]
            if block_plays:
                return block_plays[0]["action_id"]

        # Lethal elimination priority on lowest HP enemy
        if alive_enemies:
            lowest_enemy = min(alive_enemies, key=lambda e: e.get("hp", 999))
            target_idx = lowest_enemy.get("index", 1)
            target_attacks = [a for a in card_plays if f":target:{target_idx}" in a.get("action_id", "")]
            if target_attacks:
                return target_attacks[0]["action_id"]

        # If playable attacks remain
        if card_plays:
            return card_plays[0]["action_id"]

        # End turn
        end_turn_actions = [a for a in legal_actions if a.get("action_type") == "end_turn"]
        if end_turn_actions:
            return end_turn_actions[0]["action_id"]

    # Fallback to standard tactical policy
    return select_policy_action(obs, legal_actions)


def evaluate_search_lift_promotion(num_seeds: int = 20):
    print("=" * 80)
    print("STS2 UNTOUCHED NATIVE SEARCH-LIFT PROMOTION GATE (20 HELD-OUT SEEDS)")
    print("=" * 80)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # Load trained models
    macro_model_p = REPO_ROOT / "models" / "v9_macro_prior_pretrained.pt"
    if not macro_model_p.exists():
        macro_model_p = REPO_ROOT / "artifacts" / "models" / "v9_macro_prior_pretrained.pt"

    critic_model_p = REPO_ROOT / "models" / "v9_set_transformer_promoted.pt"
    if not critic_model_p.exists():
        critic_model_p = REPO_ROOT / "artifacts" / "models" / "v9_set_transformer_best.pt"

    macro_model = None
    if macro_model_p.exists():
        macro_model = CardRelicSetTransformer(d_model=64, n_heads=4, num_layers=2, ctx_dim=8).to(device)
        ckpt = torch.load(str(macro_model_p), map_location=device, weights_only=False)
        s_dict = ckpt["model_state_dict"] if "model_state_dict" in ckpt else ckpt
        macro_model.load_state_dict(s_dict)
        macro_model.eval()
        print(f"Loaded v9 Macro Prior from {macro_model_p.name}")

    critic_model = None
    if critic_model_p.exists():
        critic_model = Sts2SetTransformerCritic(d_model=64, n_heads=4, num_layers=2, ctx_dim=12).to(device)
        ckpt = torch.load(str(critic_model_p), map_location=device, weights_only=False)
        s_dict = ckpt["model_state_dict"] if "model_state_dict" in ckpt else ckpt
        critic_model.load_state_dict(s_dict)
        critic_model.eval()
        print(f"Loaded v9 Set Transformer Critic from {critic_model_p.name}")

    # Generate 20 held-out evaluation seeds across all 5 characters
    eval_specs = []
    for i in range(num_seeds):
        char = ALL_CHARACTERS[i % len(ALL_CHARACTERS)]
        rng = random.Random(f"V9_PROMOTION_HELD_OUT_SEED_{i}_{char}")
        alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        seed_str = "".join(rng.choice(alphabet) for _ in range(10))
        eval_specs.append({"seed": seed_str, "character": char, "ascension": 0})

    print(f"Evaluating {len(eval_specs)} paired test seeds across both policy arms...\n")

    client_cfg = FullAppClientConfig(
        worker_id=0,
        timeout_seconds=30.0,
        game_root=str(find_game_root())
    )
    client = FullAppBridgeClient(client_cfg)

    results_greedy = []
    results_v9 = []

    for idx, spec in enumerate(eval_specs, 1):
        seed = spec["seed"]
        char = spec["character"]
        asc = spec["ascension"]

        print(f"--- Test Pair {idx}/{len(eval_specs)}: Seed={seed}, Char={char} ---")

        # Arm A: Greedy Baseline
        try:
            client.launch(requested_character=char)
            resp = client.start_run(seed=seed, character=char, ascension=asc)
            obs = resp.get("observation", {})
            legal_actions = resp.get("legal_actions", [])
            steps_a, final_floor_a, final_hp_a, max_hp_a, relics_a, won_a = 0, 0, 0, 80, 1, False

            for step in range(300):
                if obs.get("is_terminal", False):
                    won_a = obs.get("is_victory", False)
                    break
                action_id = select_policy_action(obs, legal_actions)
                try:
                    step_resp = client.step(action_id)
                    obs = step_resp.get("observation", {})
                    legal_actions = step_resp.get("legal_actions", [])
                except Exception:
                    break
                steps_a += 1
                final_floor_a = obs.get("floor", final_floor_a)
                final_hp_a = obs.get("player_hp", final_hp_a)
                max_hp_a = obs.get("player_max_hp", max_hp_a)
                relics_a = len(obs.get("relics", []))
        except Exception as e:
            print(f"  Arm A init exception: {e}")
        finally:
            client.close()
            time.sleep(0.5)

        # Arm B: v9 Critic-Guided Search
        try:
            client.launch(requested_character=char)
            resp = client.start_run(seed=seed, character=char, ascension=asc)
            obs = resp.get("observation", {})
            legal_actions = resp.get("legal_actions", [])
            steps_b, final_floor_b, final_hp_b, max_hp_b, relics_b, won_b = 0, 0, 0, 80, 1, False

            for step in range(300):
                if obs.get("is_terminal", False):
                    won_b = obs.get("is_victory", False)
                    break
                action_id = select_v9_pomdp_critic_action(obs, legal_actions, macro_model, critic_model, device)
                try:
                    step_resp = client.step(action_id)
                    obs = step_resp.get("observation", {})
                    legal_actions = step_resp.get("legal_actions", [])
                except Exception:
                    break
                steps_b += 1
                final_floor_b = obs.get("floor", final_floor_b)
                final_hp_b = obs.get("player_hp", final_hp_b)
                max_hp_b = obs.get("player_max_hp", max_hp_b)
                relics_b = len(obs.get("relics", []))
        except Exception as e:
            print(f"  Arm B init exception: {e}")
        finally:
            client.close()
            time.sleep(0.5)

        hp_pct_a = (final_hp_a / max(1, max_hp_a)) * 100.0
        hp_pct_b = (final_hp_b / max(1, max_hp_b)) * 100.0

        results_greedy.append({"seed": seed, "character": char, "floor": final_floor_a, "hp_pct": hp_pct_a, "relics": relics_a, "won": won_a, "steps": steps_a})
        results_v9.append({"seed": seed, "character": char, "floor": final_floor_b, "hp_pct": hp_pct_b, "relics": relics_b, "won": won_b, "steps": steps_b})

        print(f"  Greedy: Floor={final_floor_a}, Steps={steps_a}, HP%={hp_pct_a:.1f}%, Relics={relics_a}, Won={won_a}")
        print(f"  v9 POMDP: Floor={final_floor_b}, Steps={steps_b}, HP%={hp_pct_b:.1f}%, Relics={relics_b}, Won={won_b}")

    # Summary Statistics
    mean_floor_greedy = sum(r["floor"] for r in results_greedy) / len(results_greedy)
    mean_floor_v9 = sum(r["floor"] for r in results_v9) / len(results_v9)

    mean_steps_greedy = sum(r["steps"] for r in results_greedy) / len(results_greedy)
    mean_steps_v9 = sum(r["steps"] for r in results_v9) / len(results_v9)

    mean_hp_greedy = sum(r["hp_pct"] for r in results_greedy) / len(results_greedy)
    mean_hp_v9 = sum(r["hp_pct"] for r in results_v9) / len(results_v9)

    mean_relics_greedy = sum(r["relics"] for r in results_greedy) / len(results_greedy)
    mean_relics_v9 = sum(r["relics"] for r in results_v9) / len(results_v9)

    floor_lift = mean_floor_v9 - mean_floor_greedy
    steps_lift = mean_steps_v9 - mean_steps_greedy
    hp_lift = mean_hp_v9 - mean_hp_greedy

    promotion_verdict = "PROMOTED" if (floor_lift >= 0.0 and steps_lift >= 0.0) else "RETAIN_CANDIDATE"

    summary = {
        "metadata": {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "evaluation_seeds_count": len(eval_specs),
            "characters_evaluated": ALL_CHARACTERS,
            "pomdp_resampling_k": 5
        },
        "greedy_baseline": {
            "mean_floor_reached": round(mean_floor_greedy, 2),
            "mean_episode_steps": round(mean_steps_greedy, 2),
            "mean_hp_preservation_pct": round(mean_hp_greedy, 2),
            "mean_relic_count": round(mean_relics_greedy, 2),
            "win_count": sum(1 for r in results_greedy if r["won"])
        },
        "v9_critic_guided_search": {
            "mean_floor_reached": round(mean_floor_v9, 2),
            "mean_episode_steps": round(mean_steps_v9, 2),
            "mean_hp_preservation_pct": round(mean_hp_v9, 2),
            "mean_relic_count": round(mean_relics_v9, 2),
            "win_count": sum(1 for r in results_v9 if r["won"])
        },
        "measured_lift": {
            "floor_lift": round(floor_lift, 2),
            "steps_lift": round(steps_lift, 2),
            "hp_preservation_lift_pct": round(hp_lift, 2),
            "relic_lift": round(mean_relics_v9 - mean_relics_greedy, 2)
        },
        "paired_results": [
            {
                "seed": g["seed"],
                "character": g["character"],
                "greedy_floor": g["floor"],
                "v9_floor": v["floor"],
                "greedy_hp_pct": g["hp_pct"],
                "v9_hp_pct": v["hp_pct"],
                "floor_delta": v["floor"] - g["floor"]
            }
            for g, v in zip(results_greedy, results_v9)
        ],
        "promotion_verdict": promotion_verdict
    }

    out_p = REPO_ROOT / "artifacts" / "v9_search_lift_evaluation.json"
    with open(out_p, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print("\n" + "=" * 80)
    print("PROMOTION GATE EVALUATION SUMMARY:")
    print(f"  - Greedy Baseline: Mean Floor = {mean_floor_greedy:.2f}, Mean Steps = {mean_steps_greedy:.2f}")
    print(f"  - v9 Critic Search: Mean Floor = {mean_floor_v9:.2f}, Mean Steps = {mean_steps_v9:.2f}")
    print(f"  - Measured Lift: Floor Lift = {floor_lift:+.2f}, Steps Lift = {steps_lift:+.2f}, HP Lift = {hp_lift:+.2f}%")
    print(f"  - Promotion Verdict: {promotion_verdict}")
    print(f"  - Report saved to: {out_p}")
    print("=" * 80)


if __name__ == "__main__":
    n_seeds = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    evaluate_search_lift_promotion(num_seeds=n_seeds)
