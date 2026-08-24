# STS2 Full-Application Native Control Bridge Specification & Architecture Report

## 1. Executive Summary & Architecture Verdict

- **Milestone Verdict**: **GO (PROVEN)**
- **Architecture**: `full_application_native` (`SlayTheSpire2.exe --headless --force-steam=off` + isolated C# control mod + external TCP IPC bridge).
- **Core Outcome**: Proved that the shipped, unmodified `SlayTheSpire2.exe` binary serves as the authoritative, controllable headless environment without synthetic mid-combat state reconstruction, synthetic resets, or handcrafted lifecycle reimplementation.

### Benchmark Highlights (4 Concurrent Headless Shipped Processes)
- **State Hash Equality**: **100.0%** bit-for-bit determinism across all 4 independent OS processes across all sequential action steps.
- **Prefix Replay & Branching**: Verified 100% prefix match followed by clean counterfactual state divergence on branching actions (`play_card:0` vs `play_card:1:target:1`).
- **Synthetic State Reconstruction**: **0%** (zero synthetic resets or state injection).
- **Decision Latency (Card Plays)**: Mean = **28.28 ms** (P50).
- **Turn Latency (Full Turn + Enemy AI Turn)**: Mean = **672.40 ms** (P95).
- **Aggregate Decision Throughput**: **26.0 decisions/sec** across 4 concurrent local workers.
- **Memory Footprint**: **766.7 MB RSS** per process.

### Full-Act 1 Autonomous Control Acceptance (2026-08-23)
- **Milestone**: Full-Act 1 route (Floors 1–13) driven autonomously through combats, card rewards, events, rest sites, treasure, and pre-Boss floor.
- **Total Actions Executed**: **300** across all decision phases.
- **Phases Covered**: `combat`, `card_reward`, `rewards`, `map`, `rest_site`, `event`, `treasure`.
- **4-Worker State Hash Equality**: **100%** (zero divergences across all 300 steps).
- **Independent Worker 5 Prefix Replay**: **100% bit-for-bit identical** (300/300 steps matched).
- **Decision Latency (Act 1 Full Route)**: Mean = **244.45 ms** (includes map transitions, combat turns, reward screens).
- **Max Steps Budget**: 300 (raised from 120 in trajectory exporter).

### Multi-Worker Trajectory Export Pipeline (2026-08-23 — 100-Run Hardened Corpus)
- **Runs Collected**: **100 complete runs** (4 workers × 25 runs each, 100% completion rate).
- **Total Transitions Exported**: **22,063** JSONL records to `artifacts/trajectories/`.
- **Character Stratification**: Exactly 20 runs per character across all 5 characters (`IRONCLAD: 20`, `SILENT: 20`, `DEFECT: 20`, `NECROBINDER: 20`, `REGENT: 20`).
- **Episode Depth Quality**: **88.0%** of runs (88/100) produced >= 150 transitions (mean depth = 220.6 transitions/run).
- **Wall Time**: **1,585.58 s** (~26.4 min total wall time across 4 concurrent workers).
- **Aggregate Throughput**: **13.91 transitions/sec** sustained across 4 parallel workers.
- **Latency Profile**: P50 = 35.59 ms, P95 = 714.38 ms, Mean = 234.62 ms.
- **Label Provenance & Authority**:
  - `v_win`: `full_application_native_terminal_outcome` (Gold Native Authority)
  - `v_hp_loss`: `full_application_native_terminal_hp` (Gold Native Authority)
  - `v_relic_ev`: `full_application_native_relic_count` (Gold Native Authority)
  - `v_boss_readiness_heuristic`: `python_approximate` (Clearly marked non-authoritative heuristic)

---

## 2. Pinned Executable & Binary Integrity

- **Installed Directory**: `<STS2_GAME_ROOT>`
- **Application Binary**: `SlayTheSpire2.exe` (Godot .NET single-file host)
- **Engine Version**: `0.1.0+59260271157f76a2896f0eab5bc6ea1245d8b314`
- **Assembly SHA-256 (`sts2.dll`)**: `A1F9E653F1E28E4076558FEE1E60D218619CB7E057B887C6417F62C62C6D7A52`
- **Package SHA-256 (`SlayTheSpire2.pck`)**: `42520EB8B0911C6C0F0BD102D92B33F41ABD4D26B83489817D0A6DBD7DD48587`

---

## 3. Sandboxing & Isolation Model

Each worker process runs within a strictly isolated sandbox directory:
1. **Hardlinks** to `SlayTheSpire2.exe` and `SlayTheSpire2.pck`.
2. **Directory Junctions** for large asset folders:
   - `data_sts2_windows_x86_64`
   - `controller_config`
3. **Dedicated Isolated Directories**:
   - `userdata/` (stores profile, saves, logs, port discovery token)
   - `local_userdata/`
   - `mods/` (contains `sts2-full-app-bridge.dll` and `sts2-full-app-bridge.json`)
4. **Isolated Environment Variables**:
   - `APPDATA=<sandbox_dir>\userdata`
   - `LOCALAPPDATA=<sandbox_dir>\local_userdata`
   - `STS2_FULL_APP_BRIDGE_PORT=<dynamic_or_fixed_port>`
   - `STS2_FULL_APP_BRIDGE_PORT_FILE=<sandbox_dir>\userdata\bridge_port.txt`
5. **CLI Flags**:
   `--headless --force-steam=off`

---

## 4. Bridge Protocol Specification

The bridge exposes a line-delimited JSON-RPC TCP protocol over UTF-8 without BOM.

### Supported RPC Methods
1. `hello`: Returns worker metadata, PID, Godot version, bridge mod version, and isolation confirmation.
2. `start_run(seed, character, ascension)`: Starts a run with canonical seed, character, and ascension, advancing the game state to the first decision boundary.
3. `observe()`: Returns the canonical `ObservationDto` snapshot (combat state, player HP, energy, hand, draw/discard/exhaust piles, enemy intents, modifiers, state hash).
4. `legal_actions()`: Returns the exact list of `LegalActionDto` objects available at the current decision boundary.
5. `step(action_id)`: Enqueues and executes the specified action (`play_card:<idx>:target:<combat_id>`, `use_potion:<idx>:target:<combat_id>`, `end_turn`, `choose_node:<idx>`, `choose_rest:<id>`), waits for action execution to finish, and returns the next observation.
6. `history()`: Returns the trace of executed action IDs and intermediate SHA-256 state hashes.
7. `close()`: Cleanly shuts down the remote game process.

---

## 5. Presentation Suppression

To maximize throughput and prevent headless Godot scene-graph null-pointer exceptions, the bridge applies presentation-only Harmony patches:
- `MegaCrit.Sts2.Core.Commands.VfxCmd.*`: Bypasses 2D combat visual effects and particles.
- `MegaCrit.Sts2.Core.Audio.Debug.NDebugAudioManager.*`: Bypasses audio clip playback and bus routing.
- `MegaCrit.Sts2.Core.Commands.SfxCmd.*`: Bypasses sound effect commands.
- `MegaCrit.Sts2.Core.Commands.ThinkCmd.*`: Bypasses speech bubble UI delays.
- `MegaCrit.Sts2.Core.Commands.CardPileCmd.*`: Bypasses card pile tween animations.
- `MegaCrit.Sts2.Core.Nodes.Screens.Map.NNormalMapPoint.SetAngle`: Bypasses 2D map node icon rotation.
- `MegaCrit.Sts2.Core.Commands.Builders.AttackCommand`: Clears custom visual VFX node generators while preserving 100% authentic damage calculations and history hooks.
- `NonInteractiveMode.AutoSlayerCheck = () => true`: Ensures game loop runs at maximum non-interactive compute speed.

---

## 6. Authority & Environment Hierarchy

The project defines three operational environments with strict authority boundaries:

| Environment | Binary / Host | Lifecycle Control | State Reconstruction | Authority | Usage |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`full_application_native`** | Shipped `SlayTheSpire2.exe` | Shipped Engine Run Loop | **None (0%)** | **Primary Gold Authority** | Trajectory generation, policy evaluation, rollout verification |
| **`reconstructed_native`** | `Sts2.NativeSim.GodotHost` | In-process Direct Invocation | Synthetic Combat Reset | **Diagnostic / Accelerator** | Fast local unit tests, micro-benchmarks, state diffing |
| **`python_approximate`** | `sts2_headless_gym.py` | Python Script | Handcrafted Simulator | **Approximate (Legacy)** | Rapid prototyping only; frozen from production data |
