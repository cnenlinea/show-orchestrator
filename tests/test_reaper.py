import pytest

from show_orchestrator.backends.reaper import ReaperBackend
from show_orchestrator.models import AudioTrack, Effect, EffectType, Show


@pytest.mark.parametrize("file_name, expected", [
    ("clip.mid", "SOURCE MIDI"),
    ("song.mp3", "SOURCE MP3"),
    ("song.m4a", "SOURCE VIDEO"),
    ("song.M4A", "SOURCE VIDEO"),
])
def test_create_source_formats(file_name, expected):
    assert ReaperBackend()._create_source(file_name).name == expected


def make_show(file_path=None):
    return Show(
        audio_tracks=[
            AudioTrack(
                name="Song",
                duration="0:10",
                file_path=file_path,
                events={EffectType.LIGHTS: []},
            )
        ],
        effects={EffectType.LIGHTS: [Effect(id="a", name="a", note=1)]},
    )


def test_create_project_writes_rpp(tmp_path):
    midi_path = tmp_path / "Song_lights.mid"
    midi_path.write_bytes(b"")
    midi_files = {"Song": {EffectType.LIGHTS: {"file_path": midi_path, "duration": 5.0}}}

    backend = ReaperBackend()
    backend.create_project(make_show(), midi_files, tmp_path)
    rpp_path = tmp_path / "test.rpp"
    backend.save_project(rpp_path)

    content = rpp_path.read_text()
    assert 'MARKER 0 0 "Song"' in content
    assert 'NAME "lights"' in content
    assert "SOURCE MIDI" in content


def test_missing_media_raises(tmp_path):
    show = make_show(file_path=str(tmp_path / "missing.mp3"))
    backend = ReaperBackend()
    with pytest.raises(FileNotFoundError, match="missing.mp3"):
        backend.create_project(show, {}, tmp_path)
