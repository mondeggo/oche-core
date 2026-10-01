from ochecore.autodarts.cloud import MatchState
from ochecore.events import event_name, parse_cloud_message


def test_parser_rejects_invalid_frames_and_filters_old_matches():
    assert parse_cloud_message(b"\xff") is None
    assert parse_cloud_message('{"channel": [], "topic": "test", "data": {}}') is None
    message = parse_cloud_message(
        '{"channel":"autodarts.matches","topic":"old.state","data":{"id":"old"}}'
    )
    assert event_name(message, "board", "new") is None
    assert event_name(message, "board", "old") == "match.state"


def test_match_state_clears_previous_match_and_redacts_secrets():
    state = MatchState()
    state.select("first")
    state.update({"id": "first", "access_token": "private"})
    assert state.latest["access_token"] == "[redacted]"
    state.select("second")
    assert state.latest is None
    state.update({"id": "first"})
    assert state.latest is None
    state.update({"id": "second", "turns": []})
    assert state.latest["id"] == "second"
    state.select(None)
    assert state.latest is None
