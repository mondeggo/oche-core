import json
from copy import deepcopy
from pathlib import Path

import pytest
from conftest import BOARD_ID, MATCH_ID

from ochecore.events import EventBus, EventNormalizer


@pytest.fixture
def frame():
    return json.loads((Path(__file__).parent / "fixtures/x01-state.json").read_text())


@pytest.fixture
def replay(frame):
    bus = EventBus(capacity=1000)
    normalizer = EventNormalizer(BOARD_ID, bus)
    normalizer.select(MATCH_ID)

    def feed(data, event="match.state", snapshot=False):
        before = bus.sequence
        raw = bus.publish("cloud", event, deepcopy(data), snapshot=snapshot)
        normalizer.consume(raw)
        return [item for item in bus.normalized_history if item.sequence > before]

    feed.normalizer = normalizer
    feed.bus = bus
    feed(frame, snapshot=True)
    return feed


def dart(identifier="dart-1", number=20, multiplier=3):
    return {
        "id": identifier,
        "segment": {
            "number": number,
            "multiplier": multiplier,
            "name": f"{'SDT'[multiplier - 1]}{number}" if multiplier else "Miss",
            "bed": "Triple" if multiplier == 3 else "Single",
        },
    }


def names(events):
    return [event.event for event in events]


def add_dart(frame, value=None):
    turn = frame["turns"][0]
    value = value or dart(f"dart-{len(turn['throws']) + 1}")
    turn["throws"].append(value)
    turn["points"] += value["segment"]["number"] * value["segment"]["multiplier"]
    frame["gameScores"][frame["player"]] = 501 - turn["points"]


@pytest.mark.parametrize("variant", ["X01", "Cricket"])
@pytest.mark.parametrize("state_first", [True, False])
def test_captured_visits_and_paired_board_readiness(variant, state_first):
    frames = json.loads((Path(__file__).parent / "fixtures/live-visits.json").read_text())[variant]
    bus = EventBus()
    normalizer = EventNormalizer(BOARD_ID, bus)
    normalizer.select(MATCH_ID)

    def feed(name, data):
        normalizer.consume(bus.publish("cloud", name, deepcopy(data)))

    feed("match.state", frames[0])
    feed("board.state", {"status": "Throw", "event": "Manual reset", "numThrows": 0})
    feed("board.events", {"event": "Manual reset"})
    assert normalizer.current_state(True)["phase"] == "ready"
    assert names(bus.normalized_history) == ["manual_reset"]
    for count, frame in enumerate(frames[1:4], 1):
        status = {
            "status": "Throw" if count < 3 else "Takeout",
            "event": "Throw detected",
            "numThrows": count,
        }
        if state_first:
            feed("board.state", status)
        feed("board.events", {"event": "Throw detected", "throwNumber": count})
        feed("match.state", frame)
        if not state_first:
            feed("board.state", status)
        assert normalizer.current_state(True)["phase"] == ("ready" if count < 3 else "takeout")
        endings = [e for e in bus.normalized_history if e.event == "turn_end"]
        assert len(endings) == (1 if count == 3 else 0)
        feed("match.state", frame)  # Identical frames must not repeat calls.
    assert names(bus.normalized_history).count("throw") == 3
    if len(frames) > 4:
        feed("match.state", frames[4])
        assert bus.normalized_history[-1].event == "throw_corrected"
    feed(
        "board.state", {"status": "Takeout in progress", "event": "Takeout started", "numThrows": 3}
    )
    feed("board.events", {"event": "Takeout started"})
    feed("board.state", {"status": "Throw", "event": "Takeout finished", "numThrows": 0})
    feed("board.events", {"event": "Takeout finished"})
    assert normalizer.current_state(True)["phase"] == "ready"
    assert names(bus.normalized_history).count("takeout_finished") == 1


def test_three_darts_repeat_and_finished_timestamp(frame, replay):
    for index in range(3):
        add_dart(frame)
        events = replay(frame)
        assert names(events) == (["throw"] if index < 2 else ["throw", "turn_end"])
        assert events[0].data["dart"]["points"] == 60
        assert events[0].data["dart"]["position"] == index + 1
        assert events[0].data["player"]["is_local"] is True
        assert events[0].source == "core"
        assert replay(frame) == []
    frame["turns"][0]["finishedAt"] = "2026-10-02T12:00:00Z"
    assert replay(frame) == []


@pytest.mark.parametrize("state_first", [True, False])
def test_captured_calibration_overrides_completed_visit_and_takeout(replay, state_first):
    frames = json.loads((Path(__file__).parent / "fixtures/live-visits.json").read_text())["X01"]
    replay(frames[-1])
    replay({"event": "Takeout started"}, "board.events")
    assert replay.normalizer.current_state(True)["phase"] == "takeout"

    state = {"status": "Calibrating", "event": "Calibration started", "numThrows": 0}
    messages = [(state, "board.state"), ({"event": "Calibration started"}, "board.events")]
    if not state_first:
        messages.reverse()
    for data, event in messages:
        replay(data, event)
        view = replay.normalizer.current_state(True)
        assert view["phase"] == "waiting"
        assert view["reason"] == "Board is calibrating"
        assert view["turn_score"] == 14
    assert names(replay.bus.normalized_history).count("calibration_started") == 1

    # A repeated scoring state cannot override the board's calibration status.
    replay(frames[-1])
    assert replay.normalizer.current_state(True)["phase"] == "waiting"
    replay({"event": "Calibration finished"}, "board.events")
    replay({"status": "Throw", "event": "Calibration finished", "numThrows": 0}, "board.state")
    next_visit = deepcopy(frames[0])
    next_visit["turns"][0]["id"] = "after-calibration"
    replay(next_visit)
    replay({"status": "Throw", "event": "Manual reset", "numThrows": 0}, "board.state")
    assert replay.normalizer.current_state(True)["phase"] == "ready"


def test_calibration_outside_a_match_waits_until_finished():
    bus = EventBus()
    normalizer = EventNormalizer(BOARD_ID, bus)
    normalizer.consume(bus.publish("cloud", "board.events", {"event": "Calibration started"}))
    assert normalizer.current_state(True)["phase"] == "waiting"
    normalizer.consume(bus.publish("cloud", "board.events", {"event": "Calibration finished"}))
    assert normalizer.current_state(True)["phase"] == "idle"


def test_batched_darts_are_all_delivered(frame, replay):
    for _ in range(3):
        add_dart(frame)
    assert names(replay(frame)) == ["throw", "throw", "throw", "turn_end"]


def test_correction_undo_and_rethrow(frame, replay):
    add_dart(frame)
    replay(frame)
    frame["turns"][0]["throws"][0] = dart(number=19)
    events = replay(frame)
    assert names(events) == ["throw_corrected"]
    assert events[0].data["previous_dart"]["points"] == 60
    assert events[0].data["dart"]["points"] == 57
    assert replay(frame) == []
    frame["turns"][0]["throws"] = []
    assert names(replay(frame)) == ["throw_removed"]
    assert replay(frame) == []
    add_dart(frame, dart("new-dart"))
    assert names(replay(frame)) == ["throw"]


def test_replaced_id_is_a_correction_and_restored_id_is_not_a_throw(frame, replay):
    add_dart(frame)
    replay(frame)
    frame["turns"][0]["throws"][0] = dart("replacement", 5, 1)
    assert names(replay(frame)) == ["throw_corrected"]
    frame["turns"][0]["throws"] = []
    assert names(replay(frame)) == ["throw_removed"]
    add_dart(frame, dart("replacement", 5, 1))
    restored = replay(frame)
    assert names(restored) == ["throw_corrected"]
    assert restored[0].data["restored"] is True


def next_turn(frame, player=1):
    frame["player"] = player
    frame["turns"].insert(
        0,
        {
            "id": "visit-2",
            "playerId": frame["players"][player]["id"],
            "round": 1,
            "points": 0,
            "throws": [],
        },
    )


def test_newest_turn_is_first_and_locality_uses_board_not_account(frame, replay):
    add_dart(frame)
    replay(frame)
    next_turn(frame)
    assert names(replay(frame)) == ["turn_end", "player_changed", "turn_started"]
    add_dart(frame, dart("remote-dart"))
    events = replay(frame)
    assert names(events) == ["throw"]
    assert events[0].data["turn_id"] == "visit-2"
    assert events[0].data["player"]["is_local"] is False
    # A guest on the selected board is local even with a different player/account ID.
    frame["players"][1]["boardId"] = BOARD_ID
    add_dart(frame, dart("guest-dart"))
    assert replay(frame)[0].data["player"]["is_local"] is True
    frame["players"][1]["cpuPPR"] = 60
    add_dart(frame, dart("bot-dart"))
    assert replay(frame)[0].data["player"]["is_local"] is False


@pytest.mark.parametrize("replace_id", [False, True])
def test_previous_visit_correction_updates_cache_without_replaying_throws(
    frame, replay, replace_id
):
    add_dart(frame)
    replay(frame)
    next_turn(frame)
    replay(frame)
    old_turn = frame["turns"][1]
    old_turn["throws"][0] = dart("replacement" if replace_id else "dart-1", 20, 1)
    old_turn["points"] = 20
    frame["gameScores"][0] = 481
    events = replay(frame)
    assert names(events) == ["throw_corrected"]
    assert events[0].data["historical"] is True
    assert events[0].data["turn_id"] == "visit-1"
    assert events[0].data["player"]["id"] == "local-player"
    assert events[0].data["previous_dart"]["points"] == 60
    assert events[0].data["dart"]["points"] == 20
    assert events[0].data["remaining"] == 481
    assert replay(frame) == []
    # Returning to the corrected visit uses its updated baseline.
    frame["turns"].pop(0)
    frame["player"] = 0
    assert names(replay(frame)) == ["player_changed"]


def test_previous_visit_removal_and_addition_do_not_change_live_readiness(frame, replay):
    add_dart(frame)
    replay(frame)
    next_turn(frame, player=0)
    replay(frame)
    replay({"status": "Throw", "numThrows": 0}, "board.state")
    old_turn = frame["turns"][1]
    old_turn["throws"] = []
    old_turn["points"] = 0
    removed = replay(frame)
    assert names(removed) == ["throw_removed"]
    assert removed[0].data["historical"] is True
    old_turn["throws"] = [dart("added-later")]
    old_turn["points"] = 60
    added = replay(frame)
    assert names(added) == ["throw_corrected"]
    assert added[0].data["historical"] is True
    assert added[0].data["restored"] is False
    assert replay.normalizer.current_state(True)["phase"] == "ready"
    assert replay(frame) == []


def test_backfilled_visit_darts_are_corrections_and_ending_uses_updated_total(frame, replay):
    add_dart(frame)
    replay(frame)
    next_turn(frame)
    old_turn = frame["turns"][1]
    old_turn["throws"].extend([dart("late-dart-2"), dart("late-dart-3")])
    old_turn["points"] = 180
    frame["gameScores"][0] = 321
    events = replay(frame)
    assert names(events) == [
        "throw_corrected",
        "throw_corrected",
        "turn_end",
        "player_changed",
        "turn_started",
    ]
    assert all(event.data["historical"] for event in events[:2])
    assert events[2].data["score"] == 180
    assert events[2].data["remaining"] == 321
    assert replay(frame) == []


def test_newly_discovered_history_is_silent_but_later_corrections_are_reported(frame, replay):
    frame["turns"].append(
        {
            "id": "older-visit",
            "playerId": "remote-player",
            "points": 60,
            "throws": [dart("older-dart")],
            "finishedAt": "2026-10-02T12:00:00Z",
        }
    )
    assert replay(frame) == []
    frame["turns"][1]["throws"][0] = dart("older-dart", 19, 1)
    frame["turns"][1]["points"] = 19
    assert names(replay(frame)) == ["throw_corrected"]
    assert replay(frame) == []


@pytest.mark.parametrize("snapshot", [False, True])
def test_history_changes_during_resync_establish_silent_baseline(frame, replay, snapshot):
    add_dart(frame)
    replay(frame)
    next_turn(frame)
    replay(frame)
    replay.normalizer.resync()
    frame["turns"][1]["throws"][0] = dart("changed-offline", 19, 1)
    frame["turns"][1]["points"] = 19
    assert replay(frame, snapshot=snapshot) == []
    assert replay(frame) == []
    frame["turns"][1]["throws"][0] = dart("changed-offline", 5, 1)
    assert names(replay(frame)) == ["throw_corrected"]


def test_long_history_keeps_current_visit_silent_after_snapshot(frame, replay):
    frame["turns"].extend(
        {"id": f"older-visit-{index}", "playerId": "local-player", "points": 0, "throws": []}
        for index in range(300)
    )
    assert replay(frame, snapshot=True) == []
    assert replay(frame) == []
    add_dart(frame)
    assert names(replay(frame)) == ["throw"]
    assert replay(frame) == []


def test_undo_to_previous_visit_does_not_reannounce_darts_or_end_undone_visit(frame, replay):
    add_dart(frame)
    replay(frame)
    previous = deepcopy(frame)
    next_turn(frame)
    replay(frame)
    add_dart(frame, dart("remote-dart"))
    replay(frame)
    assert names(replay(previous)) == ["player_changed"]


def test_takeout_and_detection_are_not_scored_throws(frame, replay):
    assert replay({"event": "Throw detected"}, "board.events") == []
    started = replay({"event": "Takeout started"}, "board.events")
    assert names(started) == ["takeout_started"]
    assert replay({"event": "Takeout started"}, "match.events") == []
    assert names(replay({"event": "Takeout finished"}, "board.events")) == ["takeout_finished"]
    assert replay({"event": "Takeout finished"}, "board.events") == []
    replay({"event": "Throw detected"}, "board.events")
    assert names(replay({"event": "Takeout started"}, "board.events")) == ["takeout_started"]


def test_miss_is_not_bust_and_bust_is_once_per_visit(frame, replay):
    add_dart(frame, dart("miss", 0, 0))
    assert names(replay(frame)) == ["throw"]
    frame["turns"][0]["busted"] = True
    assert names(replay(frame)) == ["bust", "turn_end"]
    assert replay(frame) == []
    next_turn(frame)
    replay(frame)
    add_dart(frame, dart("other-bust"))
    frame["turns"][0]["busted"] = True
    assert names(replay(frame)) == ["throw", "bust", "turn_end"]


@pytest.mark.parametrize("match_won", [False, True])
def test_winner_zero_checkout_and_repeated_finish(frame, replay, match_won):
    add_dart(frame, dart("winning-dart", 20, 2))
    frame["gameWinner"] = 0
    frame["winner"] = 0 if match_won else -1
    frame["gameScores"][0] = 0
    events = replay(frame)
    assert names(events) == ["throw", "turn_end", "checkout", "leg_win"] + (
        ["match_win"] if match_won else []
    )
    assert all(event.data["player"]["index"] == 0 for event in events)
    frame["finished"] = match_won
    frame["turns"][0]["finishedAt"] = "2026-10-02T12:00:00Z"
    assert replay(frame) == []
    frame["turns"][0]["throws"][0]["segment"]["bed"] = "Double"
    assert names(replay(frame)) == ["throw_corrected"]
    frame["turns"][0]["throws"][0]["id"] = "corrected-winning-dart"
    assert names(replay(frame)) == ["throw_corrected"]
    assert replay(frame) == []


def test_victory_after_actual_undo_is_reported_even_when_same_dart_is_restored(frame, replay):
    add_dart(frame, dart("winning-dart", 20, 2))
    frame["gameWinner"] = frame["winner"] = 0
    replay(frame)
    won = deepcopy(frame)
    frame["gameWinner"] = frame["winner"] = -1
    frame["turns"][0]["throws"] = []
    assert names(replay(frame)) == ["throw_removed"]
    events = replay(won)
    assert names(events) == ["throw_corrected", "checkout", "leg_win", "match_win"]
    assert events[0].data["restored"] is True
    assert replay(won) == []


def test_match_victory_can_follow_leg_victory_without_repeating_leg(frame, replay):
    add_dart(frame, dart("winning-dart", 20, 2))
    frame["gameWinner"] = 0
    replay(frame)
    frame["winner"] = 0
    assert names(replay(frame)) == ["match_win"]


@pytest.mark.parametrize("snapshot", [False, True])
def test_winning_snapshot_then_dart_replacement_never_replays_victory(frame, replay, snapshot):
    add_dart(frame, dart("winning-dart", 20, 2))
    frame["gameWinner"] = frame["winner"] = 0
    replay.normalizer.resync()
    assert replay(frame, snapshot=snapshot) == []
    frame["turns"][0]["throws"][0]["id"] = "corrected-winning-dart"
    assert names(replay(frame)) == ["throw_corrected"]


def test_leg_transition_and_undo_then_new_checkout(frame, replay):
    add_dart(frame, dart("winning-dart", 20, 2))
    frame["gameWinner"] = 0
    replay(frame)
    frame["gameWinner"] = -1
    frame["turns"][0]["throws"] = []
    assert names(replay(frame)) == ["throw_removed"]
    add_dart(frame, dart("new-checkout", 20, 2))
    frame["gameWinner"] = 0
    assert names(replay(frame)) == ["throw", "checkout", "leg_win"]
    frame["leg"] = 2
    frame["gameWinner"] = -1
    frame["turns"][0]["throws"] = []
    replay(frame)
    add_dart(frame, dart("next-leg", 25, 2))
    events = replay(frame)
    assert names(events) == ["throw"]
    assert events[0].data["leg"] == 2
    assert events[0].data["dart"]["points"] == 50


def test_snapshot_reconnect_and_first_state_never_replay_actions(frame, replay):
    for _ in range(3):
        add_dart(frame)
    frame["gameWinner"] = frame["winner"] = 0
    replay.normalizer.resync()
    assert replay(frame, snapshot=True) == []
    assert replay(frame) == []
    replay.normalizer.resync()
    assert replay(frame) == []  # Same safety when the REST snapshot was unavailable.
    replay.normalizer.select(None)
    replay.normalizer.select(MATCH_ID)
    assert replay(frame) == []


def test_partial_invalid_and_wrong_match_do_not_damage_baseline(frame, replay):
    add_dart(frame)
    assert names(replay(frame)) == ["throw"]
    assert names(replay({"id": MATCH_ID, "activated": 1})) == ["match_editing"]
    bad = deepcopy(frame)
    bad["turns"] = "wrong"
    assert replay(bad) == []
    assert replay.normalizer.invalid_states == 1
    bad = deepcopy(frame)
    bad["id"] = "stale-match"
    assert replay(bad) == []
    assert replay(frame) == []
    add_dart(frame)
    assert names(replay(frame)) == ["throw"]


def test_takeout_keeps_owner_when_state_advances_to_next_player(frame, replay):
    add_dart(frame)
    replay(frame)
    started = replay({"event": "Takeout started"}, "board.events")
    next_turn(frame)
    replay(frame)
    finished = replay({"event": "Takeout finished"}, "board.events")
    assert started[0].data["player"] == finished[0].data["player"]
    assert finished[0].data["player"]["id"] == "local-player"


def test_reconnect_in_middle_of_visit_only_announces_subsequent_darts(frame, replay):
    add_dart(frame)
    replay.normalizer.resync()
    assert replay(frame, snapshot=True) == []
    add_dart(frame)
    events = replay(frame)
    assert names(events) == ["throw"]
    assert events[0].data["dart"]["position"] == 2


def test_match_lifecycle_and_new_match_reset(frame, replay):
    assert names(replay({"event": "start", "id": MATCH_ID}, "board.matches")) == ["match_started"]
    assert replay({"event": "start", "id": MATCH_ID}, "board.matches") == []
    assert replay({"event": "delete", "id": "old"}, "board.matches") == []
    assert names(replay({"event": "delete", "id": MATCH_ID}, "board.matches")) == ["match_ended"]
    replay.normalizer.select(None)
    assert names(replay({"event": "start", "id": "new-match"}, "board.matches")) == [
        "match_started"
    ]
    assert replay.normalizer.frame is None


def test_reordered_roster_uses_array_position_for_winner(frame, replay):
    frame["players"].reverse()
    frame["player"] = 1
    frame["players"][1]["index"] = 0
    add_dart(frame)
    frame["gameWinner"] = frame["winner"] = 1
    events = replay(frame)
    assert events[-1].event == "match_win"
    assert events[-1].data["player"]["id"] == "local-player"
    assert events[-1].data["player"]["index"] == 1


def test_turn_context_for_solo_targets_and_partial_editing(frame, replay):
    frame["variant"] = "ATC"
    frame["state"] = {"targets": [[{"number": 5, "bed": "Double"}], []], "currentTargets": [0, 0]}
    frame["turns"][0]["id"] = "solo-visit"
    events = replay(frame)
    started = next(event for event in events if event.event == "turn_started")
    assert started.data["target"] == {"number": 5, "bed": "Double"}
    assert started.data["remaining"] is None
    assert started.data["game_score"] == 501
    assert replay({"id": MATCH_ID, "activated": 1})[0].data["editing"] is True
    assert replay({"id": MATCH_ID, "activated": -1})[0].data["editing"] is False
    add_dart(frame)
    assert replay(frame)[0].data["editing"] is False


@pytest.mark.parametrize("completed", [False, True])
def test_editing_overrides_ready_or_takeout_until_editing_finishes(frame, replay, completed):
    if completed:
        for _ in range(3):
            add_dart(frame)
        replay(frame)
    replay({"status": "Takeout" if completed else "Throw"}, "board.state")
    phase = "takeout" if completed else "ready"
    assert replay.normalizer.current_state(True)["phase"] == phase
    assert names(replay({"id": MATCH_ID, "activated": 0})) == ["match_editing"]
    view = replay.normalizer.current_state(True)
    assert view["editing"] is True
    assert view["phase"] == "waiting"
    assert view["reason"] == "Editing score"
    assert view["remaining"] == frame["gameScores"][0]
    replay({"id": MATCH_ID, "activated": -1})
    assert replay.normalizer.current_state(True)["phase"] == phase


def test_editing_snapshot_is_silent_and_never_shows_ready(frame, replay):
    frame["activated"] = 0
    assert replay(frame, snapshot=True) == []
    replay({"status": "Throw"}, "board.state")
    assert replay.normalizer.current_state(True)["phase"] == "waiting"
    assert replay.normalizer.current_state(True)["reason"] == "Editing score"
