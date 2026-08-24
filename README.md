# divine-sts2

Deterministic, build-pinned headless research tooling for Slay the Spire 2.

divine-sts2 provides two complementary native execution backends:

- A persistent Godot/.NET worker farm for high-throughput trajectory generation.
- A full-application bridge for authoritative end-to-end acceptance and differential replay.

Both execute mechanics from a compatible, user-owned game installation. This
repository contains no game executable, assembly, resource pack, save, art,
audio, decompiled source, dataset, or pretrained model.

> **Research status:** the environment and rollout infrastructure are useful;
> no holistic run policy is currently promoted. Simulator throughput, mechanical
> fidelity, and policy strength are independent claims.

## Current evidence

On the development machine, the persistent farm has sustained approximately
6,000–9,000 complete episodes/hour with 6–8 workers. RAM is the practical worker
limit. The full-application bridge is slower and is intended for acceptance,
not bulk collection. See [the rollout report](docs/native-rollout-farm.md) and
[the evidence guide](docs/project-status-and-review-guide.md) for scope and caveats.

The current pinned evidence was collected on game build
`0.1.0+59260271157f76a2896f0eab5bc6ea1245d8b314`. The doctor reports hashes for
the installed assembly and PCK. A mismatched build is unsupported until its
acceptance suite passes.

## Requirements

- Windows 10/11
- A lawfully installed Steam copy of Slay the Spire 2
- Python 3.11+
- .NET 9 SDK
- PowerShell 7
- Godot 4.5.1 .NET, installed automatically by the bootstrap script
- Optional NVIDIA CUDA GPU for training; native rollout workers remain CPU/RAM-bound

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
two-worker sample is clean.

Rollout policy scripts require an explicitly supplied checkpoint and any
optional macro/card datasets. None are silently downloaded or distributed.

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

## Training on NVIDIA

Install training dependencies and verify CUDA. The generic PyPI dependency may
install a CPU-only Torch build; NVIDIA users must select a Torch wheel matching
their CUDA driver before running the command below:

```powershell
pwsh scripts/bootstrap.ps1 -Train
python -c "import torch; print(torch.__version__, torch.cuda.is_available()); raise SystemExit(0 if torch.cuda.is_available() else 1)"
```

To force a CUDA wheel, pass the official index selected for the friend’s driver
and CUDA runtime, for example `-TorchIndexUrl
https://download.pytorch.org/whl/cu124`. Use the current command from the
[official PyTorch installer](https://pytorch.org/get-started/locally/) if that
CUDA line has changed.

Simulation is CPU/RAM-bound; an NVIDIA GPU is needed only for the optional
training path. Verify `torch.cuda.is_available()` before using `--device cuda`.
If it is false, the installer selected CPU Torch and training should stop until
the correct official CUDA index is used.

The complete-state V12 trainer supports CUDA, mixed precision, pinned-memory
loading, and episode-grouped validation:

```powershell
python python/train_v12_combat_policy.py `
  --shards artifacts/training/*.jsonl.gz `
  --output models/v12-candidate.pt `
  --device cuda `
  --amp `
  --workers 4
```

Checkpoints are candidates until they improve held-out native run results on
identical seeds and pass the documented promotion gates.

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

## Legal

divine-sts2 is independent and is not affiliated with Mega Crit. Users provide
their own compatible game installation. The MIT license covers only original
code and documentation in this repository; see [NOTICE.md](NOTICE.md).
