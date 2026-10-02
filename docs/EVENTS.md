# Event API

## Two independent streams

| Interface | Normalized game events | Raw AutoDarts frames |
|---|---|---|
| Recent history | `GET /api/events` | `GET /api/events/raw` |
| Live WebSocket | `/events` | `/events/raw` |
| CLI history | `ochecore events` | `ochecore events --raw` |
| CLI live | `ochecore events --follow` | `ochecore events --raw --follow` |

Each history holds the latest 100 entries in memory. Each live subscriber has an independent
bounded queue. There is no persistent journal or replay on WebSocket connection. The optional
UI switches between both streams. Both APIs remain available with the UI disabled.

## Current game state

`GET /api/game` and `ochecore game` expose the current display state independently of event
history: `board_id`, `match_id`, `available`, `phase`, `reason`, `player`, `remaining`,
`turn_score` and `last_dart`, plus match context when known. Scores come from validated cloud
state. REST baselines, corrections and undo update this view without requiring a new throw.

`available` means the cloud is connected and the board's online status was checked within
the greater of 60 seconds or three reconciliation intervals. Missing or invalid match state
clears displayed scores while keeping the event comparison baseline. The phase is `waiting`
until a valid match state arrives; with no active match it is `idle`.

For local players, `ready` requires explicit board status `Ready` or `Ready for throw`.
Unknown/calibrating statuses and new detection signals use `waiting`. Completed local visits
and local takeout signals use `takeout`; takeout completion waits for fresh readiness.
Remote players and bots cannot enable local green lighting. These readiness strings are based
on supplied references and still require validation during real matches.

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
`remaining` comes from AutoDarts' `gameScores`; it is not recomputed by OcheCore.

| Event | Trigger and additional fields |
|---|---|
| `match_started` | Explicit board match-start notification |
| `match_ended` | Matching board finish/delete/end notification; `reason` |
| `player_changed` | The active player's identity changes between full states |
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
503. Rejection of the optional user-events topic is shown as a warning; game normalization
uses board and match topics. Errors clear on topic removal, incoming data for that topic, or
reconnection. The development `darts-caller` Client ID returned `unauthorized client` for the
user-events topic during the October 2 deployment check.

## Comparison with supplied resources

Checked October 2, 2026:

- [Tools for AutoDarts types](../ressources/tools-for-autodarts-main/tools-for-autodarts-main/utils/websocket-helpers.ts)
  define the full match, player, visit and segment fields used by the parser.
- [Tools' update handling](../ressources/tools-for-autodarts-main/tools-for-autodarts-main/utils/settle-game-data.ts)
  uses `turns[0]` and describes repeated updates during corrections and visit completion.
- [Tools' win identity](../ressources/tools-for-autodarts-main/tools-for-autodarts-main/utils/win.ts)
  uses match/set/leg/winning-dart identity to suppress duplicate wins.
- [darts-caller](../ressources/darts-caller-master/darts-caller-master/darts-caller.py)
  also uses `turns[0]`, `gameWinner`, `winner`, `gameScores` and `busted` in `process_match_x01`.
- [AutoGlow-2](../ressources/AutoGlow-2/core/autodarts_client.py) supplies cloud topic and takeout
  references. Its last-turn selection and miss-to-bust effect mapping are not adopted.

The X01 fixture and replay transitions are synthetic and based on these sources. They test
local/remote/guest attribution, corrections, undo, busts, wins, leg changes and reconnection.
The real board detail response confirmed nested `state.connected`. A previously observed
match's state endpoint returned 404, so no live scoring capture was available for this change.
Full live-match acceptance and broader training-variant coverage remain pending.
