# Contributing

Thank you for helping with divine-sts2. Open an issue before large architectural
changes. Keep simulator fidelity, rollout throughput, and policy quality as
separate claims, each backed by a reproducible artifact or command.

## Development setup

1. Install Python 3.11+, the .NET 9 SDK, and a user-owned Steam copy of Slay the Spire 2.
2. Run `pwsh scripts/bootstrap.ps1`.
3. Run `divine-sts2 doctor --deep`.
4. Run `pwsh scripts/test-public-tree.ps1` before submitting a pull request.

Never commit game binaries/resources, local saves, datasets without documented
redistribution permission, model checkpoints without model cards, or generated
research output. Tests that require the game must fail closed on a build mismatch.

## Claims and promotion

A policy is not promoted from imitation accuracy. Report held-out native run
results on identical seed sets, error/cap rates, per-character results, and the
exact game build. Preserve rejected experiments with their reason outside the
source repository or in a small documented manifest.
