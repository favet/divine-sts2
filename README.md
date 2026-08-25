# divine-sts2

Deterministic, headless execution and RL training environment for Slay the Spire 2.

Runs persistent, presentation-suppressed .NET 9 workers executing mechanics directly from your local Slay the Spire 2 installation. Contains no copyrighted assets, game binaries, or models.

---

## Requirements

* **OS**: Windows 10 / 11 (x64)
* **Game**: Legally installed copy of Slay the Spire 2 via Steam
* **Runtime**: Python 3.10+ and PowerShell

---

## Quickstart

```powershell
git clone https://github.com/favet/divine-sts2.git
cd divine-sts2
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pwsh scripts/bootstrap.ps1
```

`bootstrap.ps1` builds the persistent .NET 9 host, configures local dependencies, and verifies the connection to your local game files.

---

## Tool Usage

### 1. Verify Installation
Check game assembly compatibility, SHA-256 signatures, and worker startup:
```powershell
python -m sts2_native_sim.cli doctor --deep --json
```

### 2. High-Throughput Rollout Farm
Run parallel headless workers to generate game episodes:
```powershell
python python/native_rollout_farm.py `
  --workers 4 `
  --episodes 10 `
  --ascension 1 `
  --summary-only
```

### 3. Gymnasium Vector Environment
Use the standard vectorized RL environment in Python:

```python
from sts2_native_sim import NativeWorkerPool, extract_agent_observation
from sts2_native_gym import Sts2NativeVectorEnv

# Create a 4-worker vectorized environment
with Sts2NativeVectorEnv(workers=4, ascension=1) as env:
    obs, info = env.reset(seed=42)
    for _ in range(100):
        # Take actions using legal action masks from info
        actions = [legals[0] for legals in info["legal_action_ids"]]
        obs, rewards, terminations, truncations, info = env.step(actions)
        if any(terminations):
            break
```

---

## Configuration

The tooling automatically locates your standard Steam installation. If your game is installed on a separate drive, create a `.env` file in the repository root:

```ini
STS2_GAME_ROOT=D:\SteamLibrary\steamapps\common\Slay the Spire 2
```

---

## Troubleshooting

| Issue | Cause | Fix |
| :--- | :--- | :--- |
| `Slay the Spire 2 was not found` | Game is on a non-default drive | Set `STS2_GAME_ROOT` in `.env` pointing to the game directory. |
| `Unsupported game build` | Game DLL/PCK hash mismatch | Ensure game build matches pinned target (`0.1.0+...`). |
| `.NET 9 SDK was not found` | Missing SDK tooling | Rerun `bootstrap.ps1` to download local `.tools/dotnet9`. |
| `worker_poisoned` | Unmanaged task abort | Pool automatically replaces the worker; restart farm if recurring. |

---

## Legal

divine-sts2 is an independent research project and is not affiliated with or endorsed by Mega Crit. Users must provide their own legally obtained copy of Slay the Spire 2. Distributed under the MIT License.

