# divine-sts2

Deterministic, build-pinned headless research tooling for Slay the Spire 2.

divine-sts2 provides two complementary native execution backends:

- A persistent Godot/.NET worker farm for high-throughput trajectory generation.
- A full-application bridge for optional end-to-end acceptance and differential replay.

Both execute mechanics from a compatible, user-owned game installation. This
repository contains no game executable, assembly, resource pack, save, art,
audio, decompiled source, dataset, or pretrained model.

> **Research status:** the persistent native environment and rollout
> infrastructure have a clean-clone smoke path. No holistic run policy is
> currently promoted. The full-application bridge is an optional acceptance
> path, not the first contributor smoke. Simulator throughput, mechanical
> fidelity, and policy strength are independent claims.

## What matters for this handoff

If you already have the game and the normal Windows development tools, follow
only [Quick start](#quick-start) and [First clean-clone acceptance](#first-clean-clone-acceptance).
The first useful result is a passing doctor plus one valid terminal episode.
You do not need private workspace access, training data, a checkpoint, or the
full-application bridge for this check.

| Your goal | Status | What to do |
| :--- | :--- | :--- |
| Validate the public clone | Ready now | Run Quick start, then the one-worker smoke |
| Generate random native episodes | Ready after the smoke | Run the two-worker sample below |
| Work on the full-application bridge | Separate advanced path | Ask the owner after the persistent smoke passes |
| Train or promote a policy | Not part of this handoff | Wait for a separately documented data/training task |
| Work on the live advisor/private workspace | Separate private work | Use the private-repository onboarding, not this clone |

## Background evidence (not a setup requirement)

Skip this section on the first pass. It records dated development measurements
for reviewers; it does not change the one-worker handoff.

The dated rollout report records **4,955 complete episodes/hour** on
2026-08-23 with six workers and 100 A1 episodes, including a 17-second cold
start. It recorded 98 valid terminal episodes and two presentation failures;
the exact seeds were subsequently repaired and replayed. This is a
development-machine measurement, not a clean-clone target. RAM is the practical
worker limit. The full-application bridge is slower and is intended for
acceptance, not bulk collection. See [the rollout report](docs/native-rollout-farm.md)
and [the evidence guide](docs/project-status-and-review-guide.md) for scope and
caveats.

The current pinned evidence was collected on game build
`0.1.0+59260271157f76a2896f0eab5bc6ea1245d8b314`. Deep doctor reports and
enforces the supported fingerprints; a mismatched build is unsupported until
its acceptance suite passes.

<details>
<summary>Fingerprint values (for diagnosing an unsupported-build result)</summary>

- Assembly SHA-256: `A1F9E653F1E28E4076558FEE1E60D218619CB7E057B887C6417F62C62C6D7A52`
- PCK SHA-256: `42520EB8B0911C6C0F0BD102D92B33F41ABD4D26B83489817D0A6DBD7DD48587`

</details>

## Assumed setup

The host tools and lawfully installed game are assumed to be ready. The first
bootstrap only downloads repo-local Godot/.NET tools and installs this package
into the active virtual environment, so allow internet access and enough free
disk space for that step.

## Quick start

```powershell
git clone https://github.com/favet/divine-sts2.git
cd divine-sts2
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pwsh scripts/bootstrap.ps1
```

Bootstrap builds the persistent host, imports the Godot C# solution, and runs
deep doctor. It exits nonzero if the worker cannot complete its startup smoke;
do not continue to rollout commands until that check passes.

If Steam discovery fails, set `STS2_GAME_ROOT` to the installed game directory.
See [.env.example](.env.example) for every override.

### First clean-clone acceptance

Run the smallest smoke first. Do not start a 100-episode farm until this
finishes with `ok: true` and one valid terminal episode:

```powershell
python -m sts2_native_sim.cli doctor --deep --json
python python/native_rollout_farm.py `
  --episodes 1 `
  --workers 1 `
  --ascension 1 `
  --summary-only `
  --output-dir artifacts/friend-smoke
```

Expected smoke results are:

- doctor JSON has `"ok": true`, matching assembly/PCK hashes, and a
  `worker_hello` object;
- rollout summary has `requested_episodes: 1`,
  `completed_episodes: 1`, `valid_terminal_episodes: 1`, `errors: 0`, and
  `worker_restarts: 0`.

Here, a **valid terminal episode** means the native environment reached a
terminal outcome without a worker error; it may be a death/defeat. `victories`
is reported separately and is not required for this infrastructure smoke.

After the one-worker smoke passes, run a small parallel sample:

```powershell
python python/native_rollout_farm.py `
  --episodes 2 `
  --workers 2 `
  --ascension 1 `
  --summary-only `
  --output-dir artifacts/smoke-a1
```

For a larger throughput sample, use 100 episodes and 4 workers only after the
two-worker sample is clean. The commands above use the built-in random smoke
path; policy scripts are separate and require an explicitly supplied
checkpoint and datasets.

### Later: full-application acceptance (owner-directed)

Do not run this for the first handoff. It launches the shipped game process and
should be used only after the persistent worker passes, with owner direction.
Expect sandbox/settings side effects and multiple game processes:

```powershell
$env:STS2_GAME_ROOT = 'C:\path\to\Slay the Spire 2'
dotnet build src/Sts2.NativeSim.FullAppBridge/Sts2.NativeSim.FullAppBridge.csproj `
  -c Release `
  -p:GameDataDir="$env:STS2_GAME_ROOT\data_sts2_windows_x86_64"
python python/full_app_bridge_acceptance.py --help
```

The acceptance harness is game-dependent and its historical benchmark reports
must be rerun on the pinned build before being treated as current certification.

### If setup fails

Stop at the first nonzero command and keep its complete output. Rerun
`pwsh scripts/bootstrap.ps1` after a transient download failure; downloads use
partial files and staged extraction, so an interrupted run is resumable. Do not
delete the game installation or copy game files into this repository.

Common corrections:

- `Slay the Spire 2 was not found`: set `STS2_GAME_ROOT` to the directory that
  contains `SlayTheSpire2.exe`, `SlayTheSpire2.pck`, and
  `data_sts2_windows_x86_64\sts2.dll`.
- `Unsupported game build`: update the game only when the project explicitly
  supports that build; do not bypass the hash check.
- `Godot ... was not found` or `.NET 9 ... was not found`: rerun bootstrap from
  the activated `.venv` and allow the `.tools` downloads to finish.
- A failed deep doctor: do not run rollouts; send the JSON doctor output and
  the bootstrap output to the repository owner.

## Out of scope for this handoff

Training and policy promotion are separate owner-directed work. No public
training shards, checkpoint, or promoted policy are included, and none is
needed for the contributor smoke or random rollout sample.

## Repository map

- `src/`: native C# protocol, persistent host, trace exporter, and full-app bridge
- `python/sts2_native_sim/`: installable Python client, discovery, search, and scoring package
- `python/`: rollout, compilation, training, differential, and acceptance commands
- `schemas/`: canonical state and legal-action schemas
- `scripts/`: setup, build, benchmark, and isolated AutoTrace commands
- `tests/`: game-independent public smoke tests
- `docs/`: architecture, fidelity evidence, limitations, and research roadmap

The worker protocol supports reset/observe/legal-actions/step plus deterministic
fork and replay-based restore. Active combat has no complete shipped serializer;
non-resident restore therefore reconstructs and replays actions.

## Reproducibility and safety

Run the public-tree gate before committing:

```powershell
pwsh scripts/test-public-tree.ps1
```

The gate compiles Python and the game-independent C# projects and rejects
machine-specific paths, access tokens, or distributable game/model binaries.
CI runs the same checks without requiring the game.

Before opening a pull request, run:

```powershell
python -m compileall -q python tests
python -m pytest -q
pwsh scripts/test-public-tree.ps1
python python/full_app_bridge_acceptance.py --help
python python/generate_full_act_trajectories.py --help
python python/ingest_community_runs.py --help
```

The help commands must print usage and exit without launching the game, reading
user settings, or creating a sandbox. Ruff and mypy are currently advisory
until the legacy/experimental Python modules are separated from the supported
package.

Research artifacts must record the code revision, game build and hashes, schema
version, seed namespace, command, hardware, and source-shard hashes. Community
or session-derived data may be published only with documented redistribution
permission and privacy review.

## Second-computer handoff

The public clone is the first acceptance boundary. A collaborator does not need
private workspace access to validate the persistent worker. Ask them to return:

1. the output of `python -m sts2_native_sim.cli doctor --deep --json`;
2. `artifacts/friend-smoke/summary.json`; and
3. the exact command output if either smoke fails.

Do not request game binaries, save files, screenshots, private session data, or
unreviewed community runs. Add a collaborator to any private repository only
after the public one-worker smoke succeeds and only for work that genuinely
requires private material.

For this handoff, stop after sending the doctor output and smoke summary. The
owner will provide the next task or the private-repository instructions.

## Legal

divine-sts2 is independent and is not affiliated with Mega Crit. Users provide
their own compatible game installation. The MIT license covers only original
code and documentation in this repository; see [NOTICE.md](NOTICE.md).
