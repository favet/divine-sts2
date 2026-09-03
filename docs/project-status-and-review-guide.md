# Project status and review guide

Last evidence review: **2026-08-29**
Shipped build under test: **`0.1.0+59260271157f76a2896f0eab5bc6ea1245d8b314`**

This is the required starting point for architectural reviews. Its purpose is to keep useful outside criticism flowing without allowing proposals, old benchmark numbers, or one successful gate to be mistaken for current proof. Update this document whenever a milestone changes materially.

Claims below may refer to private development evidence and are not automatically
reproducible from the public clone. Treat the public README and rerunnable smoke
commands as the boundary for current contributor-facing capability.

## Read the project on four independent axes

| Axis | Current position | Promotion boundary |
| :--- | :--- | :--- |
| **Mechanical fidelity** | NativeSim executes shipped mechanics directly via 20 headless .NET 9 workers (~1,514+ dec/sec, 100% zero-crash stability). 49/49 exact traces and 904 checkpoints verified. | Exact differential replay on a declared, build-pinned coverage matrix. Global certification is false. |
| **Execution breadth** | Full-app control covers combat, routes, events, rewards, shops, and rest sites across all 5 characters. Multi-worker persistent pools operate with zero unmanaged aborts. | Every advertised boundary remains deterministic, fail-loud, and stress-tested. Breadth is not certification. |
| **Policy quality** | **Combat V2 diagnosed as Category C (Offline-Only Success).** Offline Top-1 distillation reached 75.19% (+25.51% over V1) with 65.08% disagreement correction, but standalone native execution was 72.66% (vs Frozen V1: 75.78%, Expert Heuristic: 79.69%). UCT B16 teacher achieves 90.62%. Phase 3D is actively evaluating V2 as policy prior in PUCT lookahead. | Statistically significant native combat lift over prior generation; 0% regression on 500-state failure suite. Top-1 imitation accuracy alone never promotes a policy. |
| **Advisor product** | Private development tooling (`evaluator.py`, `recommend_pick.py`, and `get_live_context.py`) functions under single-shot live capture. No learned macro advisor is publicly promoted. | Exact legal-action/state alignment, fail-closed behavior, calibrated advice, version checks, and acceptable end-to-end latency. |

## Evidence precedence

When documents disagree, use this order:

1. Build-pinned machine-readable artifacts and exact replay outputs.
2. Tests or benchmarks that can be rerun on the current tree.
3. This dated status record and the focused implementation reports (`docs/research/sts1-prior-art-and-transfer-strategy.md`, `artifacts/combat-v2-training/phase-3c-final-report.md`).
4. The roadmap, which describes intended work.
5. External proposals, estimates, and historical discussion.

---

## Decisions on the current external proposal set & STS1 Prior-Art Transfers

| Proposal | Decision | Correction and placement |
| :--- | :--- | :--- |
| Direct STS1 weight transfer | **Rejected** | Card IDs, keywords, mechanics, character kits, and tensor vocabularies differ completely; direct weight transfer has zero mathematical compatibility. |
| STS1 representation pretraining | **Deferred / Low ROI** | Native .NET 9 simulator produces 1,514+ transitions/s on actual STS2 mechanics; generating 500k native states takes ~5.5 min, making cross-game pretraining unnecessary. |
| Typed-token transformer (Combat V3) | **Accepted for V3** | Unify all cards, relics, powers, potions, and creatures into typed tokens with self-attention, replacing ad-hoc segment projections (`hand_proj`, `enemy_proj`). |
| Categorical HP value distribution (`value_cat`) | **Accepted** | Replace scalar $V_{\text{hp\_loss}}$ with 32-bin categorical HP distribution and explicit $P(\text{death})$ to eliminate suicidal gambles. |
| Auxiliary mechanics prediction heads | **Accepted for V3** | Train auxiliary heads on trunk for incoming unblocked damage, lethal availability, and block sufficiency to regularize internal game arithmetic. |
| Deterministic intra-turn beam search | **Accepted** | Use exact/beam enumeration for known-hand intra-turn sequences; reserve stochastic POMDP MCTS exclusively for `END_TURN` boundaries. |
| Permanent tactical failure corpus | **Accepted** | Assemble 500-root failure suite from V2 losses, reverse regressions, and human traces to serve as an automated promotion blocker. |
| Iterative DAgger / Expert Iteration loop | **Accepted** | Replace one-shot static training corpora with iterative rollout $\to$ search relabeling $\to$ policy update loops to eliminate compounding drift. |
| Serialize native combat keyframes every turn | **Investigate, not accepted as stated** | No complete active-combat serializer exists. Replay from reset remains the correctness fallback. |
| Batch neural scoring | **Accepted** | Add batched tensor scoring after token schema standardization. |
| Skip-relative card valuation | **Accepted for macro drafting** | Set Skip's advantage to zero and learn `deck + card` versus unchanged-deck outcomes from native long-horizon counterfactuals. |
| Fixed potion reservation multipliers | **Principle accepted, formula rejected** | Learn or search per-action counterfactual survival benefit and future opportunity cost. Gate necessary-to-survive potion use and avoidable rare-potion waste. |

---

## Active milestone order

1. **Phase 3D Network-Guided PUCT Evaluation:** Validate Combat V2 policy priors inside shallow MCTS ($B=8, 16$) to eliminate rollout compounding error and evaluate combat lift.
2. **Tactical Failure Regression Corpus:** Lock $\ge 500$ decision-rich blunder and reverse-drift roots to create an automated promotion ratchet.
3. **Categorical HP & Death-Risk Loss (`value_cat`):** Transition value head from scalar MSE to 32-bin categorical distribution and explicit $P(\text{death})$.
4. **Iterative DAgger Loop Implementation:** Build continuous rollout $\to$ native search relabeling $\to$ model update pipeline.
5. **Combat V3 Typed-Token Transformer:** Implement universal entity tokenization, auxiliary mechanics heads, and counterfactual pairwise ranking.
6. **Train macro decisions:** Add skip-relative card rewards, shops, routing, upgrades/removals, events, relics, and counterfactual potion reservation using native long-horizon outcomes.
7. **Ship the advisor last:** Stabilize exact read-only context extraction and legal-action alignment, fail closed on unsupported/build-mismatched state, then finish the HUD.

## Non-negotiable claims discipline

- Execute shipped native mechanics; do not reimplement individual cards inside NativeSim.
- Suppress presentation only, and inventory every suppression seam.
- Fail loudly on unsupported state.
- Never manipulate the visible game or desktop during headless research.
- Keep shadow-simulator output provenance explicit and non-authoritative.
- Keep the environment non-certifying outside exact real-game differential checkpoints.
- Keep model promotion independent from simulator certification.
