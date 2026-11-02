from pathlib import Path

import pytest

import show_orchestrator.madscenes as madscenes
from madmapper_scenes import read_scenes
from show_orchestrator.madfile import (
    T_BOOL, T_BYTEARRAY, T_INT, T_LIST, T_MAP, T_STRING, MadProject, get,
    mad_file_in, output_path_for, save_project, to_plain,
)
from show_orchestrator.madscenes import Scene, ScaffoldError, fnv1a64, scaffold_scenes


def string(text):
    return (T_STRING, text, 0)


def surface(surface_id, name, surface_type="quad", visual_id=-1):
    return (T_MAP, [
        ("type", string(surface_type)),
        ("surfaceId", (T_INT, surface_id, 0)),
        ("name", string(name)),
        ("visualId", (T_INT, visual_id, 0)),
        ("visible", (T_BOOL, True, 0)),
        ("positions", (T_BYTEARRAY, f"corners of {name}".encode(), 0)),
    ], 0)


def visual(visual_id, url, media_type="Image"):
    return (T_MAP, [
        ("id", (T_INT, visual_id, 0)),
        ("url", string(url)),
        ("type", string(media_type)),
        ("mmtype", string(media_type + "s")),
        ("gridPos", (T_INT, 0, 0)),
        ("name", string(url.rsplit("/", 1)[-1])),
    ], 0)


# A "Calib" group holding two quads, plus a loose "Template" quad.
SURFACES = [
    surface(1, "Template"),
    surface(2, "Calib", "group", visual_id=7),
    surface(3, "Left", visual_id=7),
    surface(4, "Right", visual_id=7),
]
RELATIONS = [-1, -1, 1, 1]


def surface_lists():
    return [
        ("surfaces", (T_LIST, list(SURFACES), 0)),
        ("surfacesRelations", (T_LIST, [(T_INT, p, 0) for p in RELATIONS], 0)),
    ]


def mm5_project(slots=4, visuals=()):
    return MadProject(12, [
        *surface_lists(),
        ("visuals", (T_MAP, [("visuals", (T_LIST, list(visuals), 0))], 0)),
        ("cueBanksV4", (T_MAP, [
            ("activeCueBank", string("Show")),
            ("cueBanks", (T_LIST, [(T_MAP, [
                ("bank_name", string("Show")),
                ("cues", (T_LIST, [(T_MAP, [], 0)] * slots, 0)),
            ], 0)], 0)),
        ], 0)),
    ])


def mm6_project(slots=4):
    conductor = (T_MAP, [
        ("scene", (T_BOOL, False, 0)),
        ("timeline", (T_MAP, [("name", string("conductor")), ("uid", (T_INT, 7, 0))], 0)),
        ("uid", (T_INT, 7, 0)),
    ], 0)
    return MadProject(19, [
        *surface_lists(),
        ("visuals", (T_MAP, [("visuals", (T_LIST, [], 0))], 0)),
        ("timelineBanks", (T_MAP, [
            ("activeTimelineBank", string("Show")),
            ("timelineBanks", (T_LIST, [(T_MAP, [
                ("attributeRegistryMap", (T_MAP, [], 0)),
                ("bank_name", string("Show")),
                ("conductor", conductor),
                ("timelines", (T_LIST, [(T_MAP, [], 0)] * slots, 0)),
            ], 0)], 0)),
        ], 0)),
    ])


@pytest.fixture
def media(tmp_path, monkeypatch):
    monkeypatch.setattr(madscenes, "probe_media", lambda path: (12.5, True))
    for name in ("a.png", "b.mp4", "c.jpg"):
        (tmp_path / name).write_bytes(b"media")
    return tmp_path


def surfaces(mad):
    return [to_plain(s) for s in get(mad.root, "surfaces")[1]]


def new_surfaces(mad):
    return [
        (s["name"], s["type"], s["surfaceId"], s["visualId"], s["visible"])
        for s in surfaces(mad)[len(SURFACES):]
    ]


def bank(mad):
    if get(mad.root, "timelineBanks"):
        return to_plain(get(mad.root, "timelineBanks"))["timelineBanks"][0]
    return to_plain(get(mad.root, "cueBanksV4"))["cueBanks"][0]


def entries(cue):
    return [(e["url"], e["value"]) for e in cue["entries"]]


def tracks(slot, show):
    registry = show["attributeRegistryMap"]
    return [(registry[str(t["id"])]["u"], t["it"][0]["v"]) for t in slot["timeline"]["tracks"]]


def test_fnv1a64_matches_madmapper_ids():
    # Taken from a real MadMapper 6 project's attribute registry.
    assert fnv1a64("/media/20/Offset") == 10008360761323838965


def test_each_scene_gets_a_hidden_group_holding_a_copy_of_the_template_quad(media):
    mad = mm5_project()
    scaffold_scenes(mad, [Scene("Intro", media / "a.png"), Scene("Storm", media / "b.mp4")], "Template")

    assert new_surfaces(mad) == [
        ("Intro", "group", 5, 1, False),
        ("Intro - Template", "quad", 6, 1, True),
        ("Storm", "group", 7, 2, False),
        ("Storm - Template", "quad", 8, 2, True),
    ]
    assert to_plain(get(mad.root, "surfacesRelations")) == RELATIONS + [-1, 4, -1, 6]
    assert surfaces(mad)[5]["positions"] == b"corners of Template"


def test_a_template_group_has_all_its_surfaces_copied(media):
    mad = mm6_project()
    report = scaffold_scenes(mad, [Scene("Intro", media / "a.png")], "Calib")

    assert new_surfaces(mad) == [
        ("Intro", "group", 5, 1, False),
        ("Intro - Left", "quad", 6, 1, True),
        ("Intro - Right", "quad", 7, 1, True),
    ]
    assert to_plain(get(mad.root, "surfacesRelations")) == RELATIONS + [-1, 4, 4]
    assert [s["positions"] for s in surfaces(mad)[5:]] == [b"corners of Left", b"corners of Right"]
    assert report.scenes == [("Intro", 5, [6, 7], 1)]


def test_a_scene_without_media_shows_nothing(media):
    mad = mm6_project()
    scaffold_scenes(mad, [Scene("Tap", None)], "Calib")
    assert [s[3] for s in new_surfaces(mad)] == [-1, -1, -1]
    assert to_plain(get(mad.root, "visuals"))["visuals"] == []


def test_media_entries_are_added_with_type_and_length(media):
    mad = mm5_project()
    scaffold_scenes(mad, [Scene("Intro", media / "a.png"), Scene("Storm", media / "b.mp4")], "Template")

    visuals = to_plain(get(mad.root, "visuals"))["visuals"]
    assert [(v["id"], v["type"], v["mmtype"], v["name"]) for v in visuals] == [
        (1, "Image", "Images", "a.png"),
        (2, "Movie", "Movies", "b.mp4"),
    ]
    assert visuals[1]["url"] == (media / "b.mp4").resolve().as_posix()
    assert visuals[1]["attributes"]["loop_end"] == 12.5
    assert visuals[1]["attributes"]["HAS_AUDIO_TRACK"] is True


def test_media_already_in_the_project_is_reused(media):
    url = (media / "a.png").resolve().as_posix()
    mad = mm5_project(visuals=[visual(9, url)])
    report = scaffold_scenes(mad, [Scene("Intro", media / "a.png")], "Template")

    assert report.reused_media == [media / "a.png"]
    assert len(to_plain(get(mad.root, "visuals"))["visuals"]) == 1
    assert surfaces(mad)[len(SURFACES)]["visualId"] == 9


def test_new_media_ids_skip_madmapper_internal_ids(media):
    mad = mm5_project(visuals=[visual(4, "C:/x.png"), visual(1075380227, "", "TimelineMediaTrack")])
    scaffold_scenes(mad, [Scene("Intro", media / "c.jpg")], "Template")
    assert surfaces(mad)[len(SURFACES)]["visualId"] == 5


def test_mm5_cue_per_scene_shows_only_its_group_and_restarts_its_video(media):
    mad = mm5_project()
    scaffold_scenes(mad, [Scene("Intro", media / "a.png"), Scene("Storm", media / "b.mp4")], "Template")

    cues = bank(mad)["cues"]
    assert [c.get("name") for c in cues] == ["Intro", "Storm", None, None]
    intro, storm = cues[0], cues[1]
    assert intro["is_scene_cue"] is False
    assert [(e["url"], e["value"], e["display_path"], e["ownerUrl"]) for e in intro["entries"]] == [
        ("/surfaces/5/visible", True, "/Surfaces/Intro/Visible", "/surfaces/5"),
        ("/surfaces/7/visible", False, "/Surfaces/Storm/Visible", "/surfaces/7"),
    ]
    assert entries(storm) == [
        ("/surfaces/5/visible", False),
        ("/surfaces/7/visible", True),
        ("/medias/2/play_mode", "Forward"),
        ("/medias/2/position_sec", 0.0),
    ]
    position = storm["entries"][3]
    assert position["display_path"] == "/Medias/b.mp4/Position (Seconds)"
    assert position["transition_mode"] == 0  # jumps to the start instead of fading
    assert storm["entries"][2]["transition_mode"] == 1


def test_restarting_videos_can_be_turned_off(media):
    mad = mm5_project()
    scaffold_scenes(mad, [Scene("Storm", media / "b.mp4")], "Template", restart_videos=False)
    assert entries(bank(mad)["cues"][0]) == [("/surfaces/5/visible", True)]


def test_mm5_fade_and_blackout(media):
    mad = mm5_project()
    report = scaffold_scenes(mad, [Scene("Intro", media / "a.png")], "Calib", fade=1.0, blackout="Blackout")

    cues = bank(mad)["cues"]
    assert [c.get("name") for c in cues] == ["Intro", "Blackout", None, None]
    assert cues[0]["transition_settings"]["duration"] == 1.0
    assert cues[1]["transition_settings"]["duration"] == 1.0
    # Every top-level surface goes dark: the loose template, Calib, and the scene.
    assert entries(cues[1]) == [
        ("/surfaces/1/visible", False),
        ("/surfaces/2/visible", False),
        ("/surfaces/5/visible", False),
    ]
    assert report.blackout == "Blackout"


def test_mm5_maps_keep_madmapper_5_key_order(media):
    mad = mm5_project()
    scaffold_scenes(mad, [Scene("Intro", media / "a.png")], "Template")
    cue = get(get(get(mad.root, "cueBanksV4")[1], "cueBanks")[1][0][1], "cues")[1][0]
    keys = [key for key, _ in cue[1]]
    assert keys == sorted(keys, reverse=True)


def test_mm6_timeline_per_scene_with_registered_tracks(media):
    mad = mm6_project()
    scaffold_scenes(mad, [Scene("Intro", media / "a.png"), Scene("Storm", media / "b.mp4")], "Template")

    show = bank(mad)
    slots = show["timelines"]
    assert [s.get("timeline", {}).get("name") for s in slots] == ["Intro", "Storm", None, None]
    intro, storm = slots[0], slots[1]
    assert intro["scene"] is False
    assert intro["uid"] == intro["timeline"]["uid"] == 8  # after the conductor's 7
    assert storm["uid"] == 9
    assert intro["timeline"]["tracks"] == [
        {"id": fnv1a64("/surfaces/5/visible"), "it": [{"tp": "vkf", "v": True}]},
        {"id": fnv1a64("/surfaces/7/visible"), "it": [{"tp": "vkf", "v": False}]},
    ]
    assert tracks(storm, show) == [
        ("/surfaces/5/visible", False),
        ("/surfaces/7/visible", True),
        ("/media/2/play_mode", "Forward"),
        ("/media/2/position_sec", 0.0),
    ]
    assert storm["timeline"]["tracks"][3]["tr"] == {"md": 0}  # jump, don't fade
    assert "tr" not in storm["timeline"]["tracks"][2]
    assert show["attributeRegistryMap"][str(fnv1a64("/media/2/position_sec"))] == {
        "dp": "/Media/b.mp4/Position (Seconds)", "ou": "/media/2", "u": "/media/2/position_sec",
    }
    assert len(show["attributeRegistryMap"]) == 4


def test_mm6_fade_and_blackout(media):
    mad = mm6_project()
    scaffold_scenes(mad, [Scene("Tap", None)], "Calib", fade=1.5, blackout="Blackout")

    show = bank(mad)
    tap, blackout = show["timelines"][0], show["timelines"][1]
    assert tap["timeline"]["transition_settings"] == {"d": 1.5, "tr": {"tp": 1}}
    assert blackout["timeline"]["name"] == "Blackout"
    assert tracks(blackout, show) == [
        ("/surfaces/1/visible", False),
        ("/surfaces/2/visible", False),
        ("/surfaces/5/visible", False),
    ]


def test_mm6_trigger_copies_settings_of_an_existing_timeline(media):
    mad = mm6_project()
    timelines = get(get(get(mad.root, "timelineBanks")[1], "timelineBanks")[1][0][1], "timelines")[1]
    timelines[3] = (T_MAP, [
        ("scene", (T_BOOL, False, 0)),
        ("timeline", (T_MAP, [
            ("name", string("Existing")),
            ("transition_settings", (T_MAP, [("d", (T_INT, 3, 0))], 0)),
            ("uid", (T_INT, 2, 0)),
        ], 0)),
        ("uid", (T_INT, 2, 0)),
    ], 0)
    scaffold_scenes(mad, [Scene("Intro", media / "a.png")], "Template")
    assert bank(mad)["timelines"][0]["timeline"]["transition_settings"] == {"d": 3}


def test_unknown_template_is_an_error(media):
    with pytest.raises(ScaffoldError, match="no surface named 'Missing'"):
        scaffold_scenes(mm5_project(), [Scene("Intro", media / "a.png")], "Missing")


@pytest.mark.parametrize("scenes, blackout, message", [
    ([("Intro", "a.png"), ("Intro", "c.jpg")], None, "used twice"),
    ([("Intro", "a.png")], "Intro", "used twice"),
    ([("Calib", "a.png")], None, "already has a surface"),
    ([("Intro", "missing.png")], None, "not found"),
    ([("Intro", "notes.txt")], None, "not a video or image"),
])
def test_bad_scene_lists_are_rejected_and_leave_the_project_unchanged(media, scenes, blackout, message):
    (media / "notes.txt").write_text("x")
    mad = mm5_project()
    before = mad.to_bytes()
    with pytest.raises(ScaffoldError, match=message):
        scaffold_scenes(mad, [Scene(name, media / file) for name, file in scenes], "Template",
                        blackout=blackout)
    assert mad.to_bytes() == before


def test_too_few_free_slots_is_an_error(media):
    mad = mm5_project(slots=2)
    before = mad.to_bytes()
    with pytest.raises(ScaffoldError, match="2 free slots for 3 cues"):
        scaffold_scenes(mad, [Scene("Intro", media / "a.png"), Scene("Storm", media / "b.mp4")],
                        "Template", blackout="Blackout")
    assert mad.to_bytes() == before


def test_scaffolded_project_saves_and_reloads(media):
    mad = mm6_project()
    scaffold_scenes(mad, [Scene("Intro", media / "b.mp4")], "Calib", fade=1.0, blackout="Blackout")
    assert MadProject.from_bytes(mad.to_bytes()).root == mad.root


def test_madproject_folder_is_copied_with_its_mad_renamed(media, tmp_path):
    source = tmp_path / "Show.madproject"
    (source / "Media").mkdir(parents=True)
    (source / "Media" / "shader.fs").write_text("void main() {}")
    (source / "Backup").mkdir()
    (source / "Backup" / "Show.backup.mad").write_bytes(b"old")
    mad = mm6_project()
    mad.save(source / "Show.mad")

    loaded = MadProject.load(mad_file_in(source))
    output = output_path_for(source, "_scenes")
    saved = save_project(loaded, source, output)

    assert output == tmp_path / "Show_scenes.madproject"
    assert saved == output / "Show_scenes.mad"
    assert sorted(p.name for p in output.iterdir()) == ["Media", "Show_scenes.mad"]
    assert (output / "Media" / "shader.fs").read_text() == "void main() {}"
    assert to_plain(get(MadProject.load(saved).root, "projectPath")) == output.resolve().as_posix()
    assert (source / "Show.mad").exists()


def test_scene_csv_paths_are_relative_to_the_csv_and_media_is_optional(tmp_path):
    csv_file = tmp_path / "scenes.csv"
    csv_file.write_text(
        "scene,media\nIntro,videos/intro.mp4\n,\nTap,\nOutro,C:/abs/outro.png\n", encoding="utf-8"
    )
    scenes = read_scenes(csv_file)
    assert [(s.name, s.media) for s in scenes] == [
        ("Intro", tmp_path / "videos" / "intro.mp4"),
        ("Tap", None),
        ("Outro", Path("C:/abs/outro.png")),
    ]
