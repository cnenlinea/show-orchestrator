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
    return create_app(root, users={}).test_client()


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


def test_build_puts_the_gap_between_songs(client, root):
    (root / "shows" / "demo" / "demo.csv").write_text(
        "One,audio,,0:10,,\nFade,lights,0:01,,,\nTwo,audio,,0:10,,\nFade,lights,0:01,,,\n"
    )
    response = client.post("/api/build", json={
        "file": "shows/demo/demo.csv", "output_dir": "out", "orchestrate": "reaper", "gap": "2",
    })
    assert response.status_code == 200
    assert 'MARKER 1 12.0 "Two"' in (root / "out" / "demo.rpp").read_text()


@pytest.mark.parametrize("gap", ["-1", "soon"])
def test_build_rejects_bad_gap(client, gap):
    response = client.post("/api/build", json={"file": "shows/demo/demo.csv", "gap": gap})
    assert response.status_code == 400
    assert "gap" in response.get_json()["error"]


# --- login

@pytest.fixture
def locked(root):
    return create_app(root, users={"ana": "s3cret"}).test_client()


def test_users_from_env():
    from web import users_from_env

    assert users_from_env({}) == {}
    assert users_from_env({"SHOW_ORCHESTRATOR_USERS": "ana:a:b, bo:x ,bad,:nope"}) == {"ana": "a:b", "bo": "x"}
    assert users_from_env({"SHOW_ORCHESTRATOR_PASSWORD": "pw"}) == {"show": "pw"}


def test_open_without_users(client):
    assert client.get("/").status_code == 200
    assert client.get("/api/shows").status_code == 200
    assert client.get("/login").status_code == 302


def test_login_required(locked):
    page = locked.get("/")
    assert page.status_code == 302 and "/login" in page.headers["Location"]
    api = locked.get("/api/shows")
    assert api.status_code == 401
    assert "WWW-Authenticate" not in api.headers
    assert locked.get("/login").status_code == 200


def test_login_and_logout(locked):
    wrong = locked.post("/login", data={"user": "ana", "password": "nope"})
    assert wrong.status_code == 401
    assert locked.get("/api/shows").status_code == 401

    ok = locked.post("/login", data={"user": "ana", "password": "s3cret", "next": "/?x=1"})
    assert ok.status_code == 302 and ok.headers["Location"] == "/?x=1"
    assert locked.get("/api/shows").status_code == 200
    assert b"Log out" in locked.get("/").data

    locked.get("/logout")
    assert locked.get("/api/shows").status_code == 401


@pytest.mark.parametrize("target", ["//evil.example", "https://evil.example", r"/\evil.example"])
def test_login_never_redirects_off_site(locked, target):
    response = locked.post("/login", data={"user": "ana", "password": "s3cret", "next": target})
    assert response.headers["Location"] == "/"


def test_basic_auth_for_scripts(locked):
    import base64

    def basic(user, password):
        return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}

    assert locked.get("/api/shows", headers=basic("ana", "s3cret")).status_code == 200
    assert locked.get("/api/shows", headers=basic("ana", "wrong")).status_code == 401


def test_changed_password_logs_out(root):
    first = create_app(root, users={"ana": "old"}).test_client()
    first.post("/login", data={"user": "ana", "password": "old"})
    cookie = first.get_cookie("session")
    later = create_app(root, users={"ana": "new"}).test_client()
    later.set_cookie("session", cookie.value)
    assert later.get("/api/shows").status_code == 401


# --- bundle

@pytest.fixture
def media_show(root, monkeypatch):
    # media paths in a show are relative to the working directory, which the app's root is
    monkeypatch.chdir(root)
    (root / "shows" / "demo" / "media").mkdir()
    (root / "shows" / "demo" / "media" / "song.mp3").write_bytes(b"ID3 not really audio")
    (root / "shows" / "demo" / "withmedia.csv").write_text(
        "name,type,timestamp,duration,note,file\n"
        "Song,audio,,1:00,,shows/demo/media/song.mp3\n"
        "Blackout,lights,0:00,,2,\n",
        encoding="utf-8",
    )
    return "shows/demo/withmedia.csv"


def test_bundle_holds_project_show_and_media(client, media_show):
    import io
    import zipfile

    made = client.post("/api/bundle", json={"file": media_show})
    assert made.status_code == 200, made.get_json()
    download = client.get(made.get_json()["download"])
    assert download.status_code == 200
    assert "withmedia.zip" in download.headers["Content-Disposition"]
    with zipfile.ZipFile(io.BytesIO(download.data)) as bundle:
        names = set(bundle.namelist())
        assert {
            "withmedia/withmedia.rpp",
            "withmedia/shows/demo/withmedia.csv",
            "withmedia/shows/demo/withmedia.notes.lock",
            "withmedia/shows/demo/media/song.mp3",
        } <= names
        assert any(name.endswith(".mid") and name.count("/") == 1 for name in names)
        assert "withmedia/song.mp3" not in names  # the media is stored once, where the show file says
        project = bundle.read("withmedia/withmedia.rpp").decode()
        assert "shows/demo/media/song.mp3" in project
        assert bundle.getinfo("withmedia/shows/demo/media/song.mp3").compress_type == zipfile.ZIP_STORED


def test_bundle_missing_media(client, media_show, root):
    (root / "shows" / "demo" / "media" / "song.mp3").unlink()
    response = client.post("/api/bundle", json={"file": media_show})
    assert response.status_code == 400
    assert "not found" in response.get_json()["error"]
    assert not list((root / "build" / "bundles").glob("*.part"))


def test_bundle_refuses_media_outside_root(client, media_show, root, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere") / "song.mp3"
    outside.write_bytes(b"x")
    (root / media_show).write_text(
        "name,type,timestamp,duration,note,file\n"
        f"Song,audio,,1:00,,{outside.as_posix()}\n",
        encoding="utf-8",
    )
    response = client.post("/api/bundle", json={"file": media_show})
    assert response.status_code == 400
    assert "outside the show folders" in response.get_json()["error"]


def test_bundle_download_only_serves_bundles(client):
    assert client.get("/api/bundle/..%2F..%2Fexample.yaml").status_code == 404


# --- upload

def send(client, show, *files, replace=False):
    import io

    data = {"show": show, "file": [(io.BytesIO(content), name) for name, content in files]}
    if replace:
        data["replace"] = "1"
    return client.post("/api/upload", data=data, content_type="multipart/form-data")


def test_upload_media_into_show_folder(client, root):
    response = send(client, "shows/demo/demo.csv", ("new song.mp3", b"one"), ("lights.csv", b"a,b\n"))
    assert response.status_code == 200
    assert response.get_json()["saved"] == ["shows/demo/media/new song.mp3", "shows/demo/lights.csv"]
    assert (root / "shows/demo/media/new song.mp3").read_bytes() == b"one"
    assert "shows/demo/media/new song.mp3" in client.get("/api/media").get_json()["files"]


def test_upload_does_not_replace_without_asking(client, root):
    send(client, "shows/demo/demo.csv", ("song.mp3", b"one"))
    again = send(client, "shows/demo/demo.csv", ("song.mp3", b"two"))
    assert again.status_code == 409
    assert again.get_json()["exists"] == ["shows/demo/media/song.mp3"]
    assert (root / "shows/demo/media/song.mp3").read_bytes() == b"one"
    assert send(client, "shows/demo/demo.csv", ("song.mp3", b"two"), replace=True).status_code == 200
    assert (root / "shows/demo/media/song.mp3").read_bytes() == b"two"


def test_upload_strips_folders_from_names(client, root):
    response = send(client, "shows/demo/demo.csv", ("../../evil.mp3", b"x"))
    assert response.get_json()["saved"] == ["shows/demo/media/evil.mp3"]
    assert not (root.parent / "evil.mp3").exists()


def test_upload_for_a_show_in_the_root(client, root):
    response = send(client, "example.yaml", ("a.wav", b"x"))
    assert response.get_json()["saved"] == ["shows/media/a.wav"]


@pytest.mark.parametrize("show, name, status", [
    ("shows/demo/demo.csv", "tool.exe", 400),
    ("shows/demo/demo.csv", ".mp3", 400),
    ("nope.csv", "a.mp3", 404),
])
def test_upload_refused(client, show, name, status):
    assert send(client, show, (name, b"x")).status_code == status


# --- MadMapper

@pytest.fixture
def mad_show(root):
    (root / "shows" / "demo" / "mad.csv").write_text(
        "name,type,timestamp,duration,note,file\n"
        "Song,audio,,1:00,,\n"
        "Intro,projection,0:00,,5,\n"
        "Outro,projection,0:50,,6,\n"
        "Missing,projection,0:55,,7,\n",
        encoding="utf-8",
    )
    return "shows/demo/mad.csv"


def mad_bytes(project):
    return project.to_bytes()


def patch(client, show, name, content, **form):
    import io

    data = {"show": show, "project": (io.BytesIO(content), name), **form}
    return client.post("/api/madmapper", data=data, content_type="multipart/form-data")


def test_madmapper_patches_a_mad_file(client, mad_show):
    from show_orchestrator.madfile import MadProject
    from tests.test_madmapper import bank, project, triggers

    response = patch(client, mad_show, "Show.mad", mad_bytes(project([bank("Show", "Intro", "Outro", "Other")])),
                     fades="Outro=0.5")
    assert response.status_code == 200, response.get_json()
    result = response.get_json()
    assert [(row["cue"], row["note"], row["note_name"]) for row in result["patched"]] == [
        ("Intro", 5, "F-2"), ("Outro", 6, "F#-2")]
    assert [row["effect"] for row in result["unmatched_effects"]] == ["Missing"]
    assert [row["cue"] for row in result["unmatched_cues"]] == ["Other"]
    assert result["hand_fades"] == [{"cue": "Outro", "bank": "Show", "old": None, "fade": 0.5}]
    assert result["file"] == "Show_midi.mad"  # MadMapper 5: a new project next to the original

    download = client.get(result["download"])
    assert download.status_code == 200
    patched = MadProject.from_bytes(download.data)
    assert dict(triggers(patched))["/cues/Show/cues/by_name/Intro"] == "/midiin/all/1/F-2"


def test_madmapper_preview_saves_nothing(client, mad_show, root):
    from tests.test_madmapper import bank, project

    response = patch(client, mad_show, "Show.mad", mad_bytes(project([bank("Show", "Intro")])), dry_run="1")
    assert response.status_code == 200
    assert response.get_json()["download"] is None
    assert not (root / "build" / "madmapper").exists()


def test_madmapper_6_mad_comes_back_under_its_own_name(client, mad_show):
    from tests.test_madmapper import shared_timeline_project

    response = patch(client, mad_show, "Musical.mad", mad_bytes(shared_timeline_project("Intro")), types="projection")
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["file"] == "Musical.mad"  # to put back into (a copy of) Musical.madproject


def test_madmapper_patches_a_zipped_madproject(client, mad_show):
    import io
    import zipfile

    from show_orchestrator.madfile import MadProject, T_STRING, get, set_value, to_plain
    from tests.test_madmapper import shared_timeline_project

    mad = shared_timeline_project("Intro", "Outro")
    set_value(mad.root, "projectPath", (T_STRING, "/Users/show/Musical.madproject", 0))
    sent = io.BytesIO()
    with zipfile.ZipFile(sent, "w") as archive:
        archive.writestr("Musical.madproject/Musical.mad", mad.to_bytes())
        archive.writestr("Musical.madproject/Media/clip.mp4", b"video")
        archive.writestr("Musical.madproject/Backup/Musical.backup.mad", b"old")
        archive.writestr("__MACOSX/Musical.madproject/._Musical.mad", b"fork")
    response = patch(client, mad_show, "Musical.madproject.zip", sent.getvalue())
    assert response.status_code == 200, response.get_json()
    result = response.get_json()
    assert result["file"] == "Musical_midi.madproject.zip"
    assert len(result["patched"]) == 2

    with zipfile.ZipFile(io.BytesIO(client.get(result["download"]).data)) as archive:
        names = {n for n in archive.namelist() if not n.endswith("/")}
        assert names == {"Musical_midi.madproject/Musical_midi.mad", "Musical_midi.madproject/Media/clip.mp4"}
        patched = MadProject.from_bytes(archive.read("Musical_midi.madproject/Musical_midi.mad"))
    # where madmapper_patch.py would have put it: next to the original
    assert to_plain(get(patched.root, "projectPath")) == "/Users/show/Musical_midi.madproject"


def test_madmapper_refuses_a_zip_that_escapes(client, mad_show):
    import io
    import zipfile

    sent = io.BytesIO()
    with zipfile.ZipFile(sent, "w") as archive:
        archive.writestr("../evil.mad", b"x")
    response = patch(client, mad_show, "evil.zip", sent.getvalue())
    assert response.status_code == 400
    assert "outside its folder" in response.get_json()["error"]


@pytest.mark.parametrize("name, content, form, message", [
    ("Show.txt", b"x", {}, ".mad file"),
    ("Show.mad", b"not a project", {}, "not a MadMapper project"),
    ("Show.zip", b"not a zip", {}, "not a zip"),
    ("Show.mad", None, {"fades": "Intro"}, "CUE=SECONDS"),
    ("Show.mad", None, {"fades": "Nope=1"}, "no cue of that name"),
    ("Show.mad", None, {"silence_threshold": "loud"}, "silence threshold"),
    ("Show.mad", None, {"types": "colour"}, "Unknown effect type"),
])
def test_madmapper_errors(client, mad_show, root, name, content, form, message):
    from tests.test_madmapper import bank, project

    content = content if content is not None else mad_bytes(project([bank("Show", "Intro")]))
    response = patch(client, mad_show, name, content, **form)
    assert response.status_code == 400
    assert message in response.get_json()["error"]
    assert not list((root / "build").rglob("*.mad"))


def test_show_reports_its_madmapper_settings(client, root):
    (root / "shows" / "demo" / "set.yaml").write_text(
        "madmapper:\n"
        "  fades_from_audio: true\n"
        "  fades:\n"
        "    Intro: 0.2\n"
        "effects:\n"
        "  projection:\n"
        "    - {id: Intro, name: Intro}\n"
        "audio_tracks:\n"
        "  - name: Song\n"
        "    duration: '1:00'\n"
        "    events:\n"
        "      projection:\n"
        "        - {effect_id: Intro, timestamp: '0:00'}\n",
        encoding="utf-8",
    )
    show = client.get("/api/show?file=shows/demo/set.yaml").get_json()
    assert show["madmapper"]["fades_from_audio"] is True
    assert show["madmapper"]["fades"] == {"Intro": 0.2}
    assert show["effect_types"] == ["projection"]
    assert client.get("/api/show?file=shows/demo/demo.csv").get_json()["madmapper"] == {}


# --- show settings in the editor (the YAML sections it otherwise keeps as they are)

@pytest.fixture
def yaml_show(root):
    (root / "shows" / "demo" / "lights.csv").write_text("name,type,timestamp,duration,note,file\nIntro,audio,,1:00,,\n",
                                                       encoding="utf-8")
    (root / "shows" / "demo" / "set.yaml").write_text(
        "reaper:\n"
        "  routing:\n"
        "    mac:\n"
        "      projection: 64\n"
        "effects:\n"
        "  projection:\n"
        "  - {id: Stars, name: Stars}\n"
        "audio_tracks:\n"
        "- name: Intro\n"
        "  duration: '1:00'\n"
        "  events:\n"
        "    projection:\n"
        "    - {effect_id: Stars, timestamp: '0:00'}\n"
        "- name: Escenografia\n"
        "  duration: '0:30'\n"
        "  events: {}\n",
        encoding="utf-8",
    )
    return "shows/demo/set.yaml"


def save_sections(client, show, change):
    document = client.get(f"/api/document?file={show}").get_json()
    change(document["sections"])
    return client.put("/api/document", json={"file": show, "document": document})


def test_settings_are_saved_into_the_yaml(client, root, yaml_show):
    from show_orchestrator.parser import Parser

    def change(sections):
        sections["include"] = ["lights.csv"]
        sections["reaper"].update(gap=1.5, default_routing="mac", no_markers=["Escenografia"],
                                  split_midi={"Letras ReWorship": "Letras"})
        sections["madmapper"] = {"fades_from_audio": True, "silence_threshold": -35, "fades": {"Stars": 0.2}}

    assert save_sections(client, yaml_show, change).status_code == 200
    show = Parser().load_show(root / yaml_show)
    assert show.include == ["lights.csv"]
    assert (show.reaper.gap, show.reaper.default_routing) == (1.5, "mac")
    assert show.reaper.no_markers == ["Escenografia"]
    assert show.reaper.split_midi == {"Letras ReWorship": "Letras"}
    assert show.reaper.routing == {"mac": {"projection": 64}}  # the profiles the settings don't edit are kept
    assert show.madmapper.fades == {"Stars": 0.2} and show.madmapper.silence_threshold == -35


def test_settings_removed_leave_the_yaml(client, root, yaml_show):
    assert save_sections(client, yaml_show, lambda sections: sections.pop("reaper")).status_code == 200
    assert "reaper" not in (root / yaml_show).read_text(encoding="utf-8")


@pytest.mark.parametrize("change, message", [
    (lambda s: s["reaper"].update(default_routing="pc"), "default_routing 'pc' is not one of the routing profiles"),
    (lambda s: s["reaper"].update(gap=-1), "gap"),
    (lambda s: s["reaper"].update(markers=["Intro"]), "markers"),
    (lambda s: s.update(madmapper={"fades": {"Stars": -2}}), "fades"),
])
def test_bad_settings_are_not_saved(client, root, yaml_show, change, message):
    before = (root / yaml_show).read_bytes()
    response = save_sections(client, yaml_show, change)
    assert response.status_code == 400
    assert message in response.get_json()["error"]
    assert (root / yaml_show).read_bytes() == before


def test_a_show_with_an_include_can_be_saved(client, root, yaml_show):
    # the check before saving loads the show next to the file, where its included files are
    (root / yaml_show).write_text((root / yaml_show).read_text(encoding="utf-8") + "include: [lights.csv]\n",
                                  encoding="utf-8")
    text = client.get(f"/api/source?file={yaml_show}").get_json()["text"]
    response = client.put("/api/source", json={"file": yaml_show, "text": text.replace("'1:00'", "'1:05'")})
    assert response.status_code == 200, response.get_json()
    assert not [p for p in (root / "shows" / "demo").iterdir() if ".check" in p.name]


def test_compose_and_hidden_files_are_not_shows(client, root):
    (root / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (root / "shows" / "demo" / ".demo.x.check.csv").write_text("x\n", encoding="utf-8")
    shows = client.get("/api/shows").get_json()["shows"]
    assert "compose.yaml" not in shows and not [s for s in shows if ".check" in s]
    assert "shows/demo/demo.csv" in shows
