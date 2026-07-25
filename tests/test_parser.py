from pathlib import Path

import pytest

from show_orchestrator.models import EffectType
from show_orchestrator.parser import Parser

REPO_ROOT = Path(__file__).parent.parent


def test_load_example_csv():
    show = Parser().load_show(REPO_ROOT / "example.csv")

    assert [t.name for t in show.audio_tracks] == ["Acapella", "Intro"]
    acapella, intro = show.audio_tracks
    assert acapella.duration_seconds == 62.0
    assert acapella.file_path is None
    assert intro.file_path == "shows/media/00 - Intro.mp3"

    assert len(acapella.events[EffectType.LIGHTS]) == 3
    assert len(acapella.events[EffectType.PROJECTION]) == 2
    assert len(intro.events[EffectType.LIGHTS]) == 2
    assert len(intro.events[EffectType.PROJECTION]) == 1


def test_csv_effects_are_deduplicated_by_name():
    show = Parser().load_show(REPO_ROOT / "example.csv")

    lights_ids = [e.id for e in show.effects[EffectType.LIGHTS]]
    assert lights_ids == ["Blackout Luz", "Fade In", "Strobe", "Luz Intro"]
    assert [e.id for e in show.effects[EffectType.PROJECTION]] == [
        "Blackout Proyección", "Fondo Acapella",
    ]

    blackout = show.effects[EffectType.LIGHTS][0]
    assert blackout.note == 2
    luz_intro = show.effects[EffectType.LIGHTS][3]
    assert luz_intro.note is None


def test_load_example_yaml():
    show = Parser().load_show(REPO_ROOT / "example.yaml")

    assert len(show.audio_tracks) == 2
    assert len(show.effects[EffectType.LIGHTS]) == 4
    assert len(show.effects[EffectType.PROJECTION]) == 2
    strobe = next(e for e in show.effects[EffectType.LIGHTS] if e.id == "strobe")
    assert strobe.note == 3
    assert strobe.channel == 0


def test_unsupported_extension_raises():
    with pytest.raises(ValueError, match="not supported"):
        Parser().load_show(Path("show.txt"))


def test_effect_row_before_audio_row_raises(tmp_path):
    csv = tmp_path / "bad.csv"
    csv.write_text(
        "name,type,timestamp,duration,note,file\n"
        "Orphan,lights,0:00,,,\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="before any audio row"):
        Parser().load_show(csv)


def test_invalid_note_raises(tmp_path):
    csv = tmp_path / "bad.csv"
    csv.write_text(
        "name,type,timestamp,duration,note,file\n"
        "Song,audio,,1:00,,\n"
        "Cue,lights,0:00,,abc,\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid MIDI note"):
        Parser().load_show(csv)
