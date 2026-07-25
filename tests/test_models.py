import pytest
from pydantic import ValidationError

from show_orchestrator.models import Event, ExtraAudioTrack, AudioTrack, to_seconds


def test_to_seconds_mmss():
    assert to_seconds("1:30") == 90.0
    assert to_seconds("0:05") == 5.0
    assert to_seconds("2:05.5") == 125.5


def test_to_seconds_numeric_passthrough():
    assert to_seconds(42) == 42.0
    assert to_seconds(3.5) == 3.5


def test_event_timestamp_seconds():
    event = Event(timestamp="1:02", effect_id="fx")
    assert event.timestamp_seconds == 62.0


def test_event_duration_defaults_to_none():
    event = Event(timestamp="0:00", effect_id="fx")
    assert event.duration is None
    assert event.duration_seconds is None


def test_event_rejects_malformed_timestamp():
    with pytest.raises(ValidationError):
        Event(timestamp="abc", effect_id="fx")
    with pytest.raises(ValidationError):
        Event(timestamp="100:00", effect_id="fx")


def test_event_rejects_negative_timestamp():
    with pytest.raises(ValidationError):
        Event(timestamp=-1, effect_id="fx")


def test_event_duration_accepts_numeric_string():
    event = Event(timestamp="0:00", effect_id="fx", duration="45.5")
    assert event.duration == 45.5


def test_event_rejects_invalid_duration():
    with pytest.raises(ValidationError):
        Event(timestamp="0:00", effect_id="fx", duration="abc")


def test_extra_audio_track_seconds():
    extra = ExtraAudioTrack(name="x", file_path="x.mid", duration="1:00", timestamp="0:30")
    assert extra.duration_seconds == 60.0
    assert extra.timestamp_seconds == 30.0


def test_audio_track_duration_seconds():
    track = AudioTrack(name="song", events={}, duration="3:20")
    assert track.duration_seconds == 200.0
