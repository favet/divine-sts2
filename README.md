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
pwsh scripts/bootstrap.ps1
```

If Steam discovery fails, set `STS2_GAME_ROOT` to the installed game directory.
See [.env.example](.env.example) for every override.

Validate paths and start a real worker:

```powershell
divine-sts2 doctor --deep
```

Generate a small throughput sample:

```powershell
python python/native_rollout_farm.py `
  --episodes 100 `
  --workers 4 `
  --ascension 1 `
  --summary-only `
  --output-dir artifacts/smoke-a1
```

Rollout policy scripts require an explicitly supplied checkpoint and any
optional macro/card datasets. None are silently downloaded or distributed.

## Training on NVIDIA

Install training dependencies and verify CUDA:

```powershell
pwsh scripts/bootstrap.ps1 -Train
python -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

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
- `tests/`: opt-in trace-exporter smoke test requiring a compatible game install
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

Research artifacts must record the code revision, game build and hashes, schema
version, seed namespace, command, hardware, and source-shard hashes. Community
or session-derived data may be published only with documented redistribution
permission and privacy review.

## Legal

divine-sts2 is independent and is not affiliated with Mega Crit. Users provide
their own compatible game installation. The MIT license covers only original
code and documentation in this repository; see [NOTICE.md](NOTICE.md).
