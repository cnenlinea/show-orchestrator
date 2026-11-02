import pytest

from show_orchestrator.backends.reaper import ReaperBackend
from show_orchestrator.models import AudioTrack, Effect, EffectType, Show


@pytest.mark.parametrize("file_name, expected", [
    ("clip.mid", "SOURCE MIDI"),
    ("song.mp3", "SOURCE MP3"),
    ("song.m4a", "SOURCE VIDEO"),
    ("song.M4A", "SOURCE VIDEO"),
    ("song.mp3 (1).mpeg", "SOURCE VIDEO"),
    ("song.mpg", "SOURCE VIDEO"),
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


def test_media_is_referenced_relative_to_the_project(tmp_path):
    media_dir = tmp_path / "Pistas"
    media_dir.mkdir()
    (media_dir / "song.mp3").write_bytes(b"audio")
    output_dir = tmp_path / "build"
    output_dir.mkdir()
    midi_path = output_dir / "Song_lights.mid"
    midi_path.write_bytes(b"")
    midi_files = {"Song": {EffectType.LIGHTS: {"file_path": midi_path, "duration": 5.0}}}

    backend = ReaperBackend()
    backend.create_project(make_show(str(media_dir / "song.mp3")), midi_files, output_dir)
    backend.save_project(output_dir / "show.rpp")

    files = [line.strip() for line in (output_dir / "show.rpp").read_text().splitlines()
             if line.strip().startswith("FILE")]
    assert files == ['FILE "song.mp3"', 'FILE "Song_lights.mid"']
    # Everything the project names sits next to it, so the folder can move.
    moved = tmp_path / "elsewhere"
    output_dir.rename(moved)
    assert (moved / "song.mp3").read_bytes() == b"audio"
    assert (moved / "Song_lights.mid").exists()


def test_midi_outside_the_project_folder_keeps_an_absolute_path(tmp_path):
    midi_path = tmp_path / "other" / "Song_lights.mid"
    midi_path.parent.mkdir()
    midi_path.write_bytes(b"")
    output_dir = tmp_path / "build"
    output_dir.mkdir()
    midi_files = {"Song": {EffectType.LIGHTS: {"file_path": midi_path, "duration": 5.0}}}

    backend = ReaperBackend()
    backend.create_project(make_show(), midi_files, output_dir)
    backend.save_project(output_dir / "show.rpp")
    assert f'FILE "{midi_path.resolve()}"' in (output_dir / "show.rpp").read_text()


def test_missing_media_raises(tmp_path):
    show = make_show(file_path=str(tmp_path / "missing.mp3"))
    backend = ReaperBackend()
    with pytest.raises(FileNotFoundError, match="missing.mp3"):
        backend.create_project(show, {}, tmp_path)
