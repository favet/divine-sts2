import os
import sys
import glob
import json
import gzip
import time
import math
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple, Set

from extract_agentic_macro_decisions import extract_archives

REPO_ROOT = Path(__file__).resolve().parents[1]


def normalize_card_name(raw: str) -> str:
    if not raw or not isinstance(raw, str):
        return ""
    c = raw.strip().replace("CARD.", "").replace("Card.", "")
    c = c.split("+")[0].split(" (")[0].split("(")[0].strip()
    return c.upper().replace(" ", "_").replace("-", "_")


def normalize_character_name(raw: str) -> str:
    if not raw or not isinstance(raw, str):
        return "UNKNOWN"
    c = raw.strip().upper().replace("CHARACTER.", "")
    if "IRONCLAD" in c:
        return "IRONCLAD"
    if "SILENT" in c:
        return "SILENT"
    if "DEFECT" in c:
        return "DEFECT"
    if "NECROBINDER" in c or "NECRO" in c:
        return "NECROBINDER"
    if "REGENT" in c:
        return "REGENT"
    return c


class CommunityRunIngestionEngine:
    def __init__(self, repo_root: Optional[Path] = None):
        self.repo_root = repo_root or REPO_ROOT
        self.game_db_dir = self.repo_root / "game_database"
        self.community_dir = self.repo_root / "community_runs"
        self.artifacts_dir = self.repo_root / "native_sim" / "artifacts"
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.runs: List[Dict[str, Any]] = []
        self.macro_decisions: List[Dict[str, Any]] = []

    def ingest_all_sources(self) -> Dict[str, int]:
        counts = defaultdict(int)
        master_manifest_p = self.game_db_dir / "full_corpus" / "master_runs_manifest.json"
        if master_manifest_p.exists():
            with open(master_manifest_p, "r", encoding="utf-8") as f:
                manifest_data = json.load(f)
                for item in manifest_data:
                    win = (str(item.get("result", "")).upper() == "WIN")
                    char = normalize_character_name(item.get("character", ""))
                    asc = int(item.get("ascension", 0)) if str(item.get("ascension", 0)).isdigit() else 0
                    self.runs.append({
                        "run_hash": item.get("run_hash", ""),
                        "character": char,
                        "ascension": asc,
                        "win": win,
                        "floor_count": int(item.get("floors", 0)) if str(item.get("floors", 0)).isdigit() else 0,
                        "source": "master_manifest",
                        "deck": [],
                        "relics": [],
                        "timeline": []
                    })
                    counts["master_manifest"] += 1

        raw_run_dirs = [
            self.game_db_dir / "full_corpus" / "raw_runs",
            self.game_db_dir / "synergy_dataset" / "raw_a10_runs",
            self.game_db_dir / "synergy_dataset" / "raw_a1_a9_runs"
        ]
        seen_hashes = {r["run_hash"] for r in self.runs if r["run_hash"]}

        for raw_dir in raw_run_dirs:
            if not raw_dir.exists():
                continue
            for fpath in raw_dir.glob("*.json"):
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    r_hash = data.get("run_hash", "")
                    win = (str(data.get("result", "")).upper() == "WIN")
                    char = normalize_character_name(data.get("character", ""))
                    asc = int(data.get("ascension", 0)) if str(data.get("ascension", 0)).isdigit() else 0
                    raw_deck = [normalize_card_name(c) for c in data.get("deck", [])]
                    raw_deck = [c for c in raw_deck if c]
                    raw_relics = data.get("relics", [])
                    timeline = data.get("timeline", [])

                    run_entry = {
                        "run_hash": r_hash or fpath.stem,
                        "character": char,
                        "ascension": asc,
                        "win": win,
                        "floor_count": int(data.get("floor_count", len(timeline))),
                        "source": "raw_detailed_run",
                        "deck": raw_deck,
                        "relics": raw_relics,
                        "timeline": timeline
                    }

                    if r_hash and r_hash in seen_hashes:
                        for existing in self.runs:
                            if existing.get("run_hash") == r_hash:
                                existing["deck"] = raw_deck
                                existing["relics"] = raw_relics
                                existing["timeline"] = timeline
                                break
                    else:
                        self.runs.append(run_entry)
                        if r_hash:
                            seen_hashes.add(r_hash)

                    counts["raw_detailed_runs"] += 1
                    # Timeline reward text records acquired outcomes, not the
                    # offered option set. It must never be converted into a
                    # supervised choice label.
                except Exception:
                    continue

        tr_dataset_p = self.game_db_dir / "training_runs_dataset.json"
        if tr_dataset_p.exists():
            try:
                with open(tr_dataset_p, "r", encoding="utf-8") as f:
                    tr_data = json.load(f)
                for tr in tr_data:
                    win = tr.get("victory", False)
                    decisions = tr.get("decisions", [])
                    char = normalize_character_name(tr.get("character", "IRONCLAD"))
                    asc = int(tr.get("ascension", 0))
                    for d in decisions:
                        offered = [normalize_card_name(c) for c in d.get("offered", []) if c]
                        picked = normalize_card_name(d.get("picked", "SKIP"))
                        if offered:
                            self.macro_decisions.append({
                                "decision_type": "card_reward",
                                "character": char,
                                "ascension": asc,
                                "floor": d.get("floor", 1),
                                "offered": offered,
                                "picked": picked,
                                "run_won": win
                            })
                    counts["training_runs_dataset"] += 1
            except Exception:
                pass

        expert_runs_p = self.game_db_dir / "human_expert_runs.json"
        if expert_runs_p.exists():
            try:
                with open(expert_runs_p, "r", encoding="utf-8") as f:
                    exp_data = json.load(f)
                for exp in exp_data:
                    for d in exp.get("decisions", []):
                        offered = [normalize_card_name(c) for c in d.get("offered", []) if c]
                        picked = normalize_card_name(d.get("picked", "SKIP"))
                        if offered:
                            self.macro_decisions.append({
                                "decision_type": "card_reward",
                                "character": normalize_character_name(d.get("character", "IRONCLAD")),
                                "ascension": int(d.get("ascension", 0)),
                                "floor": d.get("floor", 1),
                                "deck_snapshot": [normalize_card_name(c) for c in d.get("deck_snapshot", [])],
                                "relics_snapshot": d.get("relics_snapshot", []),
                                "offered": offered,
                                "picked": picked,
                                "run_won": d.get("run_won", False)
                            })
                    counts["human_expert_runs"] += 1
            except Exception:
                pass

        agentic_paths = sorted((self.community_dir / "agentic_sts").glob("*.jsonl.gz"))
        if agentic_paths:
            exact_examples, report = extract_archives(agentic_paths)
            counts["agentic_gz_archives"] = len(agentic_paths)
            counts["agentic_exact_decisions"] = self._ingest_agentic_decisions(exact_examples)
            counts["agentic_first_option_rate_ppm"] = round(
                float(report.get("card_reward_first_option_rate", 0.0)) * 1_000_000
            )

        return dict(counts)

    def _extract_timeline_decisions(self, run: Dict[str, Any]) -> None:
        raise RuntimeError("Timeline summaries are outcome data, not option-aligned decisions")

    def _ingest_agentic_decisions(self, examples: List[Dict[str, Any]]) -> int:
        before = len(self.macro_decisions)
        for example in examples:
            state = example.get("state") or {}
            state_type = example.get("state_type")
            action_name = example.get("action_name")
            candidates = example.get("candidates") or []
            selected_index = example.get("selected_index")
            deck = [
                normalize_card_name(card.get("name", "")) + ("+" if card.get("upgraded") else "")
                for card in state.get("deck") or []
                if card.get("name")
            ]
            common = {
                "character": "SILENT",
                "ascension": int(state.get("ascension", 0)),
                "floor": int(example.get("floor", 1)),
                "hp": int(state.get("hp", 0)),
                "max_hp": int(state.get("hp_max", 1)),
                "gold": int((state.get("player") or {}).get("gold", 0)),
                "deck_snapshot": deck,
                "run_won": bool((example.get("outcome") or {}).get("victory", False)),
                "source": "agentic_exact_state_action",
                "run_id": example.get("run_id"),
                "step": example.get("step"),
            }
            selected = next(
                (candidate for candidate in candidates
                 if candidate.get("_action_name") == action_name
                 and (selected_index is None or int(candidate.get("index", -1)) == int(selected_index))),
                None,
            )

            if state_type == "card_reward":
                offered = [
                    normalize_card_name(candidate.get("name", ""))
                    for candidate in candidates
                    if candidate.get("_action_name") == "choose_reward_card" and candidate.get("name")
                ]
                picked = (
                    normalize_card_name(selected.get("name", ""))
                    if selected and action_name == "choose_reward_card" else "SKIP"
                )
                self.macro_decisions.append({
                    **common, "decision_type": "card_reward", "offered": offered, "picked": picked,
                })
            elif state_type == "rest_site" and selected:
                choice = str(selected.get("option_id") or selected.get("title") or "")
                self.macro_decisions.append({
                    **common,
                    "decision_type": "campfire",
                    "choice": "Smith" if "SMITH" in choice.upper() else "Heal",
                    "hp_str": f"{common['hp']}/{common['max_hp']}",
                    "offered": [str(c.get("option_id") or c.get("title")) for c in candidates],
                })
            else:
                self.macro_decisions.append({
                    **common,
                    "decision_type": state_type,
                    "action_name": action_name,
                    "selected_index": selected_index,
                    "candidates": candidates,
                })
        return len(self.macro_decisions) - before

    def compute_empirical_tier_stats(self) -> Dict[str, Any]:
        char_asc_stats = defaultdict(lambda: {"total": 0, "wins": 0})
        char_stats = defaultdict(lambda: {"total": 0, "wins": 0})
        deck_char_stats = defaultdict(lambda: {"total": 0, "wins": 0})

        for r in self.runs:
            char = r.get("character", "UNKNOWN")
            if char == "UNKNOWN":
                continue
            asc = r.get("ascension", 0)
            win = 1 if r.get("win", False) else 0
            bracket = "A0" if asc == 0 else ("A1_A9" if asc < 10 else "A10_PLUS")
            char_asc_stats[(char, bracket)]["total"] += 1
            char_asc_stats[(char, bracket)]["wins"] += win
            char_stats[char]["total"] += 1
            char_stats[char]["wins"] += win
            if r.get("deck"):
                deck_char_stats[char]["total"] += 1
                deck_char_stats[char]["wins"] += win

        baselines = {}
        for char, s in char_stats.items():
            if s["total"] > 0:
                baselines[char] = {
                    "overall_win_rate": round(s["wins"] / s["total"], 4),
                    "total_runs": s["total"],
                    "total_wins": s["wins"],
                    "deck_observed_win_rate": round(
                        deck_char_stats[char]["wins"] / max(1, deck_char_stats[char]["total"]), 4
                    ),
                    "deck_observed_runs": deck_char_stats[char]["total"],
                    "brackets": {}
                }
                for bracket in ["A0", "A1_A9", "A10_PLUS"]:
                    b_stat = char_asc_stats[(char, bracket)]
                    b_wr = round(b_stat["wins"] / b_stat["total"], 4) if b_stat["total"] > 0 else baselines[char]["overall_win_rate"]
                    baselines[char]["brackets"][bracket] = {
                        "win_rate": b_wr,
                        "runs": b_stat["total"],
                        "wins": b_stat["wins"]
                    }

        card_stats = defaultdict(lambda: {"runs_with_card": 0, "wins_with_card": 0, "offered_count": 0, "picked_count": 0, "characters": defaultdict(int)})

        for r in self.runs:
            deck = r.get("deck", [])
            win = 1 if r.get("win", False) else 0
            char = r.get("character", "IRONCLAD")
            unique_in_deck = set(deck)
            for card in unique_in_deck:
                if not card:
                    continue
                card_stats[card]["runs_with_card"] += 1
                card_stats[card]["wins_with_card"] += win
                card_stats[card]["characters"][char] += 1

        for d in self.macro_decisions:
            if d.get("decision_type") == "card_reward":
                offered = d.get("offered", [])
                picked = d.get("picked", "SKIP")
                char = d.get("character", "IRONCLAD")
                for c in offered:
                    card_stats[c]["offered_count"] += 1
                    card_stats[c]["characters"][char] += 1
                if picked and picked != "SKIP":
                    card_stats[picked]["picked_count"] += 1

        tier_tables = defaultdict(dict)
        global_card_summary = {}

        for card, st in card_stats.items():
            if not card or card in ("PAD", "UNK"):
                continue
            primary_char = max(st["characters"].items(), key=lambda x: x[1])[0] if st["characters"] else "IRONCLAD"
            # Card outcomes exist only for detailed rows with a deck payload.
            # Comparing those mostly-complete runs against summary-only manifest
            # rows creates spurious +60% deltas.
            base_wr = baselines.get(primary_char, {}).get(
                "deck_observed_win_rate",
                baselines.get(primary_char, {}).get("overall_win_rate", 0.40),
            )
            runs_count = st["runs_with_card"]
            if runs_count > 0:
                card_wr = st["wins_with_card"] / runs_count
                delta_wr = card_wr - base_wr
            else:
                card_wr = base_wr
                delta_wr = 0.0

            offered_cnt = st["offered_count"]
            picked_cnt = st["picked_count"]
            pick_rate = round(picked_cnt / max(1, offered_cnt), 3) if offered_cnt > 0 else 0.50

            if delta_wr >= 0.08 and runs_count >= 10:
                tier = "S+"
            elif delta_wr >= 0.04:
                tier = "S"
            elif delta_wr >= 0.01:
                tier = "A"
            elif delta_wr >= -0.015:
                tier = "B"
            elif delta_wr >= -0.05:
                tier = "C"
            else:
                tier = "D"

            card_info = {
                "card_id": card,
                "character": primary_char,
                "tier": tier,
                "win_rate": round(card_wr, 4),
                "delta_win_rate": round(delta_wr, 4),
                "delta_win_rate_percent": f"{delta_wr * 100:+.2f}%",
                "sample_runs": runs_count,
                "sample_wins": st["wins_with_card"],
                "pick_rate": pick_rate,
                "offered_count": offered_cnt,
                "picked_count": picked_cnt
            }
            tier_tables[primary_char][card] = card_info
            global_card_summary[card] = card_info

        pair_stats = defaultdict(lambda: {"total": 0, "wins": 0})
        for r in self.runs:
            deck = list(set(r.get("deck", [])))
            win = 1 if r.get("win", False) else 0
            if len(deck) >= 2:
                for i in range(len(deck)):
                    for j in range(i + 1, len(deck)):
                        c1, c2 = sorted([deck[i], deck[j]])
                        if c1 and c2:
                            pair_stats[(c1, c2)]["total"] += 1
                            pair_stats[(c1, c2)]["wins"] += win

        synergies = []
        for (c1, c2), pst in pair_stats.items():
            if pst["total"] >= 10:
                pair_wr = pst["wins"] / pst["total"]
                c1_wr = global_card_summary.get(c1, {}).get("win_rate", 0.40)
                c2_wr = global_card_summary.get(c2, {}).get("win_rate", 0.40)
                indep_max = max(c1_wr, c2_wr)
                synergy_lift = pair_wr - indep_max
                if synergy_lift >= 0.03:
                    synergies.append({
                        "card_1": c1,
                        "card_2": c2,
                        "co_occurrence_count": pst["total"],
                        "pair_win_rate": round(pair_wr, 4),
                        "synergy_lift": round(synergy_lift, 4),
                        "synergy_lift_percent": f"{synergy_lift * 100:+.2f}%"
                    })

        synergies.sort(key=lambda x: x["synergy_lift"], reverse=True)

        result = {
            "metadata": {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "total_runs_ingested": len(self.runs),
                "total_macro_decisions": len(self.macro_decisions),
                "unique_cards_evaluated": len(global_card_summary),
                "high_synergy_pairs_identified": len(synergies)
            },
            "character_baselines": baselines,
            "character_tier_rankings": {k: dict(sorted(v.items(), key=lambda x: x[1]["delta_win_rate"], reverse=True)) for k, v in tier_tables.items()},
            "top_synergies": synergies[:100]
        }

        out_p1 = self.game_db_dir / "community_tier_stats.json"
        out_p2 = self.artifacts_dir / "community_tier_stats.json"
        with open(out_p1, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        with open(out_p2, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)

        decisions_p = self.game_db_dir / "compiled_macro_decisions.json"
        with open(decisions_p, "w", encoding="utf-8") as f:
            json.dump(self.macro_decisions, f, indent=2)

        return result


if __name__ == "__main__":
    print("=" * 80)
    print("STS2 COMMUNITY RUN INGESTION AND EMPIRICAL TIER STATS ENGINE")
    print("=" * 80)
    engine = CommunityRunIngestionEngine()
    print("[1/3] Ingesting all community run sources...")
    counts = engine.ingest_all_sources()
    for src, cnt in counts.items():
        print(f"  - {src}: {cnt} items ingested")
    print(f"  -> Total Runs in Telemetry Buffer: {len(engine.runs)}")
    print(f"  -> Total Macro Decisions Extracted: {len(engine.macro_decisions)}")

    print("\n[2/3] Computing empirical Community Tier Stats, dWR, and Synergy Matrices...")
    stats = engine.compute_empirical_tier_stats()

    print(f"\n[3/3] Tier Stats Computed Successfully:")
    print(f"  - Total Runs: {stats['metadata']['total_runs_ingested']}")
    print(f"  - Unique Cards Evaluated: {stats['metadata']['unique_cards_evaluated']}")
    print(f"  - Top Synergy Pairs: {stats['metadata']['high_synergy_pairs_identified']}")
    print("\nSample Character Baselines:")
    for char, b in stats['character_baselines'].items():
        print(f"  - {char}: Overall WR = {b['overall_win_rate']*100:.1f}% ({b['total_wins']}/{b['total_runs']})")

    print("\nTop 3 Tier S+ Cards per Character:")
    for char, cards in stats['character_tier_rankings'].items():
        top_cards = list(cards.values())[:3]
        card_strs = [f"{c['card_id']} ({c['tier']}, {c['delta_win_rate_percent']})" for c in top_cards]
        print(f"  - {char}: {', '.join(card_strs)}")
    print("=" * 80)
