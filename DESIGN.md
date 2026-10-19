# relay — design document

Processing hub for the church live-translation captions system.
Last updated: 2026-10-19

## Index

1. [What relay is](#1-what-relay-is)
2. [Context & fixed decisions](#2-context--fixed-decisions)
3. [Architecture](#3-architecture)
4. [Modules & responsibilities](#4-modules--responsibilities)
   - [4.1 capture](#41-capture--audio-intake)
   - [4.2 gladia](#42-gladia--speech-provider-client)
   - [4.3 demand](#43-demand--gatekeeper)
   - [4.4 hub](#44-hub--fan-out--history)
   - [4.5 serve](#45-serve--phone-facing-api)
   - [4.6 operator](#46-operator--human-console-tui)
   - [4.7 supervise](#47-supervise--keep-alive-cross-cutting)
   - [4.8 config](#48-config--bootstrap-cross-cutting)
5. [Data flow (channel contracts)](#5-data-flow-channel-contracts)
6. [Failure handling (risk register)](#6-failure-handling-risk-register)
7. [Startup sequence](#7-startup-sequence)
8. [Language demand model](#8-language-demand-model)
9. [Build order](#9-build-order-each-step-demoable-alone)
10. [MVP cut list](#10-mvp-cut-list)
11. [Pre-build bake-off](#11-pre-build-bake-off-before-writing-gladia-for-real)
12. [Glossary](#12-glossary)

Actionable checklists live in [TODO.md](./TODO.md).

## 1. What relay is

A single Rust binary running on a dedicated Mac Mini (M1, 8GB) wired to the
church sound mixer's line-out. It:

1. captures the service audio locally,
2. streams it to **Gladia** (EU speech-to-text + translation provider) *only
   while at least one listener is connected*,
3. fans the translated captions out to phones over WebSocket, grouped by
   language,
4. gives the AV operator a terminal dashboard and simple controls.

**Design principle:** every failure degrades to an *honest visible state* on the
phone ("captions paused — reconnecting…", "captions start from when you join") —
never a silent wrong one (frozen captions that look live).

**Out of scope for relay** (separate piece of software): the phone web app —
served by its own web server (part of that project's deployment). relay
exposes only the WebSocket API it speaks; the app points at relay's WS URL.

## 2. Context & fixed decisions

The pipeline, in order:

1. **Audio source** — mixer main-out → audio interface → Mac Mini (M1, 8GB),
   16 kHz / 16-bit / mono.
2. **Provider** — Gladia (Paris, EU). Live transcription uses `solaria-1`,
   the only live-capable model (`solaria-3` is pre-recorded-only).
   Translation add-on: flat €0.05/h **regardless of language count**.
3. **Demand gating** — no listeners → no session → no audio sent → no cost.
4. **Cost** — €0.68/h (live STT) + €0.05/h (translation) = **€0.73/h while
   streaming**; at ~5 h/month ≈ €3.65; €50 one-time free credits.
5. **Transport** — WebSocket push to phones; language choice is the first WS
   message; no separate REST endpoint. MVP connectivity: church WiFi only.
6. **Latency** — 1–3 s speaker → caption; chunk size dominates (tune
   100–200 ms).
7. **Constraints** — Gladia live session max 3 h → proactive rollover at
   ~2h45m; free tier allows 1 live session (we need exactly one).
8. **Privacy** — opt out of model training at signup; API key never leaves
   the server; EU provider chosen deliberately (sermon audio leaves the
   building).

Gladia handshake (verified against docs): `POST https://api.gladia.io/v2/live`
with full config → returns one-time WS URL + session id → connect → stream
PCM chunks → receive transcript + translation events. Target languages are
**fixed at session start**; changing the set = new init + reconnect (~1 s
blip). **No billing API exists** — relay self-tracks accrued cost from
stream uptime.

## 3. Architecture

Data flows in one direction, stage to stage:

1. `capture` pulls chunks from the mixer and emits them downstream.
2. The demand gate passes chunks through **only while ≥1 phone is
   connected**; otherwise chunks are dropped.
3. `gladia` uploads gated chunks and emits caption events as they return.
4. `hub` routes each caption into its language bucket (broadcast + history).
5. `serve` streams bucket content to phones; join/leave events flow back to
   the demand tracker, closing the loop.
6. `operator` and `supervise` observe everything; neither sits in the data path.

Wiring rule: **channels are the contracts.** No module imports another
module's internals; data crosses module boundaries only via channels.
One OS process; concurrency = tokio tasks (capture may use a dedicated
thread for `cpal` callbacks).

## 4. Modules & responsibilities

### 4.1 `capture` — audio intake
- Enumerate input devices; startup TUI picker with **live level meter**
  (human verifies right device + healthy signal in one glance).
- Open stream, chop into fixed-size chunks (default 100 ms ≈ 3.2 KB @16kHz/16-bit/mono).
- **Silence watchdog:** in Sermon state, no speech-level signal for >2 min → alert operator.
- Emits: chunk stream (bounded mpsc, capacity ~50; overflow drops oldest).
- Knows nothing about Gladia or phones.

### 4.2 `gladia` — speech provider client
- `POST https://api.gladia.io/v2/live` init: `model: "solaria-1"` (the only
  live-capable model — `solaria-3` is pre-recorded-only), wav/pcm
  16k/16-bit/mono, `translation_config { target_languages, model: enhanced,
  context: sermon context string, match_original_utterances: true }`,
  `receive_realtime_processing_events: true`.
- WS lifecycle: connect, upload gated chunks, receive typed events
  (partials/finals/translations/errors).
- Reconnect using session id; **proactive rollover at ~2h45m** (3 h cap).
- Exponential backoff on errors; honors retry headers; reports degraded state.
- Provider interaction hides behind a `Transcriber` trait so the rest of the
  codebase never imports Gladia types.

### 4.3 `demand` — gatekeeper
- Refcount per language from phone join/leave.
- **Hysteresis:** a language stays "active" 60 s after its last listener —
  prevents session-reconnect storms from flaky phones.
- Decides: start session (0 listeners → ≥1), reshape targets (new language
  outside current set → debounced reconnect), stop session (global refcount 0
  → pause audio upload entirely → **cost accrual stops**).
- Emits: commands to `gladia`; state snapshots to `operator`.

### 4.4 `hub` — fan-out & history
- `HashMap<LangCode, Bucket>`:

  ```
  Bucket {
      tx: broadcast::Sender<Caption>,   // live fan-out
      history: VecDeque<Caption>,       // ring buffer, last ~50
  }
  Caption { lang, text, timestamp(server), is_final, seq }
  ```

- Buckets (and their history) exist **only while a language has demand**;
  hysteresis grace period, then drop entirely.
- Late join / reconnect = replay `history` snapshot, then subscribe to `tx`.
- Lagged subscriber (`broadcast::RecvError::Lagged`) → resync from history or
  disconnect. One slow phone never stalls the rest.

### 4.5 `serve` — phone-facing API
- `axum`: owns the phone WS protocol. No static file serving — the web app is
  deployed by its own separate web server:
  - inbound: `{ "lang": "es" }` as first message; pings.
  - outbound: `Caption` messages + honest status messages
    (`reconnecting`, `paused`, `session_ended`, `unsupported_language[...]`).
- Validates requested language against the supported set **before** touching
  any session; invalid → friendly list back, never a reconnect.
- Graceful shutdown: announce `session_ended` to all phones.
- API key never crosses this boundary.

### 4.6 `operator` — human console (TUI)
- Startup wizard: device pick + level meter + sound-check prompt.
- Live dashboard: connections per language, service state, session status,
  accrued-cost estimate (stream uptime × €0.73/h), watchdog alerts.
- Keys: service state (1 sermon / 2 singing / 3 reading / 4 other), pause.
- **Service-state semantics:** Sermon = live captions; Singing/Reading = relay
  does *not* caption (web app shows pre-loaded text in those states — its
  concern, driven by state messages relay publishes).

### 4.7 `supervise` — keep-alive (cross-cutting)
- Supervisor wrapper on every task: catch panic → log loudly → restart with backoff.
- Heartbeat registry: each task bumps a timestamp; one watchdog task flags
  stuck ones (doubles as "pipeline secretly dead" detector alongside capture's
  silence watchdog).
- Philosophy: panics are bugs, not weather — restart + alert, no heroic recovery.

### 4.8 `config` — bootstrap (cross-cutting)
- File/env: API key, device name, default language list, chunk size, ports.
- Startup validation, **fail fast** with plain-English errors. Never limp into a Sunday.

## 5. Data flow (channel contracts)

```
capture        --mpsc(chunks, cap 50)-->   gate / gladia
demand         --mpsc(commands)-->          gladia
gladia         --(routed)-->                hub.buckets
hub.broadcast  --per-language-->            serve (one task per phone socket)
serve.events   --mpsc-->                    demand (joins/leaves)
everything     --heartbeats/status-->       supervise / operator
```

No unbounded queues anywhere. The only fixed-rate producer is capture, and its
consumer (Gladia) drains at the same rate; excess on network hiccup is dropped,
never queued into minutes of lag.

## 6. Failure handling (risk register)

**Audio chain**
| Risk | Detect | Response |
|---|---|---|
| Wrong input device (macOS default-mic trap) | config vs `cpal` device list at startup | Refuse to start, clear error |
| Mixer muted / cable out | Silence watchdog (>2 min, Sermon state) | Terminal alert "NO AUDIO — check mixer" |
| Bad gain | Startup level meter + sound-check step | Human procedure |

**Gladia / network**
| Risk | Detect | Response |
|---|---|---|
| Internet blip | WS close/error | Buffer briefly + resume via session id; long outage → phones see `reconnecting`, exponential backoff |
| 429 / 5xx | HTTP status | Honor retry headers, bounded backoff, degraded state shown |
| 3 h session cap | Session clock | Proactive rollover ~2h45m; phones see sub-second pause |
| Language-set change needed | demand tracker | Debounced reconnect (see hysteresis) |
| Phone flap churn | Bucket transitions | 60 s hysteresis |

**Server internals**
| Risk | Detect | Response |
|---|---|---|
| Task panic (silent death) | Supervisor wrapper | Log loudly, restart with backoff |
| Stuck task | Heartbeat registry | Alert + restart |
| Dead sockets accumulate | Ping/pong + write timeouts | Reap |
| Slow/lagged client | broadcast Lagged | Resync from history or disconnect |
| Unbounded memory | — | Ring buffers + bounded channels by construction |
| Bad API key / config | Startup | Fail fast, human-readable |
| Ctrl-C | Shutdown handler | Close session, notify phones `session_ended`, flush logs |

**Human / organizational**
| Risk | Mitigation |
|---|---|
| Operator forgets state switch (singing gets "transcribed") | Startup checklist; phase 2: mixer OSC auto-detect |
| Late joiner has no back-history (demand gating) | Accepted; stated on the web page |
| API key leak | Key server-side only; web app never sees it |

## 7. Startup sequence

1. Load + validate config (fail fast).
2. TUI wizard: pick device, confirm level meter while someone speaks.
3. Bind HTTP/WS server; phones can now connect (no captions yet).
4. Idle: **no Gladia session, zero cost.**
5. First phone picks a language → demand opens session with default target
   list (see §8) → captions flow to that bucket.
6. Service runs; operator flips state keys; watchdogs watch.
7. Post-service: Ctrl-C → clean close, cost summary in terminal + log.

## 8. Language demand model

- **Default target list:** every session is opened with ~10 pre-selected
  likely languages baked in. Translation is flat-rate, so extra idle targets
  cost nothing and eliminate reconnect blips for the common case.
- Reconnect (session reshape) happens **only** when a request needs a language
  outside the current set; debounced by hysteresis.
- Buckets without demand hold no history and cost nothing.

## 9. Build order (each step demoable alone)

1. `config` — loads, validates, errors well.
2. `capture` — device picker + meter in a terminal; chunks counted.
3. `gladia` — print transcripts/translations for the mixer input in a terminal
   (bake-off harness doubles as this step's test).
4. `hub` — unit tests: refcounts, hysteresis, replay snapshots.
5. `serve` + minimal web page — **captions on a real phone (the money moment).**
6. `operator` — dashboard + state keys.
7. `supervise` — wrappers, heartbeats, chaos-testing (kill tasks on purpose).

## 10. MVP cut list

**In MVP:** everything above, church-WiFi only, default language list,
terminal operator UI, self-computed billing estimate. Single provider, no
fallback plumbing.

## 11. Pre-build bake-off (before writing `gladia` for real)

Purpose: prove caption quality and the Gladia integration behaviors *before*
building on them. The full checklist lives in [TODO.md](./TODO.md).

Decision rule: quality passes → build on Gladia at €0.73/h. Fails → stop and
re-evaluate the provider choice **before launch**; relay ships with a single
provider and no fallback plumbing.

## 12. Glossary

- **Bucket** — per-language fan-out unit: broadcast channel + history ring buffer.
- **Demand** — ≥1 connected phone requesting a given language.
- **Hysteresis** — 60 s grace before tearing down a language's session/bucket
  after its last listener leaves.
- **Honest state** — a client-visible status that is never silently wrong.
- **Rollover** — proactive session restart before Gladia's 3 h cap.
