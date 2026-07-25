import mido
import pytest

from show_orchestrator.generator import MidiGenerator
from show_orchestrator.models import AudioTrack, Effect, EffectType, Event, Show


@pytest.fixture
def show():
    return Show(
        audio_tracks=[
            AudioTrack(
                name="Song",
                duration="0:10",
                events={
                    EffectType.LIGHTS: [
                        Event(timestamp="0:01", effect_id="pinned"),
                        Event(timestamp="0:02", effect_id="auto", duration=3),
                    ],
                    EffectType.PROJECTION: [],
                },
            )
        ],
        effects={
            EffectType.LIGHTS: [
                Effect(id="pinned", name="pinned", note=10, channel=2),
                Effect(id="auto", name="auto"),
            ],
            EffectType.PROJECTION: [],
        },
    )


def absolute_events(midi_file: mido.MidiFile, tempo: int) -> list[tuple[float, str, int, int]]:
    events = []
    now = 0.0
    for msg in midi_file.tracks[0]:
        now += mido.tick2second(msg.time, midi_file.ticks_per_beat, tempo)
        if not msg.is_meta:
            events.append((now, msg.type, msg.note, msg.channel))
    return events


def test_generate_midi_files(tmp_path, show):
    generator = MidiGenerator()
    result = generator.generate_midi_files(show, tmp_path)

    generated = result["Song"][EffectType.LIGHTS]
    assert generated["file_path"].exists()
    assert generated["duration"] == pytest.approx(5.0)
    # empty projection events produce no file
    assert EffectType.PROJECTION not in result["Song"]

    midi_file = mido.MidiFile(generated["file_path"])
    events = absolute_events(midi_file, generator.tempo)
    assert [
        (pytest.approx(1.0), "note_on", 10, 2),
        (pytest.approx(1.1), "note_off", 10, 2),
        (pytest.approx(2.0), "note_on", 0, 0),
        (pytest.approx(5.0), "note_off", 0, 0),
    ] == events


def test_auto_note_avoids_pinned_notes_on_same_channel(tmp_path):
    show = Show(
        audio_tracks=[],
        effects={
            EffectType.LIGHTS: [
                Effect(id="a", name="a", note=0),
                Effect(id="b", name="b"),
            ],
        },
    )
    mapping = MidiGenerator()._get_effects_by_id(show.effects)
    assert mapping["b"].note == 1
    assert mapping["b"].channel == 0
