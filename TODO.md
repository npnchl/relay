# relay — TODO

Actionable checklists. Design rationale lives in [DESIGN.md](./DESIGN.md).

## Bake-off (pre-build — blocks the `gladia` module)

- [ ] Gladia account created; **model-training opt-out set**.
- [ ] Record one sermon (~90 min) as the test asset.
- [ ] Process the recording offline with `solaria-3` (pre-recorded-only,
      EU-optimized — best-case quality) **and** via live `solaria-1`, to
      measure the live-model quality gap.
- [ ] Run translation A/B: `base` vs `enhanced` + sermon `context` string,
      per target language.
- [ ] Fluent church member grades faithfulness (theological terms, names,
      nuance).
- [ ] Verify: reconnect behavior after network drop; ~2h45m rollover; real
      end-to-end latency; `match_original_utterances` caption alignment.
- [ ] Confirm supported-language list covers the church's actual needs.
- [ ] Check Gladia data-retention settings for sermon audio.

**Decision rule:** quality passes → build on Gladia at €0.73/h. Fails → stop
and re-evaluate the provider choice **before launch**; relay ships with a
single provider and no fallback plumbing.

## Build order

- [ ] 1. `config` — loads, validates, errors well.
- [ ] 2. `capture` — device picker + meter in a terminal; chunks counted.
- [ ] 3. `gladia` — transcripts/translations printed in a terminal (bake-off
      harness doubles as this step's test). *Blocked by bake-off.*
- [ ] 4. `hub` — unit tests: refcounts, hysteresis, replay snapshots.
- [ ] 5. `serve` + minimal web page — captions on a real phone (the money moment).
- [ ] 6. `operator` — dashboard + state keys.
- [ ] 7. `supervise` — wrappers, heartbeats, chaos-testing (kill tasks on purpose).
