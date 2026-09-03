# Holistic solver roadmap

The target is a mechanically faithful, strategically excellent STS2 solver and live advisor. Mechanical fidelity and policy strength are separate promotion axes: exact mechanics do not imply strong play, and a high-scoring policy trained on drifted mechanics is not acceptable.

The separately checked-out Python simulator is a local research accelerator only. It has no detectable license, is not copied into NativeSim, and cannot become a distributed dependency without compatible permission. NativeSim remains the shipped-mechanics authority.

See `docs/architectural-guardrails.md` for the five permanent mathematical and algorithmic invariants (Information-Set POMDP Search, Multi-Task Decoupled Reward Heads, Skip-Relative Draw Dilution, Archetype Diversity Regularization, and Potion Scarcity Budgeting).

See `docs/project-status-and-review-guide.md` for the dated evidence snapshot, accepted/rejected proposal decisions, and active milestone order. That record controls when an older roadmap statement or outside performance estimate conflicts with current machine evidence.

## Current program position — 2026-08-29

- **Phase 1: authoritative simulation & 20-worker farm PROVEN (GO).** Shipped mechanics run directly in isolated, presentation-suppressed .NET 9 workers with sustained 1,514+ decisions/second throughput, 100% zero-crash stability, and strict bit-for-bit mechanics fidelity.
- **Phase 2 / 3A-3C: empirical search distillation ceiling diagnosed.** UCT MCTS (B=16) achieves ~90.6% win rate. Supervised search distillation into Combat Policy V2 jumped Top-1 teacher agreement from 49.7% to 75.2% and corrected 65.1% of search disagreements, but standalone native execution regressed to 72.7% (vs Frozen V1 at 75.8% and Expert Heuristic at 79.7%). Classification: **Category C (Offline-Only Success)**.
- **Strategic Direction: STS1 Prior-Art Architecture Integration (`docs/research/sts1-prior-art-and-transfer-strategy.md`).** The project adopts seven foundational research principles derived from mature STS1 systems (Spire Pilot, AlphaStS, sts_lightspeed, bottled_ai, sts-rl-agent). Search is recognized as a permanent first-class runtime component (PUCT / hybrid beam), categorical HP/death-risk heads replace scalar expectations, and training transitions to iterative DAgger loops.

The immediate critical path is: Phase 3D network-guided PUCT evaluation -> Tactical failure regression corpus -> Iterative DAgger / relabeling loop -> Combat V3 typed-token transformer with categorical value distributions (`value_cat`) -> macro counterfactuals -> hardened live advisor.

---

## Phase 1 — Trustworthy combat data engine

Build a deterministic, high-throughput iterator around native execution with verified provenance.

Deliverables:

- Explicit scenario injection for character, deck instances, upgrades, enchantments, relics, potions, HP, gold, ascension, encounter, seed, and optional initial checkpoint hand.
- Stable instance-preserving structured observations rather than lossy scalar card IDs and pile counts.
- Semantic legal actions keyed by card, creature, potion, and choice identity.
- Side-effect-free observation and fail-loud handling of invalid actions or unsupported mutable state.
- Provenance on every transition: `shipped_native`, `python_parity_verified`, or `python_unverified`.
- Automated differential fixtures and a mechanics coverage matrix generated without manual gameplay scraping.
- Deterministic corpus shards and end-to-end benchmarks including encoding and serialization, not raw simulation alone.

Promotion gates:

- Zero identity loss or duplicate collapse in supported scenarios.
- Repeated observation produces identical hashes and no mutation.
- Independent workers reproduce transition and terminal hashes.
- Every advertised action executes or fails the test suite; runtime failures are never converted into game losses.
- Native differential parity passes for the declared supported mechanics; everything else remains fail-closed or explicitly unverified.
- Structured iterator throughput is measured and sufficient for sustained corpus generation.

---

## Phase 2 — Tactical expert & Search-Guided Policy

Train an action-conditioned structured policy/value model using iterative search-improved targets and deploy it within information-consistent PUCT search.

Deliverables:

- Typed-token Transformer encoder unifying card instances, creatures, power/status effects, relic state, potions, and candidate actions.
- Categorical terminal HP distribution head (`value_cat` over 32 bins), calibrated $P(\text{death})$, and auxiliary mechanics heads (incoming unblocked damage, lethal detection, block requirements).
- Decoupled multi-task value heads ($V_{\text{win}}$, $V_{\text{hp\_loss}}$, $V_{\text{relic\_ev}}$, $V_{\text{boss\_readiness}}$).
- Hybrid search coordinator: deterministic within-turn beam search + inter-turn stochastic POMDP MCTS ($K \ge 5$ draw determinizations).
- Iterative DAgger training loop: active policy rollouts $\to$ state harvesting $\to$ native search relabeling $\to$ policy update.
- Permanent tactical failure corpus ($\ge 500$ blunder roots) serving as an automated promotion ratchet.

Promotion gates:

- At least 90% pairwise ranking accuracy on untouched native-held-out encounter/seed groups.
- Statistically significant native search lift over greedy, heuristic, and prior-generation baselines, with confidence intervals and no seed overlap.
- Zero suicidal lines chosen in high-variance validation scenarios; calibrated CVaR tail risk.
- Less than 2% reverse regression on the locked tactical failure corpus.
- Sub-50 ms mean decision latency under network-guided PUCT search ($B \le 8$).

## Phase 3 — Holistic run solver and live advisor

Build a hierarchical run policy that uses the tactical expert to evaluate downstream combat consequences.

Deliverables:

- Structured macro observations for complete deck composition, relic/potion state, map graph, encounter distributions, boss/elite threats, shops, events, rewards, upgrades, removals, and resources.
- Separate phase heads or action-conditioned scoring for routing, card rewards/skip, shops, rest sites, events, potions, and boss relics.
- Counterfactual evaluation of `deck + option` versus skip or alternate purchases over future encounter distributions.
- Uncertainty-aware recommendations, calibrated percentages, explanations grounded in evaluated outcomes, and low-latency inference for the HUD.
- Native full-run differential replay and long-horizon policy evaluation.

Promotion gates:

- Positive native-held-out run lift across characters, acts, ascensions, and seed families.
- Exact replay agreement for every mechanics-certified evaluation trace.
- Stable live-context extraction and legal-action alignment with no visible-game manipulation.
- Latency, calibration, failure reporting, and version/build compatibility meet the product contract.

## Immediate Phase 1 slice

The initial isolated implementation adds `sts2_env.research.CombatIterator`. It accepts explicit card instance IDs and a canonical `actN:setup_*` encounter key, emits structured state plus semantic actions, hashes observations deterministically, and labels all output `python_unverified`. Its first acceptance fixture preserves three duplicate Strike identities and reproduces the already native-audited Bygone Effigy 6/6/7 damage sequence.

The current isolated tracked patch is pinned by SHA-256 `07d0144a2aa9e2017491616fe731f3fae3274d6f6656bb2d1740337e22756a5e`. The complete external suite passes 4,629 tests with one skip. Its RNG wrapper reproduces the shipped `MegaRandom` Xoshiro256** sequence, and the iterator creates a real named-stream run context from either numeric or native string seeds. The end-to-end structured benchmark, including run-context construction, observation construction, and hashing, measures 1,847 decisions/second in one process (250 combats, 4,454 decisions), compared with roughly 3,224 raw decisions/second before structured encoding.

Deterministic corpus sharding is now operational. One-worker and four-worker generalized native-stream acceptance runs produce byte-identical output: 100 combats, 1,580 transitions, and corpus SHA-256 `ed9eb480c1567a456bb117063f2addcc9c3a44a7764f4112501e96161691d7df`. Completed shards remain checksum-validated, restartable, and atomic.

The exact native differential matrix contains those four Bygone Effigy scenarios, the Axebot Defend/full-turn scenario, Bowlbugs Normal initial state, Seapunk Normal initial state, and Aeonglass Boss through its first enemy turn. Bowlbugs proves exact private-RNG composition plus three independent Niche constructions through 48 fields; Seapunk proves the ordered Calcified Cultist/Seapunk setup through 37 fields; Aeonglass matches 33 initial and 30 post-turn fields. The machine-readable evidence is `artifacts/shadow-simulator-matrix-audit.json`; this remains a narrow verified slice rather than a general parity claim.

Character-specific state serialization now explicitly covers Defect orbs, Necrobinder's Osty, and Regent stars. The capability contract advertises blocking card choices and rejects scenarios requiring creature or option choices; those two choice kinds remain unsupported. All 80 shipped catalog encounters now have explicit setup support and separate composition, Niche creation, and MonsterAi decisions where randomness applies. Aeonglass's exact decompiled setup, powers, status, and deterministic move cycle are implemented, but it is not inserted into a random boss pool without authoritative pool-order evidence. Seapunk Normal is likewise available for explicit iterator scenarios while its Act 4 pool position remains unclaimed. A 500-combat iterator stress run retained only 8,000 bytes after warmup across 9,655 transitions, below the 8 MB gate.

The first encounter-composition audit found genuine Python drift. Shipped `BowlbugsWeak.GenerateMonsters` always places Bowlbug Rock first and then selects Egg or Nectar; the Python implementation incorrectly fixed Egg plus Nectar. Shipped `BowlbugsNormal` places Rock first and then selects two distinct workers from Egg, Silk, and Nectar; Python incorrectly fixed Egg, Rock, and Silk. Both isolated definitions and their regressions now follow the decompiled shipped rules. Exact RNG-stream parity for those encounters is still unverified.

The repeatable 32-seed catalog audit covers all 80 shipped encounter models. Seventy-nine have matching observed ordered enemy-model signature sets, and Ruby Raiders has a directly decompiled-equivalent three-distinct-from-five composition rule whose finite observed permutation samples differ. Nothing is missing. The evidence is `artifacts/shadow-encounter-composition-audit.json`; these results do not certify exact RNG, HP, AI, or transitions beyond the explicit differential matrix.

This is the shadow iterator's contract foundation, not Phase 1 completion. NativeSim has since exposed native card selection, creature targeting, bundle/relic choices, run rewards, events, shops, rest sites, treasures, and nested combat; those capabilities do not automatically transfer to the separate Python iterator. Broad stratified differential generation, explicit shadow provenance, sustained multi-policy corpus characterization, and native-held-out promotion evidence remain required. The frozen v8 fixed-feature corpus cannot train the intended token model; the next training corpus must be a versioned v9 raw-token native corpus.
