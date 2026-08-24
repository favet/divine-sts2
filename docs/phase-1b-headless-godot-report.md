# Phase 1b headless-Godot feasibility report

Status: **successful native card-step and complete-turn feasibility result; still non-certifying for ML training**

Tested on 2026-08-21 against:

- STS2 assembly SHA-256 `A1F9E653F1E28E4076558FEE1E60D218619CB7E057B887C6417F62C62C6D7A52`
- STS2 product version `0.1.0+59260271157f76a2896f0eab5bc6ea1245d8b314`
- Official Godot `4.5.1.stable.mono.official.f62fdbde1`
- .NET 9 runtime from `the .NET 9 SDK`

## Result

The smallest tested valid architecture is an isolated Godot project hosting the native game assembly. It does not start Steam or the installed game executable. The host mounts the installed `SlayTheSpire2.pck` as a resource pack, initializes the in-memory save settings and English localization needed by native logging, and runs the Phase 1 probe inside Godot's initialized assembly-load context.

All required stages pass. The full native `PlayCardAction` path validates the play, spends one energy, moves Strike from hand through play to discard, invokes `StrikeIronclad.OnPlay`, traverses `AttackCommand` and `CreatureCmd.Damage`, and changes the native Bygone Effigy creature from 127 HP to 121 HP. No approximate gameplay implementation is involved.

The probe also emits a versioned canonical combat observation and native-derived legal actions. Before the play, the legal action IDs are `play:0:target:1` and `end_turn`; afterward only `end_turn` remains. With native next-move/intent data included, their SHA-256 state hashes are respectively `A2DD1FA037DEF0C537EDAD208A4464D804EAF911FB641668BE29539AB7676BE0` and `D0F60F483EAC6D23C9B471C2AA6F8A75A54C365EA713E783ED9BD7D7A1A72C03` on the pinned build. The next-turn hash is `EC947E066D64F872505D16D1E117FB8FF6F671E3223BF9A889A75D92D7C3A496`.

The native turn coordinator is now proven through one complete boundary: end-player-turn phase one and two, enemy side start, native enemy AI execution, enemy side end, and next-player-turn start. The test advances turn 1 to turn 2, preserves player HP at 80 for the Bygone Effigy's non-damaging sleep move, refreshes energy to 3, and draws five cards. The next observation exposes the native `WAKE_MOVE` with a `BuffIntent`; attack intents also expose native-calculated per-hit damage and repeat count. Legal actions are emitted only in the player's `Play` phase.

## Required headless seam

The model action graph calls presentation systems even in a model-only combat. Harmony prefixes bypass an explicitly inventoried set of 26 methods:

- `CreatureCmd.TriggerAnim`, returning `Task.CompletedTask`
- all 12 current public void methods declared by `SfxCmd`
- the current public void thought-bubble VFX method declared by `ThinkCmd`
- card-play queue methods that only update or remove visual card nodes
- `CardPileCmd` visual branches, while preserving the native pile mutation and hooks
- the informational card-play log call used by the benchmark; warnings and errors remain active

The probe asserts every patched `SfxCmd` method returns `void`; it fails if a future build adds a state-bearing return. Animations and audio are not incorporated into the simulator. Card code, legality, resource spending, pile mutation, attack calculation, damage application, powers/triggers, RNG, combat state, and creature state remain native.

This seam is semantically plausible but not yet certified. Differential tests against the shipped game must prove that skipping these methods has no state or RNG effects for every covered mechanic.

## Determinism and throughput

The reproducible `scripts/test-godot-determinism.ps1` check starts two independent headless processes. They produced:

- all stages passing in both processes
- identical assembly hashes
- identical initial and post-action canonical state hashes
- identical next-turn state hashes, legal actions, and full native-turn facts
- identical legal-action IDs and full `PlayCardAction` transition facts
- identical RNG prefixes and RNG benchmark checksums
- exactly 6 Strike damage, one energy spent, and native hand-to-discard movement in both processes

The latest direct native-effect benchmark measured 9,207 `StrikeIronclad.OnPlay` transitions/second for 10,000 iterations. Other runs measured roughly 8,500-9,700/second.

The more representative warm-worker benchmark reconstructs a fresh deterministic run and combat, emits canonical state and legal actions, executes the full native `PlayCardAction`, then emits the resulting state and actions. It measured 495.1 complete cycles/second, or 2.020 ms/cycle, over 100 cycles. Four concurrent isolated workers on a 20-logical-processor machine produced 1,568.4 aggregate cycles/second; individual workers measured 359.3-419.9 cycles/second under contention. All four produced the same deterministic checksum.

Reflection found no native serialization surface for active `CombatState`. The shipped run/player serializers are useful outside combat but do not provide a hidden full-combat clone. For now, certified branching should reconstruct a combat in a warm worker and replay its action history. A custom deep copier is rejected unless exhaustive differential testing can prove it captures every mutable mechanic.

The benchmark resets only target HP between iterations and invokes `StrikeIronclad.OnPlay` directly. It excludes full `PlayCardAction`, card movement, energy payment, action queue orchestration, state cloning, observation encoding, and process startup. It is therefore evidence that native model transitions can be fast enough, not a claim about final environment throughput.

## Engineering fixes proven necessary

1. Load `sts2.dll` into the same `AssemblyLoadContext` as the Godot-hosted probe. Loading it through `AssemblyLoadContext.Default` creates a second `GodotSharp` binding whose native function table is uninitialized and crashes at `Godot.OS`.
2. Mount the shipped PCK before localization initialization so `res://localization/eng` is available.
3. Inject in-memory `SettingsSave`, `PrefsSave(FastMode=Instant)`, and default `ProgressState`; never construct persistent save stores.
4. Suppress the explicitly inventoried presentation-only boundary.

## What this changes

Native rehosting is no longer merely promising: a deterministic full native card step plus a compact observation/action boundary is demonstrated. A handwritten replacement simulator should not be started now. The preferred trajectory is a persistent pool of isolated headless Godot workers executing the shipped native mechanics behind a compact RPC environment API.

This architecture matches the combat-first strategy: spend search and self-play compute inside combats, summarize their outcome as survival probability and HP/resource distributions, then train the slower run-level policy over card rewards, map nodes, events, shops, boss rewards, and rest sites using those combat values. Presentation systems are outside both loops.

## Remaining certification gates

Do not train a policy on this environment yet. The next milestone must:

1. Generalize deterministic reconstruction-and-replay from the one-card scenario to arbitrary decks, encounters, RNG counters, and action histories; then measure cost versus replay depth.
2. Generalize the proven end-turn/enemy-turn/start-turn execution and intent extraction across damaging, multi-enemy, summoning, and death-ending turns.
3. Expand full-action coverage across exhaust, X-cost, powers, relics, statuses, summons, deaths, multi-hit/AOE, card choices, and potions.
4. Differentially replay recorded shipped-game transitions and require exact hashes for state and every RNG stream.
5. Put the proven in-process boundary behind persistent IPC and benchmark request batching and worker-pool scheduling without process startup.
6. Extend the same native-state/action contract to rewards, map paths, events, shops, boss rewards, and rest sites.
7. Pin every certified result to the game DLL and PCK hashes and invalidate certification after updates.

## Decision

Continue with native engine-hosted rehosting. The full card wrapper is no longer the blocker; snapshot/restore, turn progression, mechanic breadth, and differential certification are. Keep the environment labeled **non-certifying** until those gates pass. The result strongly supports the fundamental trajectory: reuse the shipped mechanics and remove only proven presentation dependencies, instead of reproducing thousands of mechanics by hand.
