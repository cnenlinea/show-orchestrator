"""The files a show keeps besides its definition: its MadMapper project, and the lighting ReQLC publishes."""
import io
import json
import zipfile

import mido
import pytest

from tests.test_web import client, root  # noqa: F401  (fixtures)
from web import create_app

WORKSPACE = (b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE Workspace>\n'
             b'<Workspace xmlns="http://www.qlcplus.org/Workspace" CreatorName="Q Light Controller Plus"/>\n')
FIXTURE = b'<?xml version="1.0"?>\n<FixtureDefinition xmlns="http://www.qlcplus.org/FixtureDefinition"/>\n'


def midi_bytes(note=60):
    track = mido.MidiTrack([mido.Message("note_on", note=note, velocity=100, time=0),
                            mido.Message("note_off", note=note, velocity=0, time=480)])
    out = io.BytesIO()
    mido.MidiFile(tracks=[track]).save(file=out)
    return out.getvalue()


# --- the MadMapper project

@pytest.fixture
def mad_show(root):  # noqa: F811
    (root / "shows" / "demo" / "mad.csv").write_text(
        "name,type,timestamp,duration,note,file\n"
        "Song,audio,,1:00,,\n"
        "Intro,projection,0:00,,5,\n"
        "Outro,projection,0:50,,6,\n",
        encoding="utf-8",
    )
    return "shows/demo/mad.csv"


def mad(*cues):
    from tests.test_madmapper import bank, project

    return project([bank("Show", *cues)]).to_bytes()


def keep(client, show, name, content):  # noqa: F811
    return client.post("/api/madmapper/project", data={"show": show, "project": (io.BytesIO(content), name)},
                       content_type="multipart/form-data")


def patch(client, show, **form):  # noqa: F811
    return client.post("/api/madmapper", data={"show": show, **form}, content_type="multipart/form-data")


def test_a_show_keeps_its_madmapper_project_and_patches_it(client, root, mad_show):  # noqa: F811
    from show_orchestrator.madfile import MadProject
    from tests.test_madmapper import triggers

    assert client.get(f"/api/madmapper/project?show={mad_show}").get_json() == {"project": None, "previous": []}
    assert patch(client, mad_show).status_code == 400  # nothing to patch yet

    kept = keep(client, mad_show, "Show.mad", mad("Intro", "Outro"))
    assert kept.status_code == 200, kept.get_json()
    project = kept.get_json()["project"]
    assert project["name"] == "Show.mad" and project["size"] > 0 and project["uploaded_at"]
    # shows sharing a folder each get their own
    assert (root / "shows/demo/mad/madmapper/Show.mad").is_file()

    patched = patch(client, mad_show)
    assert patched.status_code == 200, patched.get_json()
    result = patched.get_json()
    assert result["project"] == "Show.mad"
    assert [row["cue"] for row in result["patched"]] == ["Intro", "Outro"]
    downloaded = MadProject.from_bytes(client.get(result["download"]).data)
    assert dict(triggers(downloaded))["/cues/Show/cues/by_name/Intro"] == "/midiin/all/1/F-2"

    original = client.get(f"/api/madmapper/project/download?show={mad_show}")
    assert original.data == mad("Intro", "Outro")  # kept as it was uploaded


def test_a_new_project_replaces_the_old_which_is_kept(client, mad_show):  # noqa: F811
    from show_orchestrator.showfiles import KEEP_PREVIOUS

    keep(client, mad_show, "Show.mad", mad("Intro"))
    for _ in range(KEEP_PREVIOUS + 1):
        info = keep(client, mad_show, "Show_midi.mad", mad("Intro", "Outro")).get_json()
    assert info["project"]["name"] == "Show_midi.mad"
    assert len(info["previous"]) == KEEP_PREVIOUS
    assert all(p["name"] == "Show_midi.mad" for p in info["previous"])  # the oldest (Show.mad) went first

    oldest = info["previous"][-1]["id"]
    assert client.get(f"/api/madmapper/project/download?show={mad_show}&previous={oldest}").status_code == 200
    assert client.get(f"/api/madmapper/project/download?show={mad_show}&previous=nope").status_code == 404


def test_a_patched_project_uploaded_again_keeps_its_name(client, mad_show):  # noqa: F811
    keep(client, mad_show, "Show_midi.mad", mad("Intro"))
    assert patch(client, mad_show).get_json()["file"] == "Show_midi.mad"  # not Show_midi_midi.mad


def test_what_is_not_a_project_is_not_kept(client, mad_show):  # noqa: F811
    keep(client, mad_show, "Show.mad", mad("Intro"))
    refused = keep(client, mad_show, "Other.mad", b"not a project")
    assert refused.status_code == 400
    assert "not a MadMapper project" in refused.get_json()["error"]
    assert client.get(f"/api/madmapper/project?show={mad_show}").get_json()["project"]["name"] == "Show.mad"


def test_patching_a_project_sent_along_keeps_it_unless_previewed(client, mad_show):  # noqa: F811
    form = lambda name: {"project": (io.BytesIO(mad("Intro")), name)}  # noqa: E731
    assert patch(client, mad_show, dry_run="1", **form("Preview.mad")).status_code == 200
    assert client.get(f"/api/madmapper/project?show={mad_show}").get_json()["project"] is None
    assert patch(client, mad_show, fades="Nope=1", **form("Broken.mad")).status_code == 400
    assert client.get(f"/api/madmapper/project?show={mad_show}").get_json()["project"] is None
    assert patch(client, mad_show, **form("Sent.mad")).status_code == 200
    assert client.get(f"/api/madmapper/project?show={mad_show}").get_json()["project"]["name"] == "Sent.mad"


# --- lighting

@pytest.fixture
def lit_show(root, monkeypatch):  # noqa: F811
    monkeypatch.chdir(root)  # media paths are relative to the working directory, which the app's root is
    folder = root / "shows" / "lit"
    (folder / "luces_midi").mkdir(parents=True)
    (folder / "luces_midi" / "MS 1 Intro.mid").write_bytes(midi_bytes(1))
    (folder / "lit.yaml").write_text(
        "effects:\n"
        "  projection:\n"
        "  - {id: Stars, name: Stars}\n"
        "audio_tracks:\n"
        "- name: Intro\n"
        "  duration: '1:00'\n"
        "  events:\n"
        "    projection:\n"
        "    - {effect_id: Stars, timestamp: '0:00'}\n"
        "  extra_tracks:\n"
        "  - {name: Luces, timestamp: '0:00', duration: '1:00', file_path: shows/lit/luces_midi/MS 1 Intro.mid}\n"
        "- name: Outro\n"
        "  duration: '0:30'\n"
        "  events: {}\n"
        "  extra_tracks:\n"
        "  - {name: Luces, timestamp: '0:00', duration: '0:30', file_path: shows/lit/luces_midi/MS 2 Outro.mid}\n",
        encoding="utf-8",
    )
    return "shows/lit/lit.yaml"


@pytest.fixture
def program(root):  # noqa: F811
    """The app with a login, as on the server, and a token for ReQLC."""
    return create_app(root, users={"fer": "pw"}, tokens={"reqlc": "s3cret"}).test_client()


TOKEN = {"Authorization": "Bearer s3cret"}


def publish(client, show, midi=(), fixtures=(), workspace=("Show.qxw", WORKSPACE), headers=TOKEN, **form):  # noqa: F811
    data = {"manifest": json.dumps({"source": "reqlc", "show": "me-sostendras", "version": 7}), **form}
    if workspace:
        data["workspace"] = (io.BytesIO(workspace[1]), workspace[0])
    data["midi"] = [(io.BytesIO(content), name) for name, content in midi]
    data["fixture"] = [(io.BytesIO(content), name) for name, content in fixtures]
    return client.put(f"/api/shows/{show}/lighting", data=data, headers=headers, content_type="multipart/form-data")


def test_reqlc_publishes_the_lighting(program, root, lit_show):
    before = program.get("/api/shows/lit/lighting", headers=TOKEN).get_json()
    assert before["workspace"] is None and before["manifest"] is None
    assert [(m["name"], m["songs"], m["present"]) for m in before["midi"]] == [
        ("MS 1 Intro.mid", ["Intro"], True), ("MS 2 Outro.mid", ["Outro"], False)]

    sent = publish(program, "lit", midi=[("MS 1 Intro.mid", midi_bytes(1)), ("MS 2 Outro.mid", midi_bytes(2)),
                                         ("MS Ensayo.mid", midi_bytes(3))],
                   fixtures=[("Robotica.qxf", FIXTURE)])
    assert sent.status_code == 200, sent.get_json()
    report = sent.get_json()
    assert report["workspace"] == {"name": "Show.qxw", "status": "new"}
    assert [(m["name"], m["status"]) for m in report["midi"]] == [
        ("MS 1 Intro.mid", "unchanged"), ("MS 2 Outro.mid", "new"), ("MS Ensayo.mid", "unused")]
    assert report["missing"] == []
    assert report["manifest"]["version"] == 7 and report["manifest"]["published_by"] == "reqlc"

    assert (root / "shows/lit/luces_midi/MS 2 Outro.mid").read_bytes() == midi_bytes(2)  # where the show has it
    assert (root / "shows/lit/lighting/Show.qxw").read_bytes() == WORKSPACE
    assert (root / "shows/lit/lighting/fixtures/Robotica.qxf").is_file()
    assert (root / "shows/lit/lighting/midi/MS Ensayo.mid").is_file()  # kept, though the show doesn't play it

    after = program.get("/api/shows/shows/lit/lit.yaml/lighting", headers=TOKEN).get_json()  # by file too
    assert after["workspace"]["name"] == "Show.qxw"
    assert all(m["present"] for m in after["midi"])
    assert [m["name"] for m in after["unused_midi"]] == ["MS Ensayo.mid"]
    assert after["manifest"]["source"] == "reqlc"
    assert program.get("/api/shows/lit/lighting/workspace", headers=TOKEN).data == WORKSPACE


def test_a_new_publish_keeps_the_set_it_replaces(program, root, lit_show):
    publish(program, "lit", midi=[("MS 1 Intro.mid", midi_bytes(1))], fixtures=[("Old.qxf", FIXTURE)])
    report = publish(program, "lit", midi=[("MS 1 Intro.mid", midi_bytes(9))],
                     workspace=("Show v2.qxw", WORKSPACE.replace(b"Plus", b"Plus 2"))).get_json()
    assert report["workspace"]["status"] == "changed"
    assert report["removed_fixtures"] == ["Old.qxf"]
    assert [(m["name"], m["status"]) for m in report["midi"]] == [("MS 1 Intro.mid", "changed")]
    assert [m["name"] for m in report["missing"]] == ["MS 2 Outro.mid"]

    lighting = root / "shows/lit/lighting"
    assert [p.name for p in lighting.glob("*.qxw")] == ["Show v2.qxw"]
    assert not (lighting / "fixtures" / "Old.qxf").exists()
    previous = lighting / ".previous"
    assert (previous / "Show.qxw").read_bytes() == WORKSPACE
    assert (previous / "fixtures" / "Old.qxf").is_file()
    assert (previous / "replaced/shows/lit/luces_midi/MS 1 Intro.mid").read_bytes() == midi_bytes(1)
    # the copies kept aside aren't offered as media for the show
    assert not [f for f in program.get("/api/media", headers=TOKEN).get_json().get("files", []) if ".previous" in f]


def test_a_dry_run_changes_nothing(program, root, lit_show):
    report = publish(program, "lit", midi=[("MS 2 Outro.mid", midi_bytes(2))], dry_run="1").get_json()
    assert report["dry_run"] and report["midi"][0]["status"] == "new"
    assert not (root / "shows/lit/luces_midi/MS 2 Outro.mid").exists()
    assert not (root / "shows/lit/lighting").exists()


@pytest.mark.parametrize("change, message", [
    ({"workspace": None}, "No workspace sent"),
    ({"workspace": ("Show.qxw", b"<Workspace")}, "not a QLC+ workspace"),
    ({"workspace": ("Show.qxw", b"<Other/>")}, "not a QLC+ workspace"),
    ({"workspace": ("Show.txt", WORKSPACE)}, ".qxw"),
    ({"midi": [("MS 1 Intro.mid", b"not midi")]}, "not a MIDI file"),
    ({"midi": [("notes.txt", b"x")]}, "only MIDI files"),
    ({"midi": [("A.mid", midi_bytes()), ("a.mid", midi_bytes())]}, "sent twice"),
    ({"fixtures": [("Robotica.qxf", b"<Workspace/>")]}, "not a QLC+ fixture definition"),
    ({"manifest": "[1]"}, "JSON object"),
])
def test_a_publish_that_does_not_check_out_changes_nothing(program, root, lit_show, change, message):
    response = publish(program, "lit", **change)
    assert response.status_code == 400
    assert message in response.get_json()["error"]
    assert not (root / "shows/lit/lighting").exists()
    assert (root / "shows/lit/luces_midi/MS 1 Intro.mid").read_bytes() == midi_bytes(1)


def test_a_token_opens_the_lighting_and_media_only(program, lit_show):
    assert program.get("/api/shows/lit/lighting").status_code == 401  # no token, no login
    assert program.get("/api/shows/lit/lighting", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert program.get("/api/shows", headers=TOKEN).status_code == 401
    assert program.get("/api/source?file=shows/lit/lit.yaml", headers=TOKEN).status_code == 401
    assert program.get("/api/shows/nope/lighting", headers=TOKEN).status_code == 404
    # people logged in may use it too
    program.post("/login", data={"user": "fer", "password": "pw"})
    assert program.get("/api/shows/lit/lighting").status_code == 200
    assert publish(program, "lit", headers={}).get_json()["manifest"]["published_by"] == "fer"


def test_reqlc_reads_the_shows_media(program, root, lit_show):
    media = program.get("/api/shows/lit/media", headers=TOKEN).get_json()["files"]
    assert [(m["song"], m["track"], m["kind"], m["present"]) for m in media] == [
        ("Intro", "Luces", "extra", True), ("Outro", "Luces", "extra", False)]
    path = media[0]["path"]
    assert program.get(f"/api/shows/lit/media/file?path={path}", headers=TOKEN).data == midi_bytes(1)
    assert program.get("/api/shows/lit/media/file?path=shows/lit/lit.yaml", headers=TOKEN).status_code == 404


# --- the bundle

def test_the_bundle_has_the_madmapper_project_lighting_and_a_readme(program, root, lit_show):
    from show_orchestrator.madfile import MadProject

    program.post("/login", data={"user": "fer", "password": "pw"})
    publish(program, "lit", midi=[("MS 1 Intro.mid", midi_bytes(1)), ("MS 2 Outro.mid", midi_bytes(2))],
            fixtures=[("Robotica.qxf", FIXTURE)])
    keep(program, lit_show, "Show.mad", mad("Stars"))
    made = program.post("/api/bundle", json={"file": lit_show})
    assert made.status_code == 200, made.get_json()
    assert made.get_json()["warnings"] == []
    with zipfile.ZipFile(io.BytesIO(program.get(made.get_json()["download"]).data)) as bundle:
        names = set(bundle.namelist())
        assert {"lit/READ ME FIRST.txt", "lit/lit.rpp", "lit/shows/lit/lighting/Show.qxw",
                "lit/shows/lit/lighting/fixtures/Robotica.qxf", "lit/shows/lit/lighting/manifest.json",
                "lit/shows/lit/luces_midi/MS 2 Outro.mid", "lit/madmapper/Show_midi.mad"} <= names
        assert not any(".madmapper" in n or ".previous" in n for n in names)
        assert "Extract the whole zip first" in bundle.read("lit/READ ME FIRST.txt").decode()
        patched = MadProject.from_bytes(bundle.read("lit/madmapper/Show_midi.mad"))
    from tests.test_madmapper import triggers
    assert len(triggers(patched)) == 1  # Stars got its note


def test_a_bundle_with_a_project_that_will_not_patch_says_so(program, root, lit_show):
    program.post("/login", data={"user": "fer", "password": "pw"})
    keep(program, lit_show, "Show.mad", mad("Stars"))
    (root / "shows/lit/luces_midi/MS 2 Outro.mid").write_bytes(midi_bytes(2))
    (root / "shows/lit/lit.yaml").write_text(
        (root / "shows/lit/lit.yaml").read_text(encoding="utf-8") + "madmapper:\n  fades:\n    Nope: 1\n",
        encoding="utf-8")
    made = program.post("/api/bundle", json={"file": lit_show}).get_json()
    assert "could not be patched" in made["warnings"][0]
    with zipfile.ZipFile(io.BytesIO(program.get(made["download"]).data)) as bundle:
        assert "lit/madmapper/Show.mad" in bundle.namelist()  # as uploaded


def madproject_zip(*cues, video=b"video"):
    """A MadMapper 6 project folder, zipped as the Finder does, with a video in its Media folder."""
    from tests.test_madmapper import shared_timeline_project

    sent = io.BytesIO()
    with zipfile.ZipFile(sent, "w") as archive:
        archive.writestr("Musical.madproject/Musical.mad", shared_timeline_project(*cues).to_bytes())
        archive.writestr("Musical.madproject/Media/clip.mp4", video)
        archive.writestr("Musical.madproject/Media/Materials/look.png", b"png")
        archive.writestr("__MACOSX/Musical.madproject/._Musical.mad", b"fork")
    return sent.getvalue()


@pytest.mark.parametrize("include_videos", [False, True])
def test_the_bundle_has_the_madmapper_videos_only_when_asked(program, root, lit_show, include_videos):
    program.post("/login", data={"user": "fer", "password": "pw"})
    (root / "shows/lit/luces_midi/MS 2 Outro.mid").write_bytes(midi_bytes(2))
    assert keep(program, lit_show, "Musical.madproject.zip", madproject_zip("Stars")).status_code == 200
    made = program.post("/api/bundle", json={"file": lit_show, "include_videos": include_videos}).get_json()
    assert made["download"].endswith("lit_with_videos.zip" if include_videos else "lit.zip")
    with zipfile.ZipFile(io.BytesIO(program.get(made["download"]).data)) as bundle:
        names = set(bundle.namelist())
        readme = bundle.read("lit/READ ME FIRST.txt").decode()
    folder = "lit/madmapper/Musical_midi.madproject/"
    # the project as a folder, ready to open, rather than a zip in the zip
    assert {folder + "Musical_midi.mad", folder + "Media/Materials/look.png"} <= names
    assert not any("__MACOSX" in name for name in names)
    assert (folder + "Media/clip.mp4" in names) == include_videos
    assert ("without its 1 videos" in readme and "clip.mp4" in readme) != include_videos


# --- uploads in pieces (for files larger than a proxy lets through in one request)

def send_in_pieces(client, name, data, piece=4):  # noqa: F811
    started = client.post("/api/chunks", json={"name": name, "size": len(data)})
    assert started.status_code == 200, started.get_json()
    chunk = started.get_json()["id"]
    for offset in range(0, len(data), piece):
        sent = client.put(f"/api/chunks/{chunk}?offset={offset}", data=data[offset:offset + piece])
        assert sent.status_code == 200, sent.get_json()
    return chunk


def test_a_large_file_arrives_in_pieces(client, root):  # noqa: F811
    chunk = send_in_pieces(client, "big song.mp3", b"0123456789abcdef!")
    response = client.post("/api/upload", data={"show": "shows/demo/demo.csv", "file_chunk": chunk},
                           content_type="multipart/form-data")
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["saved"] == ["shows/demo/media/big song.mp3"]
    assert (root / "shows/demo/media/big song.mp3").read_bytes() == b"0123456789abcdef!"
    assert not list((root / "build" / "chunks").iterdir())  # used up


def test_a_piece_sent_again_or_out_of_order(client):  # noqa: F811
    chunk = client.post("/api/chunks", json={"name": "a.mp3", "size": 8}).get_json()["id"]
    assert client.put(f"/api/chunks/{chunk}?offset=0", data=b"abcd").get_json()["received"] == 4
    assert client.put(f"/api/chunks/{chunk}?offset=0", data=b"abcd").get_json()["received"] == 4  # a retry
    skipped = client.put(f"/api/chunks/{chunk}?offset=6", data=b"gh")
    assert skipped.status_code == 409 and skipped.get_json()["received"] == 4
    assert client.put(f"/api/chunks/{chunk}?offset=4", data=b"efghi").status_code == 400  # more than its size
    # not complete yet: refused by the upload it is for
    response = client.post("/api/upload", data={"show": "shows/demo/demo.csv", "file_chunk": chunk},
                           content_type="multipart/form-data")
    assert response.status_code == 400 and "not arrived completely" in response.get_json()["error"]


def test_a_piece_too_large_is_refused(client, monkeypatch):  # noqa: F811
    import web

    monkeypatch.setattr(web, "MAX_CHUNK_BYTES", 4)
    chunk = client.post("/api/chunks", json={"name": "a.mp3", "size": 8}).get_json()["id"]
    assert client.put(f"/api/chunks/{chunk}?offset=0", data=b"abcdefgh").status_code == 413


@pytest.mark.parametrize("body", [{}, {"name": "a.mp3"}, {"name": "a.mp3", "size": 0}, {"name": "", "size": 3},
                                  {"name": "a.mp3", "size": 3 * 1024**3}])
def test_a_chunked_upload_needs_a_name_and_size(client, body):  # noqa: F811
    assert client.post("/api/chunks", json=body).status_code == 400


def test_pieces_belong_to_whoever_sent_them(root):  # noqa: F811
    app = create_app(root, users={"fer": "pw", "ana": "pw2"})
    fer, ana = app.test_client(), app.test_client()
    fer.post("/login", data={"user": "fer", "password": "pw"})
    ana.post("/login", data={"user": "ana", "password": "pw2"})
    chunk = send_in_pieces(fer, "a.mp3", b"abcd")
    assert ana.put(f"/api/chunks/{chunk}?offset=0", data=b"abcd").status_code == 404
    response = ana.post("/api/upload", data={"show": "shows/demo/demo.csv", "file_chunk": chunk},
                        content_type="multipart/form-data")
    assert response.status_code == 400
    assert fer.put("/api/chunks/../../example.yaml?offset=0", data=b"x").status_code == 404


def test_a_madmapper_project_arrives_in_pieces(client, mad_show):  # noqa: F811
    chunk = send_in_pieces(client, "Musical.madproject.zip", madproject_zip("Intro"), piece=1000)
    kept = client.post("/api/madmapper/project", data={"show": mad_show, "project_chunk": chunk},
                       content_type="multipart/form-data")
    assert kept.status_code == 200, kept.get_json()
    assert kept.get_json()["project"]["name"] == "Musical.madproject.zip"
    assert patch(client, mad_show).get_json()["file"] == "Musical_midi.madproject.zip"
