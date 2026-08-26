# divine-sts2

[![.NET 9](https://img.shields.io/badge/.NET-9.0-512BD4?logo=dotnet&logoColor=white)](https://dotnet.microsoft.com/)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Throughput](https://img.shields.io/badge/Throughput-1%2C514%2B%20dec%2Fsec-success)](#benchmarks)
[![Stability](https://img.shields.io/badge/Stability-100%25%20Zero--Crash-brightgreen)](#benchmarks)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

High-throughput, deterministic, headless reinforcement learning and MCTS execution environment for **Slay the Spire 2**.

Executes game mechanics directly from your local Steam installation inside isolated, presentation-suppressed .NET 9 workers. Contains zero copyrighted assets, game binaries, or proprietary art.

---

## Benchmarks

Continuous 100-second sustained stress benchmark across 20 isolated native workers:

| Metric | Measured Value |
| :--- | :--- |
| **Sustained Throughput** | **1,514.8 decisions / sec** |
| **Combat Completion Rate** | **3,140.9 combats / min** (5,245 total) |
| **Native Decisions Evaluated** | **151,776 decisions** |
| **Unmanaged Crashes / Aborts** | **0 (100% Stability)** |
| **Worker Memory Footprint** | **~221 MB / worker** (~4.4 GB total for 20 workers) |

---

## Requirements

* **OS**: Windows 10 / 11 (x64)
* **Game**: Legally installed copy of Slay the Spire 2 via Steam
* **Runtime**: Python 3.10+ and PowerShell (`pwsh`)

---

## Quickstart

```powershell
# 1. Clone & enter repository
git clone https://github.com/favet/divine-sts2.git
cd divine-sts2

# 2. Setup Python environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# 3. Bootstrap persistent .NET 9 host & verify game link
pwsh scripts/bootstrap.ps1
```

---

## Usage

### 1. Diagnostics & Environment Verification
Verify assembly compatibility, SHA-256 signatures, and worker startup:
```powershell
python -m sts2_native_sim.cli doctor --deep --json
```

### 2. High-Throughput 20-Worker Benchmark
Run the sustained multi-worker soak benchmark:
```powershell
python python/soak_test_20_workers.py
```

### 3. Parallel Rollout Farm
Generate parallel game trajectories across headless workers:
```powershell
python python/native_rollout_farm.py --workers 6 --episodes 100 --ascension 1 --summary-only
```

### 4. Neural Turn Sequence Search
Run policy-guided turn search evaluated by the Set Transformer Critic $V(s')$:
```powershell
python python/neural_turn_search.py
```

---

## Gymnasium Vector Environment

Standard vectorized RL environment interface with action masking and state restore handles:

```python
from sts2_native_sim import NativeWorkerPool, extract_agent_observation
from sts2_native_gym import Sts2NativeVectorEnv

# Initialize vectorized 4-worker environment
with Sts2NativeVectorEnv(workers=4, ascension=1) as env:
    obs, info = env.reset(seed=42)
    for _ in range(100):
        # Step environment using legal action masks from info
        actions = [legals[0] for legals in info["legal_action_ids"]]
        obs, rewards, terminations, truncations, info = env.step(actions)
        if any(terminations):
            break
```

---

## Configuration

The runtime auto-detects standard Steam installations. For custom library paths, define `STS2_GAME_ROOT` in `.env`:

```ini
STS2_GAME_ROOT=D:\SteamLibrary\steamapps\common\Slay the Spire 2
```

---

## Troubleshooting

| Error | Root Cause | Resolution |
| :--- | :--- | :--- |
| `Slay the Spire 2 was not found` | Non-default library drive | Set `STS2_GAME_ROOT` in `.env`. |
| `Unsupported game build` | Game DLL/PCK hash mismatch | Verify game installation matches target build (`0.1.0+`). |
| `.NET 9 SDK was not found` | Missing SDK tooling | Run `scripts/bootstrap.ps1` to download local `.tools/dotnet9`. |
| `worker_poisoned` | Unmanaged task abort | Pool auto-recycles worker; restart farm if persistent. |

---

## Legal

`divine-sts2` is an independent research project and is not affiliated with or endorsed by Mega Crit. Users must provide their own legally obtained copy of Slay the Spire 2. Distributed under the [MIT License](LICENSE).

