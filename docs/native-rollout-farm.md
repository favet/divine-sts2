# Shipped-DLL rollout farm

## Current capability

`native_rollout_farm.py` runs complete seeded runs in persistent headless Godot
workers. Each worker loads the shipped `sts2.dll` once and is reused across a
dynamic episode queue. Run setup, maps, encounters, rewards, relics, events,
shops, campfires, act transitions, the A10 second boss, and the Architect terminal
event execute through shipped game models. Presentation, audio, save writes, and
UI waits are suppressed at explicit headless seams.

This is not the old handwritten approximate simulator. Output records declare
`mechanics_source: shipped_sts2_dll` and include the installed game build hashes.

Measured on 2026-08-23 with six workers and 100 A1 episodes:

- 4,955 total episodes/hour including a 17-second cold start
- 210.9 native decisions/second
- 98 terminal episodes and two presentation failures; both exact failing seeds
  were subsequently fixed and replayed successfully
- a separate post-fix 50-episode soak completed 50/50 with no errors, caps, or
  worker restarts at 3,758 episodes/hour including a 23-second cold start
- running workers can consume about 2.1 GB each during trajectory capture, so
  available RAM—not a single-threaded scheduler—is the present scale limit

These numbers establish thousands of runs/hour on this machine. They do not
establish good play. The promoted policy still usually dies in Act 1; simulator
capacity and policy strength are separate acceptance gates.

A subsequent full 1,000-episode trajectory capture completed in 568 seconds:
6,338 episodes/hour, 303.7 decisions/second, 992 valid terminals, zero worker
restarts, 111,148 compiled combat samples, and 47,360 compiled macro samples.
The eight invalid pre-fix episodes were excluded and their exact seeds were used
to repair full-belt rewards/shops, noncombat death termination, and internally
triggered end turns.

An outcome-weighted v11 smoke candidate was trained separately and rejected on
an untouched paired 200-seed evaluation. V10 averaged floor 7.91 and reached Act
2 once; v11 averaged floor 6.09 and never reached Act 2. V11 remains an
unpromoted artifact. This is evidence that self-imitation, even outcome-weighted,
is insufficient; the next data source must be native branch/search comparisons.

## Generate trajectories

```powershell
python python/native_rollout_farm.py `
  --episodes 1000 `
  --workers 6 `
  --ascension 1 `
  --output-dir artifacts/native_rollouts/a1-1000
```

Use `--start-index` and a distinct output directory to resume with nonoverlapping
deterministic episode IDs. `--summary-only` is for throughput/soak tests and does
not create training transitions.

## Compile outcome-weighted data

```powershell
python python/compile_native_rollouts.py `
  artifacts/native_rollouts/a1-1000 `
  --combat-output artifacts/training/a1-1000-combat.jsonl.gz `
  --macro-output artifacts/training/a1-1000-macro.jsonl.gz
```

The compiler accepts only valid terminal episodes. It assigns an episode return,
computes a character/ascension/floor baseline, and writes clipped AWR weights.
This prevents failed on-policy runs from being mislabeled as expert wins while
still allowing better-than-peer trajectories to carry more weight.

## Train without episode leakage

```powershell
python python/train_v10_combat_policy.py `
  --shards artifacts/training/a1-1000-combat.jsonl.gz `
  --epochs 15 `
  --output models/v11_native_combat_policy.pt
```

Validation is grouped by episode/seed. Do not compare its numbers to the old
random-transition split: that split leaked neighboring states from the same run
into training and validation.

The macro output preserves card draft, map, rest/smith, event, shop, potion,
upgrade/remove, and other choice records with the same outcome weights. The live
rollout policy currently consumes exact option-aligned examples, 42,143-run
sample-shrunk card outcome statistics and synergies, and the 742 A10-win routing
corpus. It contains no binary health gate that forbids elites.

## Full-run mechanics acceptance

```powershell
python python/run_seeded_full_act_corpus.py `
  --seeds 2 --start 101 --repeat --ascension 10 --step-limit 2500
```

The current acceptance result is deterministic victory twice with the same final
hash, 847 decisions per run, 46 map rooms, four A10 bosses, three act transitions,
and the Architect terminal event. This forced-survivability corpus validates
mechanical reachability, not policy competence.

## Promotion gates

Never promote a checkpoint from imitation accuracy alone. Require all of:

1. deterministic full-run mechanics acceptance at A1 and A10;
2. zero protocol errors, step caps, and worker restarts in a meaningful soak;
3. held-out episode/seed validation, not transition-level leakage;
4. improved native-run survival, Act 2/3 reach, elite count, and win rate against
   the current checkpoint on identical seed sets;
5. per-character reporting so one character cannot conceal collapse in another.
