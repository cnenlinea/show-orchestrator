import struct
import zlib
from copy import deepcopy

import pytest

from show_orchestrator.madfile import (
    T_BOOL, T_DOUBLE, T_FLOAT, T_IMAGE, T_INT, T_INVALID, T_LIST, T_MAP,
    T_STRING, T_STRINGLIST, T_USER, MadFileError, MadProject, get, to_plain,
)
from show_orchestrator.madmapper import (
    DEFAULT_TRIGGER_SETTINGS, Cue, CueMatchError, list_cues, note_name,
    patch_cue_triggers,
)
from show_orchestrator.models import Effect


def string(text):
    return (T_STRING, text, 0)


def cue(name):
    return (T_MAP, [("name", string(name)), ("comment", string(""))], 0)


def bank(name, *cue_names):
    cues = [cue(n) if n else (T_MAP, [], 0) for n in cue_names]
    return (T_MAP, [("bank_name", string(name)), ("cues", (T_LIST, cues, 0))], 0)


def project(banks, mappings=()):
    return MadProject(12, [
        ("cueBanksV4", (T_MAP, [("cueBanks", (T_LIST, list(banks), 0))], 0)),
        ("mappings", (T_LIST, list(mappings), 0)),
    ])


def trigger(target, source_url, enabled=True):
    settings = to_settings(source_url)
    settings[1][[k for k, _ in settings[1]].index("enabled")] = ("enabled", (T_BOOL, enabled, 0))
    return [string(target), settings]


def to_settings(source_url):
    settings = deepcopy(DEFAULT_TRIGGER_SETTINGS)
    settings[1][-1] = ("channelInfos", (T_MAP, [("url", string(source_url))], 0))
    return settings


def triggers(mad: MadProject) -> list[tuple[str, str]]:
    entries = get(mad.root, "mappings")[1]
    return [
        (entries[i][1], to_plain(get(entries[i + 1][1], "channelInfos"))["url"])
        for i in range(0, len(entries), 2)
    ]


def png():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\x00")) + chunk(b"IEND", b""))


def test_note_names_follow_madmapper():
    assert note_name(0) == "C-2"
    assert note_name(1) == "C#-2"
    assert note_name(39) == "D#1"
    assert note_name(60) == "C3"
    assert note_name(127) == "G8"
    with pytest.raises(ValueError):
        note_name(128)


def test_project_survives_save_and_load_unchanged():
    mad = MadProject(12, [
        ("flag", (T_BOOL, True, 0)),
        ("count", (T_INT, -3, 0)),
        ("level", (T_DOUBLE, 0.25, 0)),
        ("speed", (T_FLOAT, 1.5, 0)),
        ("missing", (T_INVALID, None, 1)),
        ("nullString", (T_STRING, None, 1)),
        ("files", (T_STRINGLIST, ["a.mp4", "ñ.png"], 0)),
        ("color", (T_USER, (b"MadColor\x00", b"\x00\x00\x00\x06\x00r\x00g\x00b" + b"\x00" * 32), 0)),
        ("thumbnail", (T_IMAGE, png(), 0)),
        ("nested", (T_LIST, [(T_MAP, [("name", string("Muñecas"))], 0)], 0)),
    ])
    data = mad.to_bytes()
    reloaded = MadProject.from_bytes(data)
    assert reloaded.root == mad.root
    assert reloaded.to_bytes() == data


def test_rejects_files_that_are_not_madmapper_projects():
    with pytest.raises(MadFileError):
        MadProject.from_bytes(b"\x00\x00\x00\x01" + b"\x00" * 8)


def test_lists_named_cues_in_every_bank():
    mad = project([bank("A", "Intro", None, "Outro"), bank("B", "Extra")])
    assert list_cues(mad) == [Cue("A", "Intro"), Cue("A", "Outro"), Cue("B", "Extra")]


def test_matching_cue_gets_effect_note_and_channel():
    mad = project([bank("Show", "Blackout")])
    report = patch_cue_triggers(mad, [Effect(id="Blackout", name="Blackout", note=60, channel=1)])

    assert triggers(mad) == [("/cues/Show/cues/by_name/Blackout", "/midiin/all/2/C3")]
    assert report.patched == [("Blackout", Cue("Show", "Blackout"), 60, 1)]
    channel_infos = to_plain(get(get(mad.root, "mappings")[1][1][1], "channelInfos"))
    assert channel_infos == {
        "url": "/midiin/all/2/C3",
        "path": "Midi 2/C3",
        "name": "2/C3",
        "device": {"url": "/midiin/all", "path": "Midi Input", "name": "all", "ct": 1},
    }


def test_names_match_ignoring_case_accents_and_spacing():
    mad = project([bank("Show", "Munecas  bg")])
    report = patch_cue_triggers(mad, [Effect(id="Muñecas BG", name="Muñecas BG", note=13, channel=0)])
    assert [cue.name for _, cue, _, _ in report.patched] == ["Munecas  bg"]


def test_effect_id_is_used_when_its_name_matches_no_cue():
    mad = project([bank("Show", "fade_in")])
    report = patch_cue_triggers(mad, [Effect(id="fade_in", name="Fade In (All Lights On)", note=2, channel=0)])
    assert len(report.patched) == 1


def test_existing_note_trigger_is_replaced_and_everything_else_kept():
    mad = project([bank("Show", "Blackout", "Lava")], [
        *trigger("/cues/Show/cues/by_name/Blackout", "/midiin/all/1/C#-2"),
        *trigger("/cues/Show/cues/by_name/Blackout", "/midiin/all/1/cc20"),
        *trigger("/cues/Show/cues/by_name/Lava", "/midiin/all/1/B-2"),
        *trigger("/surfaces/Lava/opacity", "/midiin/all/1/cc48"),
    ])
    report = patch_cue_triggers(mad, [Effect(id="Blackout", name="Blackout", note=5, channel=0)])

    assert triggers(mad) == [
        ("/cues/Show/cues/by_name/Blackout", "/midiin/all/1/cc20"),
        ("/cues/Show/cues/by_name/Lava", "/midiin/all/1/B-2"),
        ("/surfaces/Lava/opacity", "/midiin/all/1/cc48"),
        ("/cues/Show/cues/by_name/Blackout", "/midiin/all/1/F-2"),
    ]
    assert report.unmatched_cues == [Cue("Show", "Lava")]


def test_new_trigger_copies_settings_of_an_existing_cue_trigger_and_is_enabled():
    mad = project([bank("Show", "Blackout", "Lava")], [
        *trigger("/cues/Show/cues/by_name/Lava", "/midiin/all/1/B-2", enabled=False),
    ])
    template = get(mad.root, "mappings")[1][1]
    sensitivity = [k for k, _ in template[1]].index("sensitivity")
    template[1][sensitivity] = ("sensitivity", (T_DOUBLE, 0.5, 0))

    patch_cue_triggers(mad, [Effect(id="Blackout", name="Blackout", note=1, channel=0)])

    added = get(mad.root, "mappings")[1][3]
    assert to_plain(get(added[1], "sensitivity")) == 0.5
    assert to_plain(get(added[1], "enabled")) is True


def test_unmatched_effects_and_cues_are_reported():
    mad = project([bank("Show", "Blackout", "Lava")])
    report = patch_cue_triggers(mad, [
        Effect(id="Blackout", name="Blackout", note=1, channel=0),
        Effect(id="Full Strobe", name="Full Strobe", note=2, channel=0),
    ])
    assert [effect.id for effect in report.unmatched_effects] == ["Full Strobe"]
    assert report.unmatched_cues == [Cue("Show", "Lava")]


def test_cue_name_in_several_banks_gets_a_trigger_in_each():
    mad = project([bank("A", "Blackout"), bank("B", "Blackout")])
    patch_cue_triggers(mad, [Effect(id="Blackout", name="Blackout", note=1, channel=0)])
    assert [target for target, _ in triggers(mad)] == [
        "/cues/A/cues/by_name/Blackout",
        "/cues/B/cues/by_name/Blackout",
    ]


def test_two_effects_matching_one_cue_is_an_error():
    mad = project([bank("Show", "Blackout")])
    with pytest.raises(CueMatchError):
        patch_cue_triggers(mad, [
            Effect(id="Blackout", name="Blackout", note=1, channel=0),
            Effect(id="blackout", name="BLACKOUT", note=2, channel=0),
        ])


def test_patched_project_saves_and_reloads(tmp_path):
    mad = project([bank("Show", "Blackout")])
    patch_cue_triggers(mad, [Effect(id="Blackout", name="Blackout", note=1, channel=0)])
    path = tmp_path / "show.mad"
    mad.save(path)
    assert triggers(MadProject.load(path)) == [("/cues/Show/cues/by_name/Blackout", "/midiin/all/1/C#-2")]
