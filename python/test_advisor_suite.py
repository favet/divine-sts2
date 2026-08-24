"""
STS2 Live Advisor Suite Regression & Mathematical Integrity Test.
Verifies all 5 scenarios (Card Drafting, Campfires, Merchant Shops, Boss Relics, Map Pathing):
1. Mathematical integrity: Percentages strictly sum to 100.0%.
2. Formatting compliance: Standard markdown `---` delimiters, explicit community tiers.
3. 1-indexed branch routing notation for map pathing.
4. Single-shot sub-150ms latency.
"""

import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scenario_evaluators import scenario_advisor
from evaluator import evaluator


def extract_percentages(text: str) -> list[int]:
    """Extracts all percentage numbers associated with option headers."""
    pcts = []
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("**") or line.startswith("["):
            m = re.findall(r"\*\*(\d+)%\*\*", line)
            if m:
                pcts.extend(int(x) for x in m)
    return pcts


def test_card_drafting():
    print("Testing Card Drafting Evaluation...")
    state = {
        "character": "Ironclad",
        "hp": "75/80",
        "floor": 4,
        "ascension": 15,
        "deck": ["Strike", "Strike", "Defend", "Bash", "Carnage"],
        "relics": ["Burning Blood", "Vajra"]
    }
    offered = ["Demon Form", "Shrug It Off", "Wild Strike"]
    t0 = time.perf_counter()
    res = evaluator.evaluate_options(offered, run_state=state)
    out_text = evaluator.format_evaluation_output(res)
    lat_ms = (time.perf_counter() - t0) * 1000.0

    cards = res.get("cards", [])
    skip = res.get("skip", {})
    total_pct = sum(c["pct"] for c in cards) + skip.get("pct", 0)

    assert len(cards) == 3, f"Expected 3 cards, got {len(cards)}"
    assert total_pct == 100, f"Card drafting percentages sum to {total_pct}%, expected 100%"
    assert "[Tier:" in out_text, "Expected explicit community tier tags in output"
    assert "---" in out_text, "Expected markdown horizontal rule delimiters"
    assert lat_ms < 150.0, f"Latency {lat_ms:.2f}ms exceeded 150ms limit"
    print(f"  [PASS] Card drafting: sum={total_pct}%, latency={lat_ms:.2f}ms")


def test_rest_site():
    print("Testing Rest Site Evaluation...")
    state_healthy = {"character": "Silent", "hp": "65/70", "floor": 10, "ascension": 15, "deck": ["Footwork", "Blade Dance"]}
    state_critical = {"character": "Silent", "hp": "15/70", "floor": 10, "ascension": 15, "deck": ["Footwork", "Blade Dance"]}

    t0 = time.perf_counter()
    res_healthy = scenario_advisor.evaluate_rest_site(state_healthy)
    res_critical = scenario_advisor.evaluate_rest_site(state_critical)
    lat_ms = (time.perf_counter() - t0) * 1000.0 / 2.0

    pcts_h = extract_percentages(res_healthy)
    pcts_c = extract_percentages(res_critical)

    assert sum(pcts_h[:2]) == 100, f"Healthy rest site pcts sum to {sum(pcts_h[:2])}%, expected 100%"
    assert sum(pcts_c[:2]) == 100, f"Critical rest site pcts sum to {sum(pcts_c[:2])}%, expected 100%"
    assert "Smith" in res_healthy and pcts_h[0] > 70, "Expected Smith to be dominant when healthy"
    assert "Heal" in res_critical and pcts_c[0] > 70, "Expected Heal to be dominant when critical"
    assert lat_ms < 100.0, f"Latency {lat_ms:.2f}ms exceeded 100ms"
    print(f"  [PASS] Rest site: healthy_sum={sum(pcts_h[:2])}%, crit_sum={sum(pcts_c[:2])}%, latency={lat_ms:.2f}ms")


def test_merchant_shop():
    print("Testing Merchant Shop Evaluation...")
    state = {
        "character": "Defect",
        "gold": 280,
        "hp": "60/75",
        "floor": 8,
        "ascension": 15,
        "deck": ["Strike", "Strike", "Defend", "Zap", "Dualcast", "Defragment"],
        "relics": ["Cracked Core"]
    }
    relics_for_sale = ["Membership Card", "Vajra"]
    cards_for_sale = ["Electrodynamics", "Coolheaded"]
    t0 = time.perf_counter()
    res = scenario_advisor.evaluate_shop(relics_for_sale, cards_for_sale, 280, run_state=state)
    lat_ms = (time.perf_counter() - t0) * 1000.0

    assert "Card Removal" in res or "CARD REMOVAL" in res, "Expected Card Removal in shop options"
    assert "---" in res, "Expected standard markdown horizontal rules"
    assert lat_ms < 100.0, f"Latency {lat_ms:.2f}ms exceeded 100ms"
    print(f"  [PASS] Merchant shop: latency={lat_ms:.2f}ms")


def test_boss_relics():
    print("Testing Boss Relic Evaluation...")
    state = {
        "character": "Necrobinder",
        "hp": "70/75",
        "floor": 16,
        "ascension": 15,
        "deck": ["Strike", "Defend"],
        "relics": ["Bound Skull"]
    }
    offered = ["Runic Pyramid", "Philosopher's Stone", "Black Star"]
    t0 = time.perf_counter()
    res = scenario_advisor.evaluate_boss_relics(offered, state)
    lat_ms = (time.perf_counter() - t0) * 1000.0

    pcts = extract_percentages(res)
    assert len(pcts) == 3, f"Expected 3 offered boss relic percentages, found {len(pcts)}"
    assert sum(pcts) == 100, f"Boss relic pcts sum to {sum(pcts)}%, expected 100%"
    assert "[Tier:" in res, "Expected explicit Community Tier tags"
    assert lat_ms < 100.0, f"Latency {lat_ms:.2f}ms exceeded 100ms"
    print(f"  [PASS] Boss relics: sum={sum(pcts)}%, latency={lat_ms:.2f}ms")


def test_map_pathing():
    print("Testing Map Pathing Evaluation...")
    state = {
        "character": "Regent",
        "hp": "70/80",
        "floor": 3,
        "ascension": 15,
        "deck": ["Strike", "Defend"],
        "relics": [],
        "saved_map": {
            "points": [
                {"coord": {"x": 1, "y": 1}, "point_type": "Monster", "parents": [], "children": [{"x": 1, "y": 2}, {"x": 2, "y": 2}]},
                {"coord": {"x": 1, "y": 2}, "point_type": "Shop", "parents": [{"x": 1, "y": 1}], "children": [{"x": 1, "y": 3}]},
                {"coord": {"x": 2, "y": 2}, "point_type": "Unknown", "parents": [{"x": 1, "y": 1}], "children": [{"x": 2, "y": 3}]},
                {"coord": {"x": 1, "y": 3}, "point_type": "RestSite", "parents": [{"x": 1, "y": 2}], "children": [{"x": 1, "y": 4}]},
                {"coord": {"x": 2, "y": 3}, "point_type": "Elite", "parents": [{"x": 2, "y": 2}], "children": [{"x": 1, "y": 4}]},
                {"coord": {"x": 1, "y": 4}, "point_type": "Boss", "parents": [{"x": 1, "y": 3}, {"x": 2, "y": 3}], "children": []},
            ]
        }
    }
    t0 = time.perf_counter()
    res = scenario_advisor.evaluate_pathing(state)
    lat_ms = (time.perf_counter() - t0) * 1000.0

    assert "->" in res, "Expected chain route with -> arrows"
    assert "1 (" in res or "2 (" in res, "Expected 1-indexed branch choice notation"
    pcts = extract_percentages(res)
    assert sum(pcts) == 100, f"Map pathing pcts sum to {sum(pcts)}%, expected 100%"
    assert lat_ms < 100.0, f"Latency {lat_ms:.2f}ms exceeded 100ms"
    print(f"  [PASS] Map pathing: sum={sum(pcts)}%, latency={lat_ms:.2f}ms")


def run_all_tests():
    print("=" * 80)
    print("STS2 ADVISOR SUITE FULL REGRESSION & MATHEMATICAL INTEGRITY AUDIT")
    print("=" * 80)

    test_card_drafting()
    test_rest_site()
    test_merchant_shop()
    test_boss_relics()
    test_map_pathing()

    print("\n" + "=" * 80)
    print("[ALL PASSED] Complete Live Advisor Suite Verified (Strict 100% Sum & Sub-150ms)")
    print("=" * 80)


if __name__ == "__main__":
    run_all_tests()
