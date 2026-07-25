import mido
import pytest

from show_orchestrator.generator import DEFAULT_VELOCITY, MidiGenerator
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


def test_note_on_uses_full_velocity_and_note_off_uses_zero(tmp_path, show):
    generated = MidiGenerator().generate_midi_files(show, tmp_path)["Song"][EffectType.LIGHTS]

    velocities = {
        (msg.type, msg.velocity)
        for msg in mido.MidiFile(generated["file_path"]).tracks[0]
        if not msg.is_meta
    }
    assert velocities == {("note_on", DEFAULT_VELOCITY), ("note_off", 0)}


def test_velocity_is_configurable(tmp_path, show):
    generated = MidiGenerator(velocity=64).generate_midi_files(show, tmp_path)["Song"][EffectType.LIGHTS]

    note_ons = [
        msg for msg in mido.MidiFile(generated["file_path"]).tracks[0]
        if not msg.is_meta and msg.type == "note_on"
    ]
    assert [msg.velocity for msg in note_ons] == [64, 64]


def test_track_is_named_after_the_song_and_effect_type(tmp_path, show):
    generated = MidiGenerator().generate_midi_files(show, tmp_path)["Song"][EffectType.LIGHTS]

    track = mido.MidiFile(generated["file_path"]).tracks[0]
    assert track[0].type == "track_name"
    assert track[0].name == "Song_lights"


def test_each_cue_is_marked_with_its_effect_name(tmp_path, show):
    generated = MidiGenerator().generate_midi_files(show, tmp_path)["Song"][EffectType.LIGHTS]

    track = mido.MidiFile(generated["file_path"]).tracks[0]
    markers = [msg for msg in track if msg.type == "marker"]
    assert [msg.text for msg in markers] == ["pinned", "auto"]

    # A marker sits immediately before the note_on it names, at the same instant.
    for index, msg in enumerate(track):
        if msg.type == "marker":
            assert track[index + 1].type == "note_on"
            assert track[index + 1].time == 0


def test_markers_do_not_shift_note_timing(tmp_path, show):
    generator = MidiGenerator()
    generated = generator.generate_midi_files(show, tmp_path)["Song"][EffectType.LIGHTS]

    events = absolute_events(mido.MidiFile(generated["file_path"]), generator.tempo)
    assert [event[0] for event in events] == [
        pytest.approx(1.0), pytest.approx(1.1), pytest.approx(2.0), pytest.approx(5.0),
    ]


def test_effect_name_outside_latin1_still_saves(tmp_path):
    show = Show(
        audio_tracks=[
            AudioTrack(
                name="Song",
                duration="0:10",
                events={EffectType.LIGHTS: [Event(timestamp="0:01", effect_id="a")]},
            )
        ],
        effects={EffectType.LIGHTS: [Effect(id="a", name="Blackout — all off ✨")]},
    )
    generated = MidiGenerator().generate_midi_files(show, tmp_path)["Song"][EffectType.LIGHTS]

    markers = [msg for msg in mido.MidiFile(generated["file_path"]).tracks[0] if msg.type == "marker"]
    assert len(markers) == 1


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
