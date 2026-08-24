# Shipped-game trace exporter

`Sts2.NativeSim.TraceExporter` is an opt-in, read-only instrumentation mod. It subscribes to native combat/action lifecycle events and writes JSONL checkpoints; it does not enqueue actions, synthesize input, alter models, or control the desktop. It is inert unless explicitly enabled.

## Package

Build with:

```powershell
dotnet build .\src\Sts2.NativeSim.TraceExporter\Sts2.NativeSim.TraceExporter.csproj -c Release
```

The ready-to-copy package is under `src/Sts2.NativeSim.TraceExporter/bin/Release/net9.0/package`. Installation is intentionally manual: copy that directory beneath the game's `mods` directory only when you choose to run a capture, and remove it afterward. The repository tooling never modifies the installed game or its mod settings.

Enable capture using either `STS2_NATIVE_TRACE=1` in the game process environment or the explicit `--native-sim-trace` launch argument. Normal mod loading still requires the game's own mod-consent flow.

Traces are written under Godot's user-data directory in `native_sim_traces`, never into the game installation. Each combat gets a separate timestamped JSONL file.

## Current coverage and safety gates

The exporter currently supports single-player combat actions that complete without a nested blocking choice: card play, potion use/discard, and complete end-turn cycles. It records schema/build identity, stable deck-instance identity, realized hand and draw-pile order, realized enemy model/HP/opening move history, native card net IDs/types/targets/costs, card upgrades/enchantments/native scalar saved state, relic counters/native scalar saved state, potion slots, post-setup RNG counters, piles, resources, creature HP/block/alive state, monster moves/intents/powers, native-derived legal actions/decision state, and terminal flags.

If a native action pauses for a blocking player choice, or reset serialization encounters an unsupported complex saved-property type, the exporter writes or logs an explicit unsupported condition and closes that trace. It never guesses.

Supported exporter traces declare `comparison: "exact"`. The complete canonical observation passes exact object-key/value equality in the engine-hosted reset/card-action smoke, including its legal-action boundary. That smoke is explicitly tagged `source: "simulator_self_smoke"` and can never certify. A shipped capture is tagged `source: "shipped_game"`; it becomes certifying only if strict replay succeeds. As of 2026-08-23, the pinned campaign contains eight repeatable complete-victory traces and 177 exact checkpoints across Ironclad, Necrobinder/Osty, Nibbits, Slimes, and Shrinker Beetle. This is evidence only for those observed transitions; `global_certification` remains false.

Replay an exported trace with:

```powershell
python .\python\differential_replay.py <trace.jsonl>
```
