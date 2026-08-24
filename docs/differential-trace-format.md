# Differential trace format

The certification harness consumes newline-delimited JSON exported separately from the shipped game. It does not launch, inspect, or control the visible game.

Record zero is a header:

```json
{"type":"header","format_version":1,"source":"shipped_game","game_build":{"version":"...","assembly_sha256":"...","pck_sha256":"..."},"reset":{}}
```

The remaining records are ordered checkpoints. Sequence zero is the observation corresponding to reset and has a null action. Every later checkpoint names the stable legal action taken from the preceding observation and contains the complete canonical observation afterward:

```json
{"type":"checkpoint","sequence":0,"action_id":null,"observation":{},"state_hash":"..."}
{"type":"checkpoint","sequence":1,"action_id":"play:strike-0:target:1","observation":{},"state_hash":"..."}
```

Run `python python/differential_replay.py path/to/trace.jsonl`. The default `comparison: "exact"` pins every supplied build-fingerprint field, requires exact object keys, array order, values, and optional state hashes, and stops at the first JSON path that differs. No fields are silently ignored.

Use `--require-exact` to audit a declared subset trace with exact object-key equality. This is a validation aid only: it reports the declared and effective validation modes separately and never upgrades a subset trace to certifying status.

Early exporter traces may declare `comparison: "subset"` and list their explicit projection in the header. The comparator then permits additional keys in actual objects but still requires every projected key, value, and array element to match. Subset traces are always non-certifying. A passing trace is marked `certifying: true` only for `source: "shipped_game"` plus `comparison: "exact"`; simulator-generated traces may test the harness but cannot add differential coverage.

The environment remains non-certifying until real shipped-game traces cover the required mechanic matrix and the corresponding exact-match counts are recorded in `docs/mechanic-coverage.csv`.

Run `python python/trace_inventory.py <trace.jsonl> [...]` to summarize already-captured cards, actions, turns, enemies, moves, powers, relics, potions, and terminal coverage without launching a worker. This inventory does not itself certify a trace; it prevents existing evidence from being overlooked when selecting subsequent captures.

Run `python python/differential_campaign.py --output artifacts/differential-campaign-report.json` to discover both standard trace directories, hash and strictly replay every available trace, and aggregate the exact certifying scope. The campaign deliberately reports `global_certification: false`; adding traces expands only the enumerated evidence and never silently promotes unobserved mechanics.
