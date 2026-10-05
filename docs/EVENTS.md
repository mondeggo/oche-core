# Event API

## Two independent streams

| Interface | Normalized game events | Raw AutoDarts frames |
|---|---|---|
| Recent history | `GET /api/events` | `GET /api/events/raw` |
| Live WebSocket | `/events` | `/events/raw` |
| CLI history | `ochecore events` | `ochecore events --raw` |
| CLI live | `ochecore events --follow` | `ochecore events --raw --follow` |

Each history holds the latest 100 entries in memory. Each live subscriber has an independent
bounded queue. Normal operation does not write events to disk or replay on WebSocket connection. The optional
UI switches between both streams. Both APIs remain available with the UI disabled.

## Debug recording

In the **Events** page, turn on **Debug recording**, reproduce the issue, then turn it off and
select **Download debug file**. The toggle is independent of the displayed stream: it always
captures raw AutoDarts frames. The lists still show only their latest 100 entries.

The same controls work without the UI:

```sh
uv run ochecore events --debug on
uv run ochecore events --debug status
uv run ochecore events --debug off
```

Recording happens in the service and continues after closing the terminal or page. It survives
AutoDarts reconnection and account/board changes. It starts off after a service restart.
Each recording creates `data/debug/autodarts-TIMESTAMP-ID.jsonl` (inside the existing Docker
bind mount). Existing files are retained; the API/UI offers the latest one, including after a
restart. Older recordings remain in that folder until you remove them.

Each line contains one incoming frame, including duplicates, unknown channels and control
messages, with a receipt timestamp and raw-stream sequence number:

```json
{"schema_version":1,"sequence":42,"received_at":"2026-10-02T12:00:00+00:00","raw":{"channel":"autodarts.matches","topic":"MATCH_ID.state","data":{}}}
```

The `raw` field preserves the incoming decoded JSON with known credential fields redacted,
just like `/api/events/raw`. Invalid JSON is recorded as a string. Captures contain future
incoming frames only; they do not include earlier history, REST snapshots or normalized events.
Use the frame order and timestamps to build replay tests for the normalizer.

| Endpoint | Purpose |
|---|---|
| `GET /api/events/debug` | Recording state, file path, frame/byte counts and errors |
| `PUT /api/events/debug` | `{"enabled":true}` starts; `{"enabled":false}` stops and flushes |
| `GET /api/events/debug/file` | Download the latest stopped recording as JSONL |

Recording uses a separate background writer; event delivery does not wait for disk writes.
Stop and graceful shutdown drain pending frames. Disk errors or a full write buffer stop the
recording and report an incomplete capture, while live event processing continues. Files are
not limited to 100 frames; their size depends on how long recording stays on.

## Current game state

`GET /api/game` and `ochecore game` expose the current display state independently of event
history: `board_id`, `match_id`, `available`, `phase`, `reason`, `player`, `remaining`,
`turn_score` and `last_dart`, plus match context when known. Scores come from validated cloud
state. REST baselines, corrections and undo update this view without requiring a new throw.

`available` means the cloud is connected and the board's online status was checked within
the greater of 60 seconds or three reconciliation intervals. Missing or invalid match state
clears displayed scores while keeping the event comparison baseline. The phase is `waiting`
until a valid match state arrives; with no active match it is `idle`.

For local players, `ready` requires explicit board status `Throw`, `Ready` or `Ready for throw`.
Unknown/calibrating statuses and new detection signals use `waiting`. Completed local visits
and local takeout signals use `takeout`; takeout completion waits for fresh readiness.
Remote players and bots cannot enable local ready lighting. `Throw` and paired detection/takeout
messages were verified against the October 2 X01 and Cricket capture.

`manual_reset`, `calibration_started` and `calibration_finished` are normalized from the board
event stream. Their paired board-state messages update readiness without emitting duplicates.
These board-wide events can arrive outside a match and do not require a player.
Calibration holds the phase at `waiting`, even when the last visit is complete or a takeout
was in progress. Finishing calibration does not invent readiness; a fresh board status supplies it.

### Raw frames

Raw entries preserve the decoded AutoDarts JSON structure, including unknown envelope fields,
duplicate messages, control messages and stale-match messages. No OcheCore envelope is added:

```json
{
  "channel": "autodarts.boards",
  "topic": "BOARD_ID.events",
  "data": {"event": "Takeout started"}
}
```

Only incoming AutoDarts WebSocket messages enter this stream. REST snapshots, outgoing
subscriptions, HTTP authentication requests and generated game events do not. Known credential
fields are redacted recursively. JSON whitespace is not preserved. Invalid JSON appears as a
string; invalid UTF-8 bytes are replaced with the Unicode replacement character.

### Normalized events

Normalized entries use the OcheCore envelope with `schema_version: 1`, a unique `id`,
`sequence`, `received_at`, `source: "core"`, `kind: "normalized"`, `event` and `data`.
The existing optional transport fields are null and `snapshot` is false. The sequence covers
internal transport envelopes too, so a sequence gap alone does not prove delivery loss.

`data` includes `board_id` and `match_id`. When a match state is available it also contains
`variant`, `set`, `leg`, `round`, `turn_id`, `remaining` and `player`. `board_id` names the
selected source board; `player.board_id` identifies the board of the player who acted.
Unknown context is null or absent, rather than inferred from the logged-in account.

`player` contains `id`, roster `index`, `name`, `board_id`, `is_local` and `is_bot`. Locality
uses the player's Board ID. A guest on the selected board is local; a bot is not a local
human player. Turn ownership uses `playerId` when supplied, even if the active player changed.
`game_score` comes from AutoDarts' `gameScores`. `remaining` exposes that value only for X01,
Random Checkout and 121; it is null for modes that do not count down. Neither is recomputed.
`turn_total` preserves `turn.score` (whose meaning depends on the mode); visit `score` and
`turn_score` preserve `turn.points`. Context also includes `settings`, an optional per-player
`target`, `checkout_available` from the upstream guide, and `editing`.

| Event | Trigger and additional fields |
|---|---|
| `match_started` | Explicit board match-start notification |
| `match_ended` | Matching board finish/delete/end notification; `reason` |
| `player_changed` | The active player's identity changes between full states |
| `turn_started` | A previously unseen visit begins, including solo play; `leg_start` identifies a new set/leg |
| `match_editing` | The selected match's `activated` field enters/leaves editing; `editing` |
| `throw` | A new scored dart in the newest visit; `dart`, `turn_score` |
| `throw_corrected` | A known dart's segment changes or a dart replaces an occupied position; `dart`, `previous_dart`. Restoring an undone ID uses `restored: true` |
| `throw_removed` | A previously present dart disappears from the same visit; `dart` |
| `turn_end` | Three darts, explicit bust/finish, a winner, or transition to a new visit; `score`, `busted` |
| `bust` | AutoDarts marks the visit as busted; `score`, `busted` |
| `checkout` | X01 or Random Checkout state reports a winner; `score` |
| `leg_win` | The state reports a game/leg winner; `score` |
| `match_win` | The state reports a match winner; `score` |
| `takeout_started` | Explicit board/match takeout-start event |
| `takeout_finished` | Explicit board/match takeout-finished event |
| `manual_reset` | Explicit board reset event |
| `calibration_started` | Explicit board calibration-start event |
| `calibration_finished` | Explicit board calibration-finished event |

`dart` contains its stable `id`, one-based `position`, `segment` and `points`. Segment fields
are `name`, `number`, `multiplier` and `bed`. Dart points are number × multiplier: T20 is 60,
double bull is 50 and a miss is 0. A miss alone is not a bust. Scores and win decisions remain
authoritative AutoDarts fields; OcheCore does not implement its own X01 scoring engine.

## Interpretation rules

- Turns are newest first (`turns[0]`); darts within a visit are oldest first.
- A REST snapshot or first full state initializes a silent baseline. Starting OcheCore or
  reconnecting during a match does not announce historical throws or wins. No `match_started`
  event is invented when an existing match is discovered by REST.
- Repeated states and timestamp-only updates produce no new scoring events.
- Corrections are distinct from throws. Undo produces removal events. A new dart ID after
  undo can produce a new throw; reintroducing the same ID produces a restoration event.
- Wins are remembered by match, set, leg and winning dart ID, so repeated Finish messages
  and a correction of the winning dart do not announce the win twice.
- Partial activation updates do not erase the baseline. Malformed scoring states remain
  visible in the raw stream and increment `events.invalid_match_states`.
- Board `Throw detected` messages are detection signals, not scored darts. Takeout events
  do not synthesize throws. Takeout completion retains the player from takeout start when known.
- Deduplication keeps 256 visits and 4096 recent event identities for the selected match.
  Reconnection refreshes the baseline; configuration changes and restarts reset the normalizer.
- Events missed while offline are not reconstructed. An unversioned older state cannot always
  be distinguished from a deliberate undo; raw capture is available for further investigation.

Status includes received raw-frame count, emitted normalized count, invalid scoring-state
count, separate subscriber counts and separate delivery-drop counters. Raw traffic cannot
evict entries from a normalized subscriber's queue or normalized history.

AutoDarts subscription errors also remain in the raw stream. They appear under
`cloud.subscription_errors` in `/api/status`, rather than being counted as malformed messages.
Rejection of a board or match topic marks the connection `degraded` and makes `/readyz` return
503. OcheCore subscribes only to board and match topics. The unused user-events subscription
was removed after the development Client ID rejected it. Errors clear on topic removal,
incoming data for that topic, or reconnection.

The October 2 live recording contains 490 frames, including X01 and Cricket scoring states.
It confirmed `Throw` as an explicit board-ready status and `0001-01-01T00:00:00Z` as an
unfinished visit, rather than an end-of-turn signal. Paired board state/event frames preserve
readiness; scored darts do not invalidate a matching board throw count. Reduced, anonymized
visits cover this behavior and a real dart correction in the regression tests.

Rechecked October 3: the same 490-frame file remains the latest capture. It contains 182 full
scoring states (150 X01 and 32 Cricket) plus one partial match update. Ordered replay accepts
every full state and emits 334 normalized events, including 114 throws and two corrections.
Calibration exposed a phase issue after completed visits: a remembered takeout could override
the explicit `Calibrating` status. Calibration now takes priority and shows `waiting`, including
outside a match. All seven captured calibration states show this phase after the fix. These
counts describe replay of the capture; it does not include the service's REST snapshots.

## Comparison with supplied resources

Checked October 2, 2026:

- [Tools for AutoDarts types](https://github.com/creazy231/tools-for-autodarts/blob/main/utils/websocket-helpers.ts)
  define the full match, player, visit and segment fields used by the parser.
- [Tools' update handling](https://github.com/creazy231/tools-for-autodarts/blob/main/utils/settle-game-data.ts)
  uses `turns[0]` and describes repeated updates during corrections and visit completion.
- [Tools' win identity](https://github.com/creazy231/tools-for-autodarts/blob/main/utils/win.ts)
  uses match/set/leg/winning-dart identity to suppress duplicate wins.
- [darts-caller](https://github.com/lbormann/darts-caller/blob/master/darts-caller.py)
  also uses `turns[0]`, `gameWinner`, `winner`, `gameScores` and `busted` in `process_match_x01`.
- The supplied AutoGlow-2 `core/autodarts_client.py` provides cloud topic and takeout
  references. This local reference is excluded from Git. Its last-turn selection and
  miss-to-bust effect mapping are not adopted.

The X01 fixture and replay transitions are synthetic and based on these sources. They test
local/remote/guest attribution, corrections, undo, busts, wins, leg changes and reconnection.
The real board detail response confirmed nested `state.connected`. Live X01 and Cricket
visits now supplement the synthetic cases; see the capture notes above.
Caller replay tests now cover the common scoring/win contracts in all 14 reference modes,
CountUp aliases and Cricket/Tactics, plus targeted checkout and target cases. These transitions
are synthetic; full live-match acceptance across the modes remains pending.
