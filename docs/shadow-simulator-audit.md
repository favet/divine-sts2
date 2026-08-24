# Shadow simulator audit

The external `zhiyue/sts2-rl-agent` Python simulator is an optional, non-authoritative research accelerator. Its source is not copied into this repository, it is not a runtime dependency, and its transitions are never labeled `shipped_native`.

The audit pins an external checkout, runs a NativeSim-owned probe in that checkout's isolated Python environment, executes the corresponding shipped-native scenario, normalizes both results into explicit transition facts, and emits a per-mechanic trust matrix. Cross-simulator hashes are intentionally not compared. A passing row means only that the named fields matched for the recorded scenario and pinned revisions; it does not promote the Python simulator to a mechanics authority.

Run the local comparator after separately obtaining the external checkout:

```powershell
$shadowRoot = Join-Path $env:TEMP 'sts2-rl-agent-audit-1b7e7ce'
$shadowPython = Join-Path $shadowRoot '.audit-venv\Scripts\python.exe'
python .\python\shadow_sim_audit.py `
  --shadow-root $shadowRoot `
  --shadow-python $shadowPython `
  --expected-revision 1b7e7ce35e608722650763938c153ea8bc370333 `
  --expected-patch-sha256 b9894129fb239acffc38be68d014f55ede201a4c83824857f6a1551eec1c6ca9 `
  --verify-suite --benchmark `
  --parallel-benchmark-workers 20 --parallel-benchmark-episodes 4000 `
  --output .\artifacts\shadow-simulator-audit-patched.json
```

The current first slice covers three ordinary Strikes, energy spend, hand-to-discard movement, Bygone Effigy's Slow state and multi-card damage scaling, and the complete sleep/wake turn boundary. The audit also runs the external package without its test-suite bootstrap and records whether `sts2_env.powers` must be imported explicitly to register power implementations. Expansion must be stratified by character, card family, powers/relics, creature composition, choices, RNG use, and action composition. Uncompared mechanics remain `python_unverified`. Even a verified slice is ineligible as an authoritative label source and requires native relabeling before default-scorer training.

The upstream repository currently has no detectable license file. Do not copy, redistribute, or create a derivative integration unless its author supplies compatible permission. The audit uses only a separately installed research checkout and an independently written adapter.

## Pinned audit result

Revision `1b7e7ce35e608722650763938c153ea8bc370333` passed 4,609 upstream tests with one skip. On this machine its random Act 1 benchmark measured 3,490 steps/second and 140 combats/second, substantially below the repository headline but still useful as a candidate generator.

With `sts2_env.powers` explicitly imported before combat construction, the three-Strike Bygone Effigy slice matched every normalized checkpoint field. Both engines spent one energy per Strike, moved all three cards to discard, dealt 6, 6, then 7 damage through Slow, completed the non-damaging sleep turn, refreshed to three energy, redrew the deck, and exposed `WAKE_MOVE` with a buff intent.

The default external-package probe did not match. Bygone Effigy had no Slow power, and its third Strike dealt 6 rather than the native 7. The upstream pytest `conftest.py` performs the global power-registry import, while the production `combat_env.py` and `train_combat.py` do not do so directly. Consequently, the current upstream trainer must not be used as-is. The explicit bootstrap is necessary but does not prove other mechanics.

The machine-readable result is `artifacts/shadow-simulator-audit.json`. Its verified rows remain non-authoritative and are not eligible for default-scorer training.

## Isolated research patch result

A local-only patch at tracked-diff SHA-256 `b9894129fb239acffc38be68d014f55ede201a4c83824857f6a1551eec1c6ca9` moves the power-registry initialization to `CombatState`, adds a clean-process regression test, and adds a deterministic multi-process benchmark. The external checkout remains separate and unlicensed; this fingerprint records the evaluated patch without copying its source into NativeSim.

The patched production probe matched all five native checkpoints without an explicit adapter bootstrap. The complete external suite passed 4,611 tests with one skip. In the recorded audit run, one process produced 3,224 steps/second and 129 combats/second, while 20 isolated workers produced 22,503 steps/second and 968 combats/second over 4,000 deterministic episodes (92,944 transitions). Earlier sustained sweeps peaked at 23,039 steps/second at 20 workers; 24 workers were slower, so 20 is the measured concurrency ceiling on this host.

The machine-readable patched result is `artifacts/shadow-simulator-audit-patched.json`. This validates only the enumerated Bygone/Strike slice. All other Python mechanics remain unverified, and all promoted training labels still require native differential validation or relabeling.

## Phase 1 matrix extension

`python/shadow_sim_matrix_audit.py` now compares eight scenarios: four action compositions against Bygone Effigy, an Axebot Defend/full-turn transition, the initial states of Bowlbugs Normal and Seapunk Normal, and Aeonglass Boss through its complete first enemy turn. The production `CombatIterator` accepts native string seeds, attaches the shipped named run streams, uses the private encounter RNG only for composition, routes creature values and HP through `Niche`, and routes initial plus runtime move decisions through `MonsterAi`. The matrix probe constructs every shadow scenario through that iterator. Every normalized checkpoint passes on isolated tracked-diff SHA-256 `07d0144a2aa9e2017491616fe731f3fae3274d6f6656bb2d1740337e22756a5e`.

This extension verifies ordinary attack, multi-hit damage, block gain/clear, energy spend/reset, hand/discard/reshuffle order, Weak/Frail application, Slow scaling, Vulnerable application/tick behavior, the fixed Bygone sleep/wake boundary, one blocking multi-card-choice/exhaust sequence, two multi-creature creation boundaries, and Aeonglass's initial powers plus Ebb transition only in those recorded scenarios. Seapunk Normal passes 37 initial-state fields; Aeonglass passes 33 initial fields and 30 post-turn fields. Focused tests also cover the decompiled three-move cycle, ascension values, six-card Withering Presence trigger, and progressive Wither upgrades. The complete isolated suite passes 4,629 tests with one skip.

## Encounter composition correction

The first broader encounter comparison exposed a definition mismatch before transition comparison: native `BOWLBUGS_WEAK` always starts with Bowlbug Rock and selects Egg or Nectar for the second slot, while the external simulator fixed Egg plus Nectar. Direct decompilation of shipped `BowlbugsWeak.GenerateMonsters` and `BowlbugsNormal.GenerateMonsters` confirmed the rules. The isolated patch now matches those composition rules and tests worker variation and tough-ascension HP ranges. This correction does not yet certify per-seed selection or HP parity because the external encounter setup currently shares one RNG where the shipped run uses separated native streams.

`python/shadow_encounter_composition_audit.py` generalizes this check across the 80-model native encounter catalog. At 32 deterministic seed samples per model, 79 encounters have identical observed ordered enemy-model signature sets, and Ruby Raiders has a separately decompiled-equivalent selection rule. There are no missing Python setups. `artifacts/shadow-encounter-composition-audit.json` records the machine-readable result and explicitly limits its claim to composition support.
