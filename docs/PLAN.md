# OcheCore project plan

Date: October 1, 2026. Original brief: `.idea`.

## Goal and boundaries

Receive AutoDarts events once, track match state, normalize events and trigger independent
audio and automation handlers. Use asynchronous Python, uv and Docker. Account board discovery,
board selection and the cloud connection have been checked against an installed board.

```text
AutoDarts cloud ── OAuth / REST / WebSocket ── OcheCore runtime ── events / integrations
                                                   │
                                              Control API
                                               /       \
                                              CLI    Optional UI
```

Keep a single source tree under `src/ochecore/`, as shown in the [README](../README.md).
`main.py` assembles the application, with HTTP routes in `http.py` and live event streaming
in `websocket.py`. `events.py` groups event handling, and
`autodarts/` contains connection code and match tracking. Add modules only when functionality
needs them. Startup settings remain in `config/config.yaml`, with environment overrides.

Cloud is the event source. The legacy local HTTP adapter is removed. The core owns all
connection and event state; the CLI and optional minimal UI use its control API. The UI
can be disabled without changing core behavior. Future integrations are documented below
without placeholder source files.

## Phase 1 — Headless connection and controls

Implemented:

- [x] Python package, uv lockfile, validated YAML/environment/API settings.
- [x] Non-root Docker image, persistent data bind mount and healthcheck.
- [x] Terminal commands: serve, login, logout, config, status and events.
- [x] Account board discovery through `GET /api/boards`, `ochecore boards` and a simple UI selector.
- [x] Optional minimal UI for connection settings, status and recent events.
- [x] API-only mode with `serve --no-ui` or `ui_enabled: false`.
- [x] Device authorization, approval polling, expiration and slowdown handling.
- [x] Refresh-token rotation with atomic storage.
- [x] User identity, board lookup, active-match subscription and REST snapshot.
- [x] Cloud board/user events, match state, reconnection and periodic reconciliation.
- [x] Messaging-gateway ticket handshake, verified against AutoGlow-2 and AutoDarts Play.
- [x] Match selection and latest raw state in `autodarts/cloud.py`.
- [x] Remove legacy local polling; tolerate old saved local settings during upgrades.
- [x] Timestamped envelopes, bounded history and live WebSocket delivery.
- [x] Simulated connection tests, including a dropped WebSocket.
- [x] Live account board discovery, selection, cloud connection and initial match events.
- [ ] Full-match acceptance, recovery and authentication with OcheCore's own OAuth Client ID.

Board discovery follows AutoGlow-2's `GET /bs/v0/boards` reference. It uses the saved account
session and leaves selection explicit. The same configuration API persists CLI/UI selections.

Real acceptance: approve login, connect the cloud source, receive match state, switch matches,
recover after an Internet outage, run beyond 15 minutes, and restart without another approval
while the refresh token is valid. Repeat control operations with the UI disabled.

## Phase 2 — Match interpretation and normalized events

Implemented against supplied reference contracts:

- [x] Separate normalized and raw HTTP/WebSocket streams, with CLI access to both.
- [x] Board `state` and match `events` subscriptions in addition to the existing topics.
- [x] Typed match frames and normalized throw, correction, removal, turn, bust and win events.
- [x] Local, guest, remote and bot attribution using the player's Board ID.
- [x] Silent snapshots, repeated-state suppression and bounded deduplication after reconnects.
- [x] Reference-based X01 replay tests for takeout, undo, corrections and leg transitions.
- [ ] Live gameplay acceptance for X01, training variants, remote opponents and shared boards.

Cloud scoring remains authoritative. Normalization stays in the headless core. See
[event contracts and source comparison](EVENTS.md) for coverage and limits.

Acceptance: one normalized event per throw in covered scenarios, no extra throw on correction,
and correct local/remote player attribution.

## Phase 3 — Rules, WLED and caller

First WLED implementation:

- [x] Persisted controller/target settings, CLI/API controls and optional WLED UI.
- [x] Independent controller workers, bounded queues, timeouts and reconnect handling.
- [x] Segment colours/native effects and individual LED/range colours.
- [x] Current game API, readiness reasons, configurable phase colours and timed previews.
- [x] Local triple, bull, 180, bust and win effects with priority and cancellation.
- [x] Numeric matrix scores, orientation/serpentine layout, correction and snapshot updates.
- [x] Mocked device, headless API and UI form validation.
- [x] Local mDNS discovery with API verification, CLI access and an explicit Add action in the UI.
- [ ] Live phase/readiness and physical matrix acceptance with the installed hardware.
- [ ] Native whole-device preset actions, matrix player names and scrolling messages.
- [ ] Caller integration and shared profiles/rules if needed across integrations.

The implemented settings are documented in the [README](../README.md#wled-lighting-and-matrix-scores).
The broader scope and remaining delivery goals follow.

1. Independent bounded integration queues, timeouts and failure isolation.
2. Validated rules: target, phase/event, filters and actions. Persist integration settings under
   `data/`; CLI and the optional UI read and edit them through the service API.
3. Implement `wled.py`: device/segment/pixel controls, phase lighting and a matrix scoreboard
   as specified below, with multiple devices and rate limits.
4. Implement `caller.py`: user-provided sounds, then player names and checkout announcements.
   Choose browser or host audio based on deployment needs.
5. Initial rules: triple, bull, 180, bust and victory; define priority and cancellation.

Acceptance: a WLED outage cannot block audio or incoming events. Rules can be tested without
a match. Replayed events do not trigger actions by default.

### WLED scope

Requirements updated October 2, 2026: independent LED presets, colours for each game phase,
and scores on an LED matrix belong in the WLED integration.

**Targets and saved appearances**

- Name controllers and their lighting targets: a strip, WLED segment, LED range, individual
  addressable LED, or matrix region. Validate the device capabilities, bounds and overlapping
  targets before applying settings.
- Save colours, brightness and effects independently for each target. For example, the
  surround can show the game phase while the matrix shows the score and board illumination
  stays steady. Treat whole-device WLED presets as explicit actions; use segment/pixel
  controls for target-level settings. Native presets may contain changes to several segments.
- Account for segment ownership when using individual pixel control: it can suspend the
  native effect on that segment. Compose updates for each owned target and restore its
  current phase when a temporary effect ends.

**Persistent game phases**

| Phase | Default appearance |
|---|---|
| Ready to throw on the selected board | Green |
| Remove darts / takeout in progress | Yellow |
| Wait / do not throw | Red |
| No active match | Configurable idle appearance |

Colours are configurable per target. These are persistent states, with temporary throw,
bull, 180, bust and victory effects layered according to priority and duration. When an effect
ends, restore the current phase, which may have changed while the effect was playing.

The headless core must expose the interpreted game phase and current match state to all
clients and integrations. Derive phases from verified cloud state and takeout/turn signals.
A scored throw or an online board alone does not prove the detector is ready. Validate the
cloud signals for readiness, calibration and pauses; keep unsupported or stale readiness
explicitly unknown and use the waiting appearance. No legacy local-board adapter is required
or implied. If the cloud connection is lost while WLED remains reachable, clear the ready
indication. A disconnected WLED controller cannot receive a fallback command.

**Matrix scoreboard**

- Show remaining score, visit total, last dart and active player, with brief messages such
  as `180`, `BUST`, `WIN`, `REMOVE DARTS` and `WAIT`.
- Read scores from the core's interpreted AutoDarts state. Corrections, undo, player changes
  and reconnection snapshots must update the display without replaying celebrations.
- Configure matrix dimensions, orientation and pixel layout, including serpentine wiring.
  Discover supported WLED effects: use native text where suitable and pixel rendering for
  a fixed scoreboard layout. Hardware and firmware support must be checked on the controller.
- Keep matrix updates scoped to their target so score changes do not overwrite phase lights.

**Controls and delivery order**

Keep the WLED page to Devices, Phase colours, Event effects and Matrix display. Provide
equivalent CLI/API controls, saved settings, per-target enable/disable, and tests that can
preview a phase or sample score without a real match.

1. Implement controller connection, capability discovery, targets and manual colour tests.
2. Add the core phase/current-match view, configurable phase colours and transition tests.
3. Add temporary event effects, priorities, cancellation and return to the current phase.
4. Add matrix layout, live scores and correction/reconnection tests.

Acceptance includes two independent targets on one controller, per-LED selection, green /
yellow / red transitions, phase changes during a celebration, stale-state handling, matrix
score correction and continued headless operation. Confirm the complete flow on real hardware.

References: [WLED segments](https://kno.wled.ge/features/segments/),
[JSON API and individual pixel control](https://kno.wled.ge/interfaces/json-api/),
[native effects and matrix text](https://kno.wled.ge/features/effects/), and the supplied
AutoGlow-2 phase and segment-isolation implementations. AutoGlow-2's "matrix" of event
assignments is distinct from the physical LED matrix scoreboard required here.

## Phase 4 — MQTT, Home Assistant and scoreboard

1. Implement `mqtt.py`: versioned events and availability.
2. Implement `home_assistant.py`: webhooks, then MQTT discovery if useful.
3. Use the interpreted match state from phase 3 for a browser scoreboard/OBS overlay.
4. Add a deliberate Socket.IO adapter if legacy caller extensions are needed.

Acceptance: recover from broker outages without replaying effects from retained state;
publish a documented schema.

## Phase 5 — Rule editor and operations

1. API and CLI rule editing, action previews and integration status, with a simple optional UI.
2. Optional anonymized capture and deterministic replay for diagnostics.
3. Configuration migrations, export/backup and retention policy.
4. Interface authentication before shared or remote use.
5. Test amd64/arm64 images, verify Raspberry Pi operation and document updates.

## Remaining constraints

Live development checks use the configured `darts-caller` Client ID. Authentication with an
OAuth Client ID assigned to OcheCore still needs verification before distribution.
Upstream payloads may change. Caller, shared profiles and a durable event log remain planned.
A healthy process does not imply a connected board or controller.

All maintained project content uses English. Preserve the source brief and third-party references.
See [API research](AUTODARTS_API.md) and [validation notes](VALIDATION.md).
