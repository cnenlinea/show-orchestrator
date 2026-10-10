from pathlib import Path

import pytest

from show_orchestrator.editor import read_document, render_document
from show_orchestrator.parser import Parser

REPO_ROOT = Path(__file__).parent.parent


def load(tmp_path, text, suffix):
    file = tmp_path / f"show{suffix}"
    file.write_text(text, encoding="utf-8")
    return Parser().load_show(file)


@pytest.mark.parametrize("name", ["example.csv", "example.yaml"])
def test_round_trip_keeps_the_show(tmp_path, name):
    source = REPO_ROOT / name
    text = source.read_text(encoding="utf-8")

    rendered = render_document(read_document(text, source.suffix), source.suffix)

    assert load(tmp_path, rendered, source.suffix) == Parser().load_show(source)


def test_csv_keeps_header_ignored_lines_and_row_order():
    text = (
        "Nombre,Tipo,Timestamp,Duracion,Nota,Archivo\n"
        "Song,audio,,1:00,,song.mp3\n"
        "Clip,extra track,0:00,0:30,,clip.mid\n"
        "remember the fog,todo,,,,\n"
        "Fade,lights,0:10,5,4,\n"
    )

    document = read_document(text, ".csv")

    assert [row["kind"] for row in document["songs"][0]["rows"]] == ["extra", "ignored", "cue"]
    assert render_document(document, ".csv") == text


def test_csv_writes_the_effect_note_on_every_cue():
    document = read_document("Song,audio,,1:00,,\nFade,lights,0:10,,,\nFade,lights,0:20,,,\n", ".csv")
    document["effects"][0]["note"] = 9

    assert render_document(document, ".csv") == (
        "Song,audio,,1:00,,\nFade,lights,0:10,,9,\nFade,lights,0:20,,9,\n"
    )


def test_csv_timestamp_in_seconds_becomes_mmss():
    document = read_document("Song,audio,,3:00,,\nFade,lights,0:10,,,\n", ".csv")
    document["songs"][0]["rows"][0]["time"] = "90.5"

    assert "Fade,lights,1:30.5,,," in render_document(document, ".csv")


def test_yaml_song_without_cues_still_loads(tmp_path):
    # Hand-written files often leave `events` out, which the Parser rejects.
    text = "audio_tracks:\n  - name: Quiet\n    duration: '1:00'\neffects: {}\n"

    rendered = render_document(read_document(text, ".yaml"), ".yaml")

    assert load(tmp_path, rendered, ".yaml").audio_tracks[0].events == {}


def test_yaml_keeps_ids_notes_and_channels(tmp_path):
    document = read_document((REPO_ROOT / "example.yaml").read_text(encoding="utf-8"), ".yaml")
    strobe = next(effect for effect in document["effects"] if effect["id"] == "strobe")
    strobe.update(name="Big Strobe", note=40, channel=2)

    show = load(tmp_path, render_document(document, ".yaml"), ".yaml")

    effect = next(effect for effect in show.effects["lights"] if effect.id == "strobe")
    assert (effect.name, effect.note, effect.channel) == ("Big Strobe", 40, 2)


@pytest.mark.parametrize("change, message", [
    (lambda doc: doc["songs"][0].update(name=""), "Every song needs a name"),
    (lambda doc: doc["songs"][0]["rows"][0].update(effect=""), "has no effect"),
    (lambda doc: doc["songs"][0]["rows"][0].update(type="lasers"), "unknown cue type"),
    (lambda doc: doc["effects"][0].update(note=128), "between 0 and 127"),
])
def test_render_rejects_incomplete_documents(change, message):
    document = read_document("Song,audio,,1:00,,\nFade,lights,0:10,,,\n", ".csv")
    change(document)

    with pytest.raises(ValueError, match=message):
        render_document(document, ".csv")
