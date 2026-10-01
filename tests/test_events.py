from ochecore.autodarts.cloud import MatchState
from ochecore.events import EventBus, event_name, parse_cloud_message


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
    state.update({"id": "second", "activated": 1})
    assert state.latest["turns"] == []
    state.select(None)
    assert state.latest is None


def test_raw_and_normalized_streams_have_independent_bounded_queues():
    bus = EventBus(capacity=2)
    with (
        bus.subscribe(capacity=1, mode="raw") as raw,
        bus.subscribe(capacity=1, mode="normalized") as normalized,
    ):
        bus.publish("core", "throw", {"score": 60}, kind="normalized")
        for _ in range(3):
            bus.record_raw('{"type":"control","token_metadata":{"access_token":"secret"}}')
        assert normalized.get_nowait().event == "throw"
        assert raw.get_nowait()["token_metadata"]["access_token"] == "[redacted]"
        assert bus.raw_dropped == 2 and bus.dropped == 0
        assert len(bus.raw_history) == 2
        assert len(bus.normalized_history) == 1
        bus.record_raw("not json")
        assert bus.raw_history[-1] == "not json"
    assert not bus.raw_queues and not bus.normalized_queues
