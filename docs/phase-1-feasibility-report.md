# Phase 1 native-model feasibility report

Status: **historical Phase 1 result; superseded by the successful non-certifying Phase 1b engine-hosted action probe**

Phase 1b now executes native Strike successfully inside an isolated official Godot 4.5.1 .NET host. See `phase-1b-headless-godot-report.md`. The plain-console findings below remain valid and explain why an engine-hosted process is required.

Target inspected on 2026-08-21:

- Assembly: `<STS2_GAME_ROOT>\data_sts2_windows_x86_64\sts2.dll`
- SHA-256: `A1F9E653F1E28E4076558FEE1E60D218619CB7E057B887C6417F62C62C6D7A52`
- Product version: `0.1.0+59260271157f76a2896f0eab5bc6ea1245d8b314`
- Assembly size: 9,364,480 bytes
- Types loaded: 9,409

## Answers to the Phase 1 questions

1. **Can the mechanical model layer run outside the full game? Partly.** `ModelDb.Init()` succeeds in a .NET 9 console process after marking the empty mod registry as skipped. It exposes 578 cards, 80 encounters, and 297 relics. Native `Rng`, `RunRngSet`, `RunState`, `Player`, `CombatState`, encounter generation, creature construction, combat piles, and mutable card instances all ran without launching Steam or reading a profile.

2. **What minimum Godot initialization is required?** None for the preceding model/state construction. A valid Godot engine context is required before the installed logging path can be touched: `Logger` asks `Godot.OS.GetCmdlineArgs()`. A full Strike effect additionally enters `CreatureCmd.TriggerAnim`; absent visuals cause `Log.Error`, which reaches that Godot call. `NetCombatCardDb.StartCombat` also logs and has the same boundary. The spike has not yet proven whether engine-core bootstrap alone is sufficient after that point, because later action code may require combat node singletons. It would be incorrect to claim “no Godot required” or to claim a complete minimum scene tree yet.

3. **Can an authentic native combat state be constructed? Yes, at the model level.** The probe constructed an Ironclad test run, selected the native `BygoneEffigyElite` encounter, generated one native enemy creature, populated native combat piles, and created a mutable native `StrikeIronclad` owned by the player. This is not yet a fully started `CombatManager`/scene combat.

4. **Can a native card action execute deterministically? No, not in the plain console host.** The unsafe subprobe reached `StrikeIronclad.OnPlay -> AttackCommand.Execute -> CreatureCmd.TriggerAnim -> Log.Error -> Logger.GetIsRunningFromGodotEditor -> Godot.OS.GetCmdlineArgs` and the process terminated with native status `0xC0000005`. The production probe therefore fails this gate loudly and does not approximate damage.

5. **Can the result be serialized and cloned? Partly.** Native `RunRngSet.ToSerializable`/`FromSave` succeeded; the next 64 values from the shuffle stream matched exactly after restore. The standalone native `Rng` clone also matched the next 32 values at counter 8. A complete combat-state serializer/clone was not found or proven, and no after-Strike result exists to serialize.

6. **What throughput does the minimal transition achieve? Unknown for a card transition.** A diagnostic benchmark of 100,000 reflected native RNG calls measured 6.60 million calls/second in one Release run (checksum `107439598597829`). This is not a combat-action benchmark and must not be compared to the 10,000 atomic combat transitions/second target.

7. **Is native rehosting viable? Conditionally promising; replacement generation is not justified yet.** Most model/state construction rehosts cleanly and should be retained. The next spike must host the assembly inside the smallest valid headless Godot engine context and replace or satisfy only the node/singleton dependencies proven by traces. Do not start a replacement simulator unless that spike proves the action layer cannot be isolated.

## Exact dependency inventory observed

| Component | Plain .NET 9 | First relevant dependency |
|---|---:|---|
| `ModelDb.Init` | Pass | Requires `ModManager.State != None`; empty registry can be `Skipped` |
| `Rng` / `RunRngSet` | Pass | None observed |
| `Player.CreateForNewRun` | Pass with test isolation | Reads `SaveManager.Instance.Progress`; probe injects default in-memory test state |
| `RunState.CreateForTest` | Pass | Native player, acts, RNG, odds, relic bags |
| `CombatState` + encounter creatures | Pass | `CombatManager.Instance.StateTracker` for combat piles |
| `NetCombatCardDb.StartCombat` | Native crash outside engine | `Log -> Logger -> Godot.OS` |
| `StrikeIronclad.OnPlay` | Native crash outside engine | `AttackCommand -> CreatureCmd.TriggerAnim -> Log.Error -> Godot.OS` |
| Full `PlayCardAction` | Not safely reached | Also references `NCardPlayQueue.Instance` and `GameAction` static logger |

## Safety isolation

The probe does not start Steam or the game executable. It does not enumerate, read, or write real save/profile/cloud directories. `SaveManager.MockInstanceForTesting` receives an uninitialized manager with only a default in-memory `ProgressState`, sufficient for the native player constructor's ascension-stat lookup.

## Decision

Keep the project **non-certifying**. Proceed only to a minimal headless-Godot feasibility extension of Phase 1. Do not train on simulator output and do not begin the replacement engine.
