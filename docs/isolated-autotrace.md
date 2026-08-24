# Isolated shipped-runtime AutoTrace

`Sts2.NativeSim.AutoTraceDriver` is a separate, opt-in, gameplay-affecting validation mod. It uses the shipped `AutoSlayer` only to create and route a run, then replaces combat action selection while the read-only exporter records the shipped runtime. The deterministic `coverage` policy selects among native-playable cards and uses native potions; `basics` restricts selection to ordinary Strike/Defend cards. It is not part of the simulator or live advisor.

The driver refuses to initialize unless all of these conditions hold:

- Godot uses the headless display driver;
- `--force-steam=off` disables Steam initialization and cloud synchronization;
- `--native-sim-trace` enables the independent exporter;
- `STS2_NATIVE_AUTOTRACE_ROOT` is defined and `OS.GetUserDataDir()` is beneath it.

The launcher creates a deterministic seed/count/policy child sandbox, hardlinks immutable root game files, junctions the controller and managed-data directories read-only-by-convention, and creates its own `mods` and user-data directories. It never writes to the installed game's `mods` directory or real STS2 user-data directory. It copies only the settings schema into the sandbox, enables the two validation mods there, and does not copy profiles, progress, run saves, history, or account data. A completed capture can be resumed for replay/promotion; a mismatched manifest is rejected rather than overwritten or deleted.

The shipped AutoSlayer combat handler injects 999 Plating and Regen and uses an autoplay path that bypasses normal queued player actions. The driver therefore replaces that handler with a bounded policy using `CardModel.TryManualPlay`, native potion enqueue, the shipped action executor, and the shipped `EndPlayerTurnAction`. Card and target order are stable and do not consume the AutoSlayer scheduling RNG. This changes only which legal action is selected. Card/potion effects, targeting validation, enemy AI, powers, damage, RNG, relic hooks, and turn mechanics remain shipped implementations.

Build-only validation is safe and does not install or launch anything:

```powershell
dotnet build .\src\Sts2.NativeSim.AutoTraceDriver\Sts2.NativeSim.AutoTraceDriver.csproj -c Release
```

The launcher is intentionally not invoked automatically during ordinary tests:

```powershell
.\scripts\run-isolated-autotrace.ps1 -Seed A1B2C3D4E5
```

Use `-CombatCount N` to keep the isolated native run alive through `N` consecutive combats. The exporter writes one independent trace per combat and the launcher requires exactly `N` traces, exact-replays each one, and promotes them individually. Non-combat routing and rewards between fights remain the shipped AutoSlayer's test policy; they are not exported as certification evidence.

The launcher retains every result under `artifacts/shipped-autotraces/candidates`, runs strict exact replay, and copies only a passing trace into `artifacts/shipped-autotraces/certified`. Failed candidates remain useful regression cases but are not certification evidence. A driver launch by itself is never evidence.

Pet relic reconstruction invokes each shipped pet relic's native `BeforeCombatStart` lifecycle after enemies are registered. This preserves native combat-ID ordering and delegates companion creation, stats, powers, hooks, and later revival/growth behavior to the game. The first Necrobinder/Osty automated trace passes exact replay through victory; other pet relics and companion-producing effects remain unclaimed until separately observed and replayed.
