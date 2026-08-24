# A1 policy recovery plan

Last updated: **2026-08-23**

## Decision

Stop using five simultaneous full-application runs as the development loop. They are slow by construction and, until this audit, were also reporting invalid progress. Use the persistent native environment for data generation, search, and large seeded evaluations. Use the shipped full application only for bounded acceptance and differential checks.

The immediate target is a reproducible Ascension 1 policy that completes Act 3. Ascension 10 work begins only after the A1 promotion gate passes.

## What the audit established

- The reported v10 78.14% validation Top-1 measures imitation of the collector, not combat advantage or run success.
- The root trajectory set contains 40,192 records and no positive `v_win` outcomes.
- At least 3,073 historical actions did not change state: 2,841 potion actions and 232 reward actions.
- Macro training was order-confounded: approximately 98.7% of examples selected the first offered option.
- V11's old checkpoint encoded zero enemy identities because nullable metadata aborted vocabulary discovery.
- The densified “winning” corpus did not contain replay-verified winning seeds and must not be used.
- The full-app bridge discarded card rewards, crashed at rests, and mishandled potion execution/targeting. These defects are repaired and covered by fail-fast progress checks plus focused tests.
- A corrected replay of Regent seed `QU62X22APK` took 54.65 seconds, won five combats, used a potion successfully, and died on floor 9. There was no infrastructure failure to hide the policy failure.
- The earlier macro ingester split acquired reward text on whitespace, called those tokens the offered cards, and selected token zero as the label. That produced 67,013 fake card choices with a 98.01% first-option rate. Campfire rows used post-action HP. Both paths and their checkpoints are removed from active inference.
- Ten previously ignored agentic archives contain 149,682 structured events. The corrected exact-state extractor yields 7,777 aligned combat/macro decisions; the 146 exact card picks have a 28.08% first-option rate. After merging other explicitly offered/picked sources, `compiled_macro_decisions.json` contains 10,006 decisions and a 38.5% card first-option rate.
- Successful A10 timelines average 2.23, 2.06, and 1.64 elites in Acts 1-3 (5.93 total over 742 wins). Elite routing is now a soft, reproducible sampled score combining exact offered-route behavior with this winning occupancy, never an HP permission gate.
- The persistent shipped-DLL farm now sustains 6,338 full trajectory episodes/hour with six reusable workers. A 1,000-run A1 corpus produced 992 valid terminals, 111,148 compiled combat samples, and 47,360 macro samples; every invalid exact seed was retained for regression replay.
- The first outcome-weighted native v11 smoke candidate was correctly rejected: on the same 200 untouched seeds, mean peak floor regressed from 7.91 to 6.09 and Act 2 reaches fell from one to zero. V10 remains promoted. This empirically rules out more self-imitation as the recovery strategy.

## Operating loop

### 1. Fast inner loop

Run unit/schema tests on every change. Evaluate combat and macro candidates in the persistent native environment with fixed seed manifests. Record run outcome, peak floor, act/boss clears, HP loss per combat, action count, wall time, and every termination reason. Never cap a run without reporting the cap as the outcome.

### 2. Search-teacher data, not more weak imitation

At sampled legal states, use native branching/restore to estimate action advantage with multiple continuations. Store the complete candidate set, behavior probability, return horizon, terminal outcome, seed, build, character, and schema version. Train an action-ranking or advantage objective from these comparisons. Do not call floor/HP weighting “advantage.”

Start with Act 1 encounters and boss survival, then expand the same exact schema into Acts 2 and 3. Mix on-policy states back into the search-teacher queue so the model learns to recover from its own distributions rather than only copying a collector.

### 3. Repair the observation contract before V11 retraining

Add enemy move identity plus expected damage, repeat count, attack/block/debuff flags, and relevant move/power state. Preserve card instance state, energy/block, legal target identity, potion identity/target type, and character resources. Preflight must fail if any required vocabulary or numeric field is empty or constant.

### 4. Learn macro choices from order-independent outcomes

Randomize offered-option order in training/evaluation. Treat Skip as an explicit candidate. Train drafts, routing, rests, shops, and events from native counterfactual or long-horizon returns. Report per-character and per-choice-type accuracy/lift; aggregate first-option accuracy is not a gate.

The interim active prior must use only `agentic_macro_decisions.jsonl`, corrected explicitly offered/picked records in `compiled_macro_decisions.json`, and `community_route_prior.json`. It uses continuous/contextual scores and deterministic softmax sampling so every legal route remains reachable. Mechanical validity checks may mask unaffordable, out-of-stock, full-belt, or otherwise illegal/no-op actions; gameplay HP thresholds may not mask elites, rests, smiths, potions, shops, upgrades, or card choices.

### 5. Separate throughput from acceptance

- Native batch evaluation: many fixed seeds, used for iteration and promotion statistics.
- Full-app smoke: one deterministic seed for the changed character/path, with unchanged-state and legal-action invariants.
- Full-app breadth: at most two or three workers after a resource preflight; run only at milestone boundaries.
- Five-character full-app campaign: run only after the candidate already passes native gates.

## Promotion gates

All gates use frozen, disjoint seeds and publish the seed list plus complete machine-readable results.

### Gate 0 — trustworthy harness

- Zero exceptions, socket drops, illegal actions, or unchanged-state accepted actions across 10 deterministic full-app smokes.
- Correct combat counting and explicit termination reasons.
- Exact card-reward, rest, shop, event, and potion transitions represented in the smoke set.

### Gate 1 — Act 1 competence

On at least 20 untouched seeds per character:

- Beat the Act 1 boss on at least 50% overall and at least 25% for every character.
- Statistically outperform both the fixed collector and a simple legal-action baseline on boss-clear rate.
- No worst character may regress when aggregate results improve.

### Gate 2 — A1 Act 3 candidate

On a fresh 100-run mixed-character manifest:

- Reach Act 3 on at least 20% overall and at least once for every character.
- Complete an Ascension 1 run on at least 5% overall.
- Reproduce at least one complete A1 victory through the full-app bridge with no invariant failure.

### Gate 3 — A1 promotion

- At least 20% full-run win rate over a larger untouched manifest, with a reported confidence interval.
- Positive lift over the incumbent for every character or an explicit character-specific policy split.
- Calibration, worst-slice, determinism, and full-app acceptance gates all pass.

Only after Gate 3 should Ascension 10 curriculum and evaluation begin.

## Next implementation order

1. Add intent damage/repeat/flags and schema assertions to the full-app and persistent observations.
2. Build an exact-seed corpus auditor and delete-by-manifest training inclusion: only verified records enter a dataset.
3. Generate Act 1 native branch comparisons from a frozen seed/encounter matrix.
4. Train a new checkpoint under a new schema/version; do not overwrite v10/v11.
5. Run Gate 1 natively, then the 10-run full-app Gate 0 suite.
6. Expand data/search to Acts 2 and 3 only after Act 1 boss-clear lift is real.
