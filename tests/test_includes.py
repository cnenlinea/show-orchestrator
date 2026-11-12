"""Lights timelines included from light-show-simulator: includes, the channel column, the lights-channel checks."""
import pytest

from show_orchestrator.checks import ShowCheckError, check_note_clashes, check_show
from show_orchestrator.generator import MidiGenerator
from show_orchestrator.models import EffectType
from show_orchestrator.notes import assign_missing_notes
from show_orchestrator.parser import Parser

SHOW = (
    "name,type,timestamp,duration,note,file\n"
    "Tambores,audio,,3:27,,\n"
    "Full Blackout,projection,0:00,0:00.1,1,\n"
    "Tambores BG,projection,0:00.1,3:16,2,\n"
    "Obertura,audio,,3:46,,\n"
    "Obertura BG,projection,0:00,3:45,3,\n"
    ",include,,,,show.lights.csv\n"
)
LIGHTS = (
    "name,type,timestamp,duration,note,file,channel\n"
    "Tambores,audio,,3:27,,,\n"
    "Tambores · Intro,lights,0:00.1,0:00.1,0,,1\n"
    "barrido,lights,0:31,0:00.1,1,,1\n"
    "barrido,lights,0:51,0:00.1,1,,1\n"
    "Obertura,audio,,3:46,,,\n"
    "Obertura · Negro,lights,0:00,0:00.1,0,,2\n"
)


def write(tmp_path, show=SHOW, lights=LIGHTS):
    (tmp_path / "show.csv").write_text(show, encoding="utf-8")
    (tmp_path / "show.lights.csv").write_text(lights, encoding="utf-8")
    return tmp_path / "show.csv"


def test_include_adds_lights_to_songs_by_name(tmp_path):
    show = Parser().load_show(write(tmp_path))
    assert show.include == ["show.lights.csv"]
    tambores, obertura = show.audio_tracks
    assert [e.effect_id for e in tambores.events[EffectType.LIGHTS]] == ["Tambores · Intro", "barrido", "barrido"]
    assert [e.effect_id for e in tambores.events[EffectType.PROJECTION]] == ["Full Blackout", "Tambores BG"]
    lights = {e.id: e for e in show.effects[EffectType.LIGHTS]}
    assert (lights["barrido"].note, lights["barrido"].channel) == (1, 1)
    assert (lights["Obertura · Negro"].note, lights["Obertura · Negro"].channel) == (0, 2)
    assert all(e.included_from == "show.lights.csv" for e in lights.values())
    check_show(show)


def test_include_with_an_unknown_song_is_refused(tmp_path):
    with pytest.raises(ValueError, match="does not have"):
        Parser().load_show(write(tmp_path, lights=LIGHTS.replace("Obertura,audio", "Otra,audio")))


def test_hand_made_lights_rows_are_refused_once_a_show_includes(tmp_path):
    show = Parser().load_show(write(tmp_path, show=SHOW + "Luz a mano,lights,0:05,,9,\n"))
    with pytest.raises(ShowCheckError, match="not from an included lights file"):
        check_show(show)


def test_other_types_on_lights_channels_are_refused(tmp_path):
    show = Parser().load_show(write(tmp_path, show=SHOW.replace("Obertura BG,projection,0:00,3:45,3,", "Obertura BG,projection,0:00,3:45,3,,1")))
    with pytest.raises(ShowCheckError, match="uses lights channel 1"):
        check_show(show)


def test_shows_without_includes_keep_their_lights_rows():
    from pathlib import Path
    show = Parser().load_show(Path(__file__).parent.parent / "example.csv")
    check_show(show)


def test_note_clashes_are_refused(tmp_path):
    show = Parser().load_show(write(tmp_path, lights=LIGHTS.replace("Obertura · Negro,lights,0:00,0:00.1,0,,2", "Obertura · Negro,lights,0:00,0:00.1,0,,1")))
    mapping = assign_missing_notes(show.effects)
    with pytest.raises(ShowCheckError, match="share note 0 on channel 1"):
        check_note_clashes(mapping)


def test_midi_uses_the_included_channels(tmp_path):
    path = write(tmp_path)
    show = Parser().load_show(path)
    (tmp_path / "out").mkdir()
    files = MidiGenerator().generate_midi_files(show, tmp_path / "out", None)
    import mido
    mid = mido.MidiFile(files["Tambores"][EffectType.LIGHTS]["file_path"])
    notes = [(m.channel, m.note) for m in mid.tracks[0] if m.type == "note_on"]
    assert notes == [(1, 0), (1, 1), (1, 1)]
