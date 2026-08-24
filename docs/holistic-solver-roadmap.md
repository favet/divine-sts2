# Holistic solver roadmap

The target is a mechanically faithful, strategically excellent STS2 solver and live advisor. Mechanical fidelity and policy strength are separate promotion axes: exact mechanics do not imply strong play, and a high-scoring policy trained on drifted mechanics is not acceptable.

The separately checked-out Python simulator is a local research accelerator only. It has no detectable license, is not copied into NativeSim, and cannot become a distributed dependency without compatible permission. NativeSim remains the shipped-mechanics authority.

See `docs/architectural-guardrails.md` for the five permanent mathematical and algorithmic invariants (Information-Set POMDP Search, Multi-Task Decoupled Reward Heads, Skip-Relative Draw Dilution, Archetype Diversity Regularization, and Potion Scarcity Budgeting).

See `docs/project-status-and-review-guide.md` for the dated evidence snapshot, accepted/rejected proposal decisions, and active milestone order. That record controls when an older roadmap statement or outside performance estimate conflicts with current machine evidence.

## Current program position — 2026-08-23

- **Phase 1: advanced, core architecture milestone PROVEN (GO).** The full-application native control bridge (`SlayTheSpire2.exe --headless` + isolated C# mod + TCP RPC) is verified 100% deterministic across 4 concurrent OS processes with zero synthetic state reconstruction. Broad native execution and automated isolated AutoTrace expand differential coverage across all 5 characters and combat tiers.
- **Phase 2: experiments exist, no promoted critic.** The fixed-feature v8 corpus exposed an architecture ceiling, and the earlier ranking-qualified MLP failed fresh native search lift. A raw-token v9 corpus will be generated directly from the authoritative full-application environment.
- **Phase 3: infrastructure prototypes only.** Native full-Act routes and macro choice boundaries execute, while live extraction, calibrated macro advice, and the HUD have not passed product gates.

The immediate critical path is: full-application native control bridge (GO) -> versioned v9 raw-token corpus -> action-conditioned critic -> untouched ranking and fresh-search lift -> uncertainty-aware search -> macro counterfactuals -> hardened live advisor. Performance work follows measured bottlenecks; it does not replace fidelity or policy gates.

The 2026-08-23 breadth increment corrected the AutoTrace launcher before accepting new evidence: bounded timeout scales with requested combat count; seed/count/policy sandboxes are resumable; all candidates replay before a batch failure; failures retain JSON diagnostics; certified copies are collision-checked and atomic; canonical ten-character seeds are mandatory; and byte/semantic duplicates remain candidates instead of inflating certification. The driver reapplies the seed at the shipped lobby-ready boundary. Bounded exact native choice-path inference covers Survivor, Armaments, Havoc, and generated Skill Potion selections. Generic shipped room/combat/turn-start hooks now reconstruct Gorget and Cracked Core; opt-in orb snapshots certify capacity, model, passive/evoke values, and native RNG effects. Enemy block is restored after the shipped combat-start lifecycle, which made Cubex exact without reimplementing its mechanic.

The current exact campaign inventory is `artifacts/differential-coverage-inventory.json` (SHA-256 `A56FB39D9B22DA295D7CF88EEEE4BEFCB8B0383EEF9E20E0F81EB7F628F4347F`). It is keyed by version plus assembly/PCK hashes and stores 49 exact certified trace hashes plus 797 distinct semantic-checkpoint hashes. The breadth gate is not complete because only 14 of the target 40+ encounter IDs are certified. The precise next blocker is deterministic coverage-guided seed/character/route scheduling for missing encounter/mechanic sets; the current shipped AutoSlayer chooses a deterministic run from a seed but cannot yet consume a gap manifest. Transformer/v9 work has not started.

## Phase 1 — Trustworthy combat data engine

Build a deterministic, high-throughput iterator around the isolated Python simulator without changing its legacy Gym interface.

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

## Phase 2 — Tactical expert

Train an action-conditioned structured policy/value model using mixed-policy exploration and search-improved targets.

Deliverables:

- Token encoders for card instances/zones, creatures/powers/intents, relic state, potions, character resources, and candidate actions.
- Policy logits over native-derived legal actions plus value distributions for victory, HP loss, turns, and resource use.
- A mixed corpus from heuristic, greedy, epsilon-random, model, and search policies across characters, archetypes, deck thickness, encounter tiers, ascension, and low-HP pressure states.
- Iterative expert improvement: search labels states, the network distills the improved policy/value estimates, and the new network supplies better search priors.
- GPU-batched offline training and asynchronous CPU rollout generation.

Promotion gates:

- At least 90% pairwise ranking accuracy on untouched native-held-out encounter/seed groups.
- Statistically significant native search lift over greedy and heuristic baselines, with confidence intervals and no seed overlap.
- Calibration and worst-slice reports, not only aggregate accuracy.
- No default scorer promotion from Python-only labels; native relabeling or declared verified-slice provenance is required.

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
