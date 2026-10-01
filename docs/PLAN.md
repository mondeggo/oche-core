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
- [x] Non-root Docker image, persistent volume and healthcheck.
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

1. Independent bounded integration queues, timeouts and failure isolation.
2. Validated YAML rules: events, filters and lists of actions.
3. Implement `wled.py`: JSON presets, multiple devices and rate limits.
4. Implement `caller.py`: user-provided sounds, then player names and checkout announcements.
   Choose browser or host audio based on deployment needs.
5. Initial rules: triple, bull, 180, bust and victory; define priority and cancellation.

Acceptance: a WLED outage cannot block audio or incoming events. Rules can be tested without
a match. Replayed events do not trigger actions by default.

## Phase 4 — MQTT, Home Assistant and scoreboard

1. Implement `mqtt.py`: versioned events and availability.
2. Implement `home_assistant.py`: webhooks, then MQTT discovery if useful.
3. Expose interpreted match state and add a browser scoreboard/OBS overlay.
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
Upstream payloads may change. No caller, WLED effect, rule engine or durable event log is active
in phase 1. A healthy process does not imply a connected board.

All maintained project content uses English. Preserve the source brief and third-party references.
See [API research](AUTODARTS_API.md) and [validation notes](VALIDATION.md).
