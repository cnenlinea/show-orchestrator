import shutil
from pathlib import Path

import pytest

from web import create_app

REPO_ROOT = Path(__file__).parent.parent


@pytest.fixture
def root(tmp_path):
    shutil.copy(REPO_ROOT / "example.yaml", tmp_path / "example.yaml")
    (tmp_path / "shows" / "demo").mkdir(parents=True)
    (tmp_path / "shows" / "demo" / "demo.csv").write_text(
        "name,type,timestamp,duration,note,file\n"
        "Song,audio,,1:00,,\n"
        "Blackout,lights,0:00,,2,\n"
        "Fade,lights,0:10,5,,\n"
        "Blackout,lights,0:30,,,\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def client(root):
    return create_app(root).test_client()


def test_index_renders(client):
    response = client.get("/")

    assert response.status_code == 200
    assert b"Show Orchestrator" in response.data


def test_lists_show_files(client):
    assert client.get("/api/shows").get_json() == {
        "shows": ["example.yaml", "shows/demo/demo.csv"]
    }


def test_show_describes_songs_and_effects(client):
    show = client.get("/api/show?file=shows/demo/demo.csv").get_json()

    assert show["duration"] == 60.0
    [song] = show["songs"]
    assert [(cue["time"], cue["effect"], cue["note"]) for cue in song["cues"]] == [
        (0.0, "Blackout", 2),
        (10.0, "Fade", 0),
        (30.0, "Blackout", 2),
    ]
    assert [(e["name"], e["note"], e["cues"]) for e in show["effects"]] == [
        ("Blackout", 2, 2),
        ("Fade", 0, 1),
    ]


def test_show_rejects_files_outside_the_listing(client):
    assert client.get("/api/show?file=../secret.csv").status_code == 404


def test_show_reports_parse_errors(client, root):
    (root / "shows" / "demo" / "broken.csv").write_text("Fade,lights,0:10,,,\n", encoding="utf-8")

    response = client.get("/api/show?file=shows/demo/broken.csv")

    assert response.status_code == 400
    assert "before any audio row" in response.get_json()["error"]


def test_build_writes_midi_and_project(client, root):
    response = client.post("/api/build", json={
        "file": "shows/demo/demo.csv",
        "output_dir": "out",
        "orchestrate": "reaper",
    })

    assert response.status_code == 200
    assert response.get_json()["midi_files"] == ["Song_lights.mid"]
    assert (root / "out" / "Song_lights.mid").exists()
    assert (root / "out" / "demo.rpp").exists()


def test_build_rejects_bad_velocity(client):
    response = client.post("/api/build", json={"file": "shows/demo/demo.csv", "velocity": 200})

    assert response.status_code == 400


def test_source_round_trips_and_keeps_line_endings(client, root):
    file = root / "shows" / "demo" / "demo.csv"
    file.write_bytes(b"Song,audio,,1:00,,\r\nFade,lights,0:10,,,\r\n")

    text = client.get("/api/source?file=shows/demo/demo.csv").get_json()["text"]
    assert text == "Song,audio,,1:00,,\nFade,lights,0:10,,,\n"

    response = client.put("/api/source", json={
        "file": "shows/demo/demo.csv",
        "text": text + "Strobe,lights,0:20,,,\n",
    })

    assert response.status_code == 200
    assert file.read_bytes() == (
        b"Song,audio,,1:00,,\r\nFade,lights,0:10,,,\r\nStrobe,lights,0:20,,,\r\n"
    )


def test_source_that_would_not_load_is_not_saved(client, root):
    file = root / "shows" / "demo" / "demo.csv"
    before = file.read_bytes()

    response = client.put("/api/source", json={
        "file": "shows/demo/demo.csv",
        "text": "Fade,lights,0:10,,,\n",
    })

    assert response.status_code == 400
    assert "before any audio row" in response.get_json()["error"]
    assert file.read_bytes() == before


def test_document_describes_a_csv_show(client):
    document = client.get("/api/document?file=shows/demo/demo.csv").get_json()

    assert document["format"] == "csv"
    assert document["preamble"] == [["name", "type", "timestamp", "duration", "note", "file"]]
    [song] = document["songs"]
    assert (song["name"], song["duration"]) == ("Song", "1:00")
    assert [(row["time"], row["effect"]) for row in song["rows"]] == [
        ("0:00", "Blackout"), ("0:10", "Fade"), ("0:30", "Blackout"),
    ]
    assert [(e["name"], e["note"]) for e in document["effects"]] == [("Blackout", 2), ("Fade", None)]


def test_document_save_rewrites_the_show(client, root):
    document = client.get("/api/document?file=shows/demo/demo.csv").get_json()
    document["songs"][0]["rows"].append(
        {"kind": "cue", "type": "projection", "time": "45", "duration": "", "effect": "Stars"}
    )
    document["effects"].append(
        {"type": "projection", "id": "Stars", "name": "Stars", "note": 7, "channel": None}
    )

    response = client.put("/api/document", json={"file": "shows/demo/demo.csv", "document": document})

    assert response.status_code == 200
    assert (root / "shows" / "demo" / "demo.csv").read_text(encoding="utf-8") == (
        "name,type,timestamp,duration,note,file\n"
        "Song,audio,,1:00,,\n"
        "Blackout,lights,0:00,,2,\n"
        "Fade,lights,0:10,5,,\n"
        "Blackout,lights,0:30,,2,\n"
        "Stars,projection,0:45,,7,\n"
    )


def test_document_that_would_not_load_is_not_saved(client, root):
    file = root / "shows" / "demo" / "demo.csv"
    before = file.read_bytes()
    document = client.get("/api/document?file=shows/demo/demo.csv").get_json()
    document["songs"][0]["duration"] = "a minute"

    response = client.put("/api/document", json={"file": "shows/demo/demo.csv", "document": document})

    assert response.status_code == 400
    assert file.read_bytes() == before


def test_lists_media_files(client, root):
    (root / "shows" / "demo" / "song.MP3").write_bytes(b"")

    assert client.get("/api/media").get_json() == {"files": ["shows/demo/song.MP3"]}
