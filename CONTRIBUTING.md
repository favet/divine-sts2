# Contributing

Thank you for helping with divine-sts2. Open an issue before large architectural
changes. Keep simulator fidelity, rollout throughput, and policy quality as
separate claims, each backed by a reproducible artifact or command.

## Development setup

Use the [README clean-clone acceptance](README.md#first-clean-clone-acceptance)
as the source of truth:

1. Install Python 3.11+, PowerShell 7, and a lawfully installed Steam copy of
   Slay the Spire 2.
2. Create and activate `.venv`; bootstrap supplies the pinned Godot/.NET tools.
3. Run `pwsh scripts/bootstrap.ps1` and wait for deep doctor to pass.
4. Run the one-worker smoke before any larger rollout or full-app acceptance.
5. Before a pull request, run `python -m compileall -q python tests`,
   `python -m pytest -q`, and `pwsh scripts/test-public-tree.ps1`.

The public repository is source-first. Game files, private session data,
community runs, training shards, and advisor tooling stay outside the public
clone unless their redistribution, privacy, and provenance review is complete.

Never commit game binaries/resources, local saves, datasets without documented
redistribution permission, model checkpoints without model cards, or generated
research output. Tests that require the game must fail closed on a build mismatch.

## Claims and promotion

A policy is not promoted from imitation accuracy. Report held-out native run
results on identical seed sets, error/cap rates, per-character results, and the
exact game build. Preserve rejected experiments with their reason outside the
source repository or in a small documented manifest.
