# Project status and review guide

Last evidence review: **2026-08-23**
Shipped build under test: **`0.1.0+59260271157f76a2896f0eab5bc6ea1245d8b314`**

This is the required starting point for architectural reviews. Its purpose is to keep useful outside criticism flowing without allowing proposals, old benchmark numbers, or one successful gate to be mistaken for current proof. Update this document whenever a milestone changes materially.

Claims below may refer to private development evidence and are not automatically
reproducible from the public clone. Treat the public README and rerunnable smoke
commands as the boundary for current contributor-facing capability.

## Read the project on four independent axes

| Axis | Current position | Promotion boundary |
| :--- | :--- | :--- |
| **Mechanical fidelity** | NativeSim executes shipped mechanics. Historical full-application evidence reached a **PROVEN (GO)** milestone on its tested build, with independent headless shipped processes maintaining bit-for-bit determinism across the reported traces. That historical result is not blanket certification of every public build or boundary. | Exact differential replay on a declared, build-pinned coverage matrix. Global certification is false. |
| **Execution breadth** | Full-app control covers combat, routes, events, rewards, shops, and rest sites, but the August 23 policy audit found three full-run bridge defects: card rewards could be silently discarded, rest selection crashed the process, and potion use could be a no-op or target the wrong side. Those paths are repaired and a deterministic Regent replay completed with real drafts, shop actions, and a state-changing potion use, with no socket error or no-progress transition. Multi-worker stress remains unproven after these repairs. | Every advertised boundary remains deterministic, fail-loud, and stress-tested. Breadth is not certification. |
| **Policy quality** | **No run policy is promoted.** The v10 combat policy's 78.14% validation Top-1 is behavior-cloning accuracy against a weak handwritten collector, not win-rate evidence. The root trajectory corpus has 40,192 records with no positive terminal labels; 3,073 unchanged-state actions were identified. The prior densified “winning” corpus used derived rather than replayable seeds and stamped transitions positive without observed terminal victories, so it is quarantined. The v11 enemy-aware checkpoint had an empty enemy vocabulary and is preserved under `artifacts/failed_models`, never promoted. A corrected deterministic Regent replay died on floor 9 after five wins. | Fresh exact-seed, outcome-labeled data; untouched run-level gates; then statistically significant lift over fixed baselines. Top-1 imitation accuracy alone never promotes a policy. |
| **Advisor product** | Private development tooling (`evaluator.py`, `recommend_pick.py`, and `get_live_context.py`) is not part of the public package. No public learned advisor is promoted; private datasets/checkpoints require separate provenance and privacy review. | Exact legal-action/state alignment, fail-closed behavior, calibrated advice, version checks, and acceptable end-to-end latency. |

Phase 1 has a historical `full_application_native` headless-bridge result on
the pinned development build, but remains bounded by the exact differential
coverage matrix rather than global certification. Phase 2 has working
data/model/search experiments ready for token-based v9 training using true
native trajectories with exact label provenance. Phase 3 has run-environment
and UI prototypes, not a trustworthy holistic advisor.

The active run-policy recovery sequence and quantitative gates are in `docs/a1-policy-recovery-plan.md`.

## Evidence precedence

When documents disagree, use this order:

1. Build-pinned machine-readable artifacts and exact replay outputs.
2. Tests or benchmarks that can be rerun on the current tree.
3. This dated status record and the focused implementation reports.
4. The roadmap, which describes intended work.
5. External proposals, estimates, and historical discussion.

Every material numeric claim in a review should name its artifact or command and date. Keep these categories separate: tasks, episodes, roots, labels, sibling continuations, ranking pairs, checkpoints, and complete traces. “Native-derived” means the label came from shipped mechanics; it does not mean the same transition has been differentially certified against the visible shipped game.

### Maintenance contract

Update this file in the same change whenever any of these occurs:

- the pinned shipped build changes;
- differential trace/checkpoint/coverage totals change materially;
- a model is promoted, demoted, or changes architecture/schema;
- a phase gate passes or fails;
- a benchmark changes an architectural priority;
- an external proposal is accepted into or rejected from the active plan.

Do not overwrite failed experiments. Preserve their artifact, seed namespace, measured result, and promotion decision so a later reviewer can distinguish a disproven route from an untried one. If a number is expensive to recompute, record the command, date, hardware/backend, and artifact hash rather than presenting it as timeless.

## Current evidence that is easy to misread

- The historical five-character “floor 13-17” policy summary is not valid run-quality evidence. Its benchmark counted reward actions as combat wins, allowed thousands of steps without a state-progress invariant, and traversed bridge paths that discarded card rewards or crashed at rests. Corrected post-repair evidence must replace it; do not compare new floor numbers against that summary as if protocols were equivalent.
- On 2026-08-23, deterministic Regent seed `QU62X22APK` completed 189 actions in 54.65 seconds with five real combat wins, a state-changing `COLORLESS_POTION` use, no bridge exception, and no unchanged-state transition; it died on floor 9. Median card actions were about 30-36 ms in the trace tail, while end-turn transitions were about 660-712 ms. Full-app runs are acceptance tests, not the high-throughput training backend.
- The existing v10/v11 trainers now read top-level energy and block, permit 32 candidate actions, and exclude transitions whose post-action hash equals the pre-action hash. V11 vocabulary preflight now sees 489 action/card tokens and 25 enemy identities. These schema fixes do not make the available labels adequate for training or promotion.
- The legacy macro ingestion and checkpoints are demoted: 67,013 timeline-derived “card choices” had a 98.01% first-option rate because acquired reward text was split on whitespace; campfire labels used post-action HP. The canonical ingester now refuses to turn timeline summaries into choice labels. It parses ten structured agentic archives into 7,777 exact state/action examples and merges only explicitly offered/picked sources, producing 10,006 compiled decisions with a 38.5% card first-option rate. Active routing/rest/draft/shop/upgrade/potion priors read these corrected artifacts; fixed HP gameplay gates are removed.

- `artifacts/differential-campaign-report.json` passes **49/49** exact traces and **904** checkpoints, including 41 terminal victories. Coverage spans Defect, Ironclad, Necrobinder, Regent, and Silent across 14 encounters and all three combat tiers, including `BYRDONIS_ELITE` and `VANTOM_BOSS`. It enumerates 656 card plays, 181 complete turns, 18 potion uses, 9 inferred native `choose_cards` actions, 58 observed card models, 50 played-card models, 22 enemy models, 48 enemy moves, 28 power models, 9 relic models, 12 potion models, four Defect orb models, nonzero Regent stars, and exact terminal outcomes. The artifact explicitly sets `global_certification` to false. Report SHA-256: `D63580DBA70B1D009A3C4485AC3A952A53E64A9B832FD89C18E5F5EAF9BDD03E`.
- `artifacts/differential-coverage-inventory.json` is the exact build-keyed scheduling inventory. It stores the complete certified trace SHA-256 set and exact semantic-checkpoint SHA-256 set; it is not a Bloom filter and is not itself certification.
- Automated isolated AutoTrace generation exists. Candidate traces become certifying evidence only after strict exact replay; failed or unreviewed candidates and their replay diagnostics remain separate. Bounded exact choice-path inference now resolves exporter-omitted card/generated-card choices only when one native path matches the complete checkpoint. Defect orb state/value/capacity and `CombatTargets` evolution are exact. Remaining quarantines are legacy pre-hook/pre-enemy-block traces; no unsupported boundary is promoted.
- The v8 breadth corpus trains over 61 non-reserved encounter IDs and reserves 9 encounter IDs for holdout. It contains 2,562 training episodes, 35,244 state labels, 4,779 sibling continuations, and 444 ranking pairs. Its records contain fixed **344-float** features, not raw card/creature/action tokens. It is a frozen baseline and task/evidence manifest; it cannot train the intended token model without regeneration.
- The v8 fixed MLP reached 72.32% validation; a validation-only loss sweep reached 79.66%. No untouched promotion labels were generated. The earlier `native-value-matrix.pt` reached 94.25% validation and 90.91% promotion ranking, then failed the mandatory fresh native search-lift gate: 0.2548 mean return versus greedy's 0.2943. It remains demoted.
- Active shipped combat state has no complete serializer in the inspected build. Restore is deterministic reconstruction plus ordered native action replay. Proposed keyframes are an investigation item, not an available exact snapshot mechanism.
- Resident native actions are millisecond-scale, but reconstruction-heavy search is not. The measured depth-64 full search cycle is about 304 ms, protocol-only calls are much smaller, and each worker is roughly 2.1 GB. Report resident step, full-turn, reset, restore-by-depth, complete search cycle, protocol overhead, and sustained memory separately. The 8192-entry LRU branch cache uses content-addressed, branch_identity-deduplicated handles (reset_request + action history + expected_hash); stale "256-entry cap" and "state-hash transposition" references in older notes are superseded.
- The existing Python/Tk overlay is a prototype. It is not yet click-through or game-window-pinned, and its consumer fields have drifted from `get_live_context.py`. OCR fallback can infer or guess combat facts. Exact claims require a build-compatible read-only native bridge; external-only OCR must surface uncertainty and fail closed.
- The installed PyTorch environment is CPU-only. The available Radeon 7900 XT must not be treated as CUDA hardware; GPU work needs a measured compatible backend. Hardware and dependency claims belong in preflight evidence, not assumptions.

## Decisions on the current external proposal set

| Proposal | Decision | Correction and placement |
| :--- | :--- | :--- |
| Serialize native combat keyframes every turn | **Investigate, not accepted as stated** | No complete active-combat serializer exists. First define a canonical reconstructible room keyframe and prove exact replay; only then evaluate turn keyframes. Replay from reset remains the correctness fallback. |
| Batch neural scoring | **Accepted** | Add `score_many`/tensor batching after the raw-token schema. Profile scorer and restore costs independently. Do not promise 1,000+ native transitions/s without evidence. |
| Three draw-pile determinizations with zero root variance | **Rejected as a gate** | Use at least 5 determinizations, mask hidden order, resample only when an action crosses a hidden-information boundary, and require root-choice variance below 5%, not zero. |
| Compact 1,320-byte observation | **Direction accepted, number rejected** | Current fixed features are 344 float32 values (1,376 bytes before framing), while the intended model needs variable tokens. Define and version the token schema first. |
| Train the Transformer directly on frozen v8 | **Rejected** | v8 lacks raw tokens. Preserve it as a baseline/task manifest and generate a v9 native corpus containing versioned observation tokens, candidate-action tokens, provenance, and labels. |
| Skip-relative card valuation | **Accepted for macro drafting** | Set Skip's advantage to zero and learn `deck + card` versus unchanged-deck outcomes from native long-horizon counterfactuals. Do not retrofit handcrafted scores as ground truth. |
| Fixed potion reservation multipliers | **Principle accepted, formula rejected** | Learn or search per-action counterfactual survival benefit and future opportunity cost. Gate necessary-to-survive potion use and avoidable rare-potion waste, not indiscriminate boss deployment. |
| Archetype-balanced replay and per-slice gates | **Accepted** | Stratify v9 by character/archetype, act/tier, deck thickness, HP pressure, policy, and outcome. Keep aggregate validation, untouched promotion, calibration, and search-lift gates alongside a per-archetype floor. |
| Live novelty capture with a Bloom filter | **Accepted with redesign** | Use a build-keyed exact coverage database and semantic signatures; false positives are unacceptable. Prefer isolated AutoTrace scheduling. Live tracing remains explicit opt-in and candidate traces require exact replay. |
| Sub-100 ms preflight doctor | **Accepted as quick/deep modes** | Quick mode validates paths, versions, cached hashes, and lightweight imports. Deep mode performs real imports, full hashes, worker hello/reset, backend checks, and smoke replay. A cold 1.9 GB PCK hash cannot honestly meet 100 ms. |
| Fixed-struct named-pipe IPC now | **Deferred** | Variable token observations make the proposed struct premature, and restore/search dominates today. Retain JSON as the debuggable reference; optimize framing or shared memory only after profiling identifies IPC as material. |
| HUD now | **Deferred behind trust gates** | First harden the exact live-state contract and advisor. Then implement click-through/window pinning and calibrated display. Never present OCR-derived guesses as exact native advice. |

## Active milestone order

1. **Broaden exact certification automatically.** The matrix now has all characters and combat tiers but only 14 of the target 40+ encounter IDs. The exact next implementation step is a build-keyed seed/route scheduler that reads the coverage inventory and selects deterministic character/route manifests for missing encounter and mechanic sets; semantic-subset suppression already prevents repeated prefixes from inflating certification.
2. **Specify and generate v9.** Freeze versioned raw tokens for card instances/zones, creatures/powers/intents, relic state, potions, character resources, hidden-information masks, and candidate actions. Generate stratified native labels with disjoint train, validation, promotion, and search-evaluation seed namespaces.
3. **Train the action-conditioned critic.** Use permutation-aware token encoding, stratified batches, calibrated multi-task heads, and batched scoring. V8 stays as an immutable regression baseline.
4. **Pass every policy gate.** Require at least 90% untouched native pairwise ranking, worst-slice floors, calibration, and statistically significant fresh-seed search lift over greedy and heuristic baselines. Ranking alone never promotes a scorer.
5. **Improve search honestly.** Batch scorer calls, profile restore scheduling, then add information-set draw determinization. Explore reconstructible keyframes only under exact differential tests.
6. **Train macro decisions.** Add skip-relative card rewards, shops, routing, upgrades/removals, events, relics, and counterfactual potion reservation using native long-horizon outcomes.
7. **Ship the advisor last.** Stabilize exact read-only context extraction and legal-action alignment, fail closed on unsupported/build-mismatched state, then finish the HUD. Optimize IPC only if end-to-end profiles justify it.

## Non-negotiable claims discipline

- Execute shipped native mechanics; do not reimplement individual cards inside NativeSim.
- Suppress presentation only, and inventory every suppression seam.
- Fail loudly on unsupported state.
- Never manipulate the visible game or desktop during headless research.
- Keep shadow-simulator output provenance explicit and non-authoritative.
- Keep the environment non-certifying outside exact real-game differential checkpoints.
- Keep model promotion independent from simulator certification.
