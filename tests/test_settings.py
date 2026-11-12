"""Show settings in YAML (reaper: gap and routing, madmapper: fades) and REAPER routing."""
import sys

import pytest
import yaml
from pydantic import ValidationError

import madmapper_patch
from show_orchestrator.backends.reaper import (
    DEFAULT_SONG_GAP, ReaperBackend, project_track_names, read_midi_routing, reaper_settings,
)
from show_orchestrator.editor import read_document, render_document
from show_orchestrator.madfile import MadProject
from show_orchestrator.madmapper import Cue, trigger_fade
from show_orchestrator.parser import Parser
from tests.test_madmapper import bank, project
from web import create_app

SHOW_YAML = """\
reaper:
  gap: 2
  routing:
    mac:
      projection: 64
      Luces: 96
madmapper:
  fades_from_audio: false
  fades:
    Intro: 2.5
audio_tracks:
- name: One
  duration: '0:10'
  events:
    projection:
    - timestamp: 0:00
      effect_id: Intro
  extra_tracks:
  - name: Luces
    timestamp: 0:00
    duration: '0:10'
    file_path: lights.mid
- name: Two
  duration: '0:10'
  events:
    projection:
    - timestamp: 0:00
      effect_id: Intro
effects:
  projection:
  - id: Intro
    name: Intro
    note: 3
    channel: 0
"""


@pytest.fixture
def show_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # file paths in a show are relative to where tools run
    (tmp_path / "lights.mid").write_bytes(b"")
    path = tmp_path / "show.yaml"
    path.write_text(SHOW_YAML, encoding="utf-8")
    return path


def build(show, tmp_path, **kwargs) -> str:
    output_dir = tmp_path / "build"
    output_dir.mkdir(exist_ok=True)
    backend = ReaperBackend()
    backend.create_project(show, {}, output_dir, **kwargs)
    backend.save_project(output_dir / "show.rpp")
    return (output_dir / "show.rpp").read_text()


def test_yaml_settings_load(show_file):
    show = Parser().load_show(show_file)
    assert show.reaper.gap == 2
    assert show.reaper.routing == {"mac": {"projection": 64, "Luces": 96}}
    assert show.madmapper.fades == {"Intro": 2.5}
    assert show.madmapper.fades_from_audio is False


@pytest.mark.parametrize("section, message", [
    ("reaper:\n  gpa: 1\n", "gpa"),  # a typo is an error, not silently ignored
    ("reaper:\n  gap: -1\n", "negative"),
    ("reaper:\n  routing:\n    mac:\n      projection: -2\n", "MIDIOUT"),
    ("madmapper:\n  fades:\n    Intro: -1\n", "negative"),
])
def test_bad_settings_are_refused(tmp_path, section, message):
    path = tmp_path / "show.yaml"
    path.write_text(section + "audio_tracks: []\neffects: {}\n", encoding="utf-8")
    with pytest.raises(ValidationError, match=message):
        Parser().load_show(path)


def test_gap_comes_from_the_flag_then_the_show_then_the_default(show_file):
    show = Parser().load_show(show_file)
    assert reaper_settings(show, gap=0.25) == (0.25, None)
    assert reaper_settings(show) == (2, None)
    show.reaper = None
    assert reaper_settings(show) == (DEFAULT_SONG_GAP, None)


def test_routing_profile_is_picked_by_name(show_file):
    show = Parser().load_show(show_file)
    assert reaper_settings(show, routing_profile="mac") == (2, {"projection": 64, "Luces": 96})
    with pytest.raises(ValueError, match="no routing profile 'pc'; its profiles: 'mac'"):
        reaper_settings(show, routing_profile="pc")


def test_routed_tracks_get_their_midi_output(show_file, tmp_path):
    show = Parser().load_show(show_file)
    rpp = build(show, tmp_path, routing={"projection": 64, "Luces": 96})
    assert read_midi_routing(rpp) == [("projection", 64), ("Luces", 96)]
    assert "MIDIOUT" not in build(show, tmp_path)  # no routing, nothing written


def test_routing_a_track_the_project_lacks_is_an_error(show_file, tmp_path):
    show = Parser().load_show(show_file)
    assert project_track_names(show) == ["projection", "Luces"]
    with pytest.raises(ValueError, match="tracks the project doesn't have: 'Luces - 1'"):
        build(show, tmp_path, routing={"Luces - 1": 64})


def test_routing_is_read_from_a_project_saved_by_reaper():
    rpp = """<REAPER_PROJECT 0.1 "7.53/macOS-arm64" 1794459455 0
  <TRACK {EF6C01BE}
    NAME "Audio Files"
    MIDIOUT -1
    <ITEM
      NAME "not a track name"
      <SOURCE MIDI
        FILE "x.mid"
      >
    >
  >
  <TRACK {F9E4C723}
    NAME projection
    MIDIOUT 64
  >
  <TRACK {1}
    NAME 'Say "hi"'
    MIDIOUT 97
  >
>
"""
    assert read_midi_routing(rpp) == [("Audio Files", -1), ("projection", 64), ('Say "hi"', 97)]


def test_editor_keeps_settings_sections_in_place():
    rendered = render_document(read_document(SHOW_YAML, ".yaml"), ".yaml")
    data = yaml.safe_load(rendered)
    assert list(data) == ["reaper", "madmapper", "audio_tracks", "effects"]
    assert data["reaper"] == yaml.safe_load(SHOW_YAML)["reaper"]
    assert data["madmapper"] == yaml.safe_load(SHOW_YAML)["madmapper"]


def test_web_build_uses_the_show_gap_and_a_routing_profile(show_file, tmp_path):
    (tmp_path / "shows").mkdir()
    show_file.rename(tmp_path / "shows" / "show.yaml")
    client = create_app(tmp_path).test_client()

    described = client.get("/api/show?file=shows/show.yaml").get_json()
    assert described["gap"] == 2 and described["routing_profiles"] == ["mac"]

    response = client.post("/api/build", json={
        "file": "shows/show.yaml", "output_dir": "out", "orchestrate": "reaper",
        "gap": "", "routing": "mac",
    })
    assert response.status_code == 200, response.get_json()
    rpp = (tmp_path / "out" / "show.rpp").read_text()
    assert 'MARKER 1 12.0 "Two"' in rpp  # the show's 2 s gap
    assert ("projection", 64) in read_midi_routing(rpp)

    response = client.post("/api/build", json={"file": "shows/show.yaml", "routing": "pc"})
    assert response.status_code == 400
    assert "no routing profile" in response.get_json()["error"]


def test_madmapper_patch_takes_fades_from_the_show(show_file, tmp_path, monkeypatch, capsys):
    mad_path = tmp_path / "Show.mad"
    project([bank("Show", "Intro")]).save(mad_path)
    output = tmp_path / "Show_midi.mad"
    monkeypatch.setattr(sys, "argv", ["madmapper_patch.py", str(show_file), str(mad_path),
                                      "-o", str(output), "--no-note-lock"])
    madmapper_patch.main()

    assert trigger_fade(MadProject.load(output), Cue("Show", "Intro")) == 2.5
    assert "Fades from the audio" not in capsys.readouterr().out  # fades_from_audio: false


def test_a_fade_flag_wins_over_the_show(show_file, tmp_path, monkeypatch):
    mad_path = tmp_path / "Show.mad"
    project([bank("Show", "Intro")]).save(mad_path)
    output = tmp_path / "Show_midi.mad"
    monkeypatch.setattr(sys, "argv", ["madmapper_patch.py", str(show_file), str(mad_path),
                                      "-o", str(output), "--no-note-lock", "--fade", "Intro=0.2"])
    madmapper_patch.main()
    assert trigger_fade(MadProject.load(output), Cue("Show", "Intro")) == 0.2
