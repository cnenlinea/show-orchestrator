import math
import struct
import wave
import zlib
from copy import deepcopy

import pytest

from show_orchestrator.madfile import (
    T_BOOL, T_DOUBLE, T_FLOAT, T_IMAGE, T_INT, T_INVALID, T_LIST, T_MAP,
    T_STRING, T_STRINGLIST, T_USER, MadFileError, MadProject, get, to_plain,
)
from show_orchestrator.madmapper import (
    DEFAULT_TRIGGER_SETTINGS, Cue, CueMatchError, fades_to_audio, list_cues,
    note_name, patch_cue_triggers, set_trigger_fade, trigger_fade,
)
from show_orchestrator.models import AudioTrack, Effect, EffectType, Event, Show


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
        ("thumbnail", (T_IMAGE, b"\x00\x00\x00\x01" + png(), 0)),  # flag, then the PNG
        ("noThumbnail", (T_IMAGE, b"\x00\x00\x00\x00", 0)),
        ("curve", (T_USER, (b"QList<float>\x00", b"\x00\x00\x00\x01" + b"?\xf0" + b"\x00" * 6), 0)),
        ("nested", (T_LIST, [(T_MAP, [("name", string("Muñecas"))], 0)], 0)),
    ])
    data = mad.to_bytes()
    reloaded = MadProject.from_bytes(data)
    assert reloaded.root == mad.root
    assert reloaded.to_bytes() == data


def test_image_without_png_after_its_flag_is_an_error():
    mad = MadProject(12, [("thumbnail", (T_IMAGE, b"\x00\x00\x00\x01not a png", 0))])
    with pytest.raises(MadFileError):
        MadProject.from_bytes(mad.to_bytes())


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


def timeline_project(*names):
    slots = [
        (T_MAP, [("scene", (T_BOOL, False, 0)), ("timeline", (T_MAP, [("name", string(n))], 0))], 0)
        for n in names
    ] + [(T_MAP, [], 0)]
    return MadProject(19, [
        ("mappings", (T_LIST, [], 0)),
        ("timelineBanks", (T_MAP, [("timelineBanks", (T_LIST, [(T_MAP, [
            ("bank_name", string("Musical")),
            ("timelines", (T_LIST, slots, 0)),
        ], 0)], 0))], 0)),
    ])


def test_madmapper_6_timelines_are_listed_as_cues():
    mad = timeline_project("Intro", "Blackout")
    assert list_cues(mad) == [Cue("Musical", "Intro", timeline=True), Cue("Musical", "Blackout", timeline=True)]
    assert Cue("Musical", "Intro", timeline=True).url == "/timelines/Musical/by_name/Intro/play_from_beginning"


def test_madmapper_6_timeline_gets_a_trigger_in_madmapper_6_key_order():
    mad = timeline_project("Intro")
    report = patch_cue_triggers(mad, [Effect(id="Intro", name="Intro", note=60, channel=0)])

    assert triggers(mad) == [("/timelines/Musical/by_name/Intro/play_from_beginning", "/midiin/all/1/C3")]
    assert len(report.patched) == 1
    settings = get(mad.root, "mappings")[1][1]
    keys = [key for key, _ in settings[1]]
    assert keys == sorted(keys)
    infos = get(settings[1], "channelInfos")[1]
    assert [key for key, _ in infos] == ["device", "name", "path", "url"]
    curve = get(get(settings[1], "curveSettings")[1], "curve")[1]
    assert curve[0] == b"QList<float>\x00"


def test_madmapper_6_existing_timeline_trigger_is_replaced():
    mad = timeline_project("Intro")
    get(mad.root, "mappings")[1].extend(
        trigger("/timelines/Musical/by_name/Intro/play_from_beginning", "/midiin/all/1/C-2")
    )
    patch_cue_triggers(mad, [Effect(id="Intro", name="Intro", note=2, channel=0)])
    assert triggers(mad) == [("/timelines/Musical/by_name/Intro/play_from_beginning", "/midiin/all/1/D-2")]


# Current MadMapper 6 keeps every timeline in one shared list, conductor
# included; a bank's grid holds their uids (-1 empty).
def shared_timeline_project(*names, fade=1.0):
    def entry(uid, name):
        return (T_MAP, [
            ("scene", (T_BOOL, False, 0)),
            ("timeline", (T_MAP, [
                ("name", string(name)),
                ("transition_settings", (T_MAP, [
                    ("d", (T_DOUBLE, fade, 0)),
                    ("tr", (T_MAP, [("tp", (T_INT, 1, 0))], 0)),
                ], 0)),
                ("uid", (T_INT, uid, 0)),
            ], 0)),
            ("uid", (T_INT, uid, 0)),
        ], 0)
    timelines = [entry(25, "conductor")] + [entry(26 + i, n) for i, n in enumerate(names)]
    slots = [(T_INT, 26 + i, 0) for i in range(len(names))] + [(T_INT, -1, 0)] * 2
    return MadProject(25, [
        ("mappings", (T_LIST, [], 0)),
        ("timelineBanks", (T_MAP, [
            ("activeTimelineBank", string("Musical")),
            ("attributeRegistryMap", (T_MAP, [], 0)),
            ("timelineBanks", (T_LIST, [(T_MAP, [
                ("bank_name", string("Musical")),
                ("is_conductor", (T_BOOL, False, 0)),
                ("timelines", (T_LIST, slots, 0)),
            ], 0), (T_MAP, [
                ("bank_name", string("Musical_Conductor")),
                ("is_conductor", (T_BOOL, True, 0)),
                ("timelines", (T_LIST, [(T_INT, 25, 0)], 0)),
            ], 0)], 0)),
            ("timelines", (T_LIST, timelines, 0)),
        ], 0)),
    ])


def test_current_madmapper_6_timelines_are_listed_without_the_conductor():
    mad = shared_timeline_project("Intro", "Blackout")
    assert list_cues(mad) == [
        Cue("Musical", "Intro", timeline=True, shared=True),
        Cue("Musical", "Blackout", timeline=True, shared=True),
    ]
    assert Cue("Musical", "Intro", timeline=True, shared=True).url == \
        "/show/Musical/by_name/Intro/play_from_beginning"


def test_current_madmapper_6_trigger_targets_the_show_and_replaces_the_old_one():
    mad = shared_timeline_project("Intro", "Blackout")
    get(mad.root, "mappings")[1].extend(
        trigger("/show/Musical/by_name/Intro/play_from_beginning", "/midiin/all/1/C-2")
        + trigger("/show/Musical/by_name/Blackout/play_from_beginning", "/key/all/q")
    )
    patch_cue_triggers(mad, [Effect(id="Intro", name="Intro", note=8, channel=0)])
    assert triggers(mad) == [
        ("/show/Musical/by_name/Blackout/play_from_beginning", "/key/all/q"),
        ("/show/Musical/by_name/Intro/play_from_beginning", "/midiin/all/1/G#-2"),
    ]


@pytest.mark.parametrize("make, cue", [
    (lambda: shared_timeline_project("Intro"), Cue("Musical", "Intro", timeline=True, shared=True)),
    (lambda: timeline_project("Intro"), Cue("Musical", "Intro", timeline=True)),
    (lambda: project([bank("Show", "Intro")]), Cue("Show", "Intro")),
])
def test_trigger_fade_is_read_and_set_in_every_layout(make, cue):
    mad = make()
    set_trigger_fade(mad, cue, 2.5)
    assert trigger_fade(mad, cue) == 2.5
    assert trigger_fade(MadProject.from_bytes(mad.to_bytes()), cue) == 2.5


def test_setting_a_fade_changes_only_that_trigger():
    mad = shared_timeline_project("Intro", "Blackout")
    set_trigger_fade(mad, Cue("Musical", "Intro", timeline=True, shared=True), 5.6)
    assert trigger_fade(mad, Cue("Musical", "Blackout", timeline=True, shared=True)) == 1.0
    assert trigger_fade(mad, Cue("Musical", "Missing", timeline=True, shared=True)) is None


def wav(path, silence, tone, rate=44100):
    """A mono WAV: `silence` seconds of silence, then `tone` seconds of a loud 440 Hz tone."""
    samples = [0] * int(silence * rate) + [
        int(16000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(int(tone * rate))
    ]
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return str(path)


def test_fades_follow_the_silence_after_each_cue(tmp_path):
    def song(name, file, *cues):
        return AudioTrack(name=name, duration="0:10", file_path=file, events={
            EffectType.PROJECTION: [Event(timestamp=t, effect_id=e) for t, e in cues],
        })

    quiet_start = wav(tmp_path / "quiet.wav", silence=2.0, tone=3.0)
    loud_start = wav(tmp_path / "loud.wav", silence=0.0, tone=3.0)
    (tmp_path / "notes.txt").write_text("not audio")
    show = Show(
        audio_tracks=[
            song("Quiet", quiet_start, (0, "Quiet"), (0.5, "Late"), (4.5, "Blackout")),
            song("Loud", loud_start, (0, "Loud"), (2.5, "Blackout")),
            song("Silent", None, (0, "Silent")),
            song("Broken", str(tmp_path / "notes.txt"), (0, "Broken")),
        ],
        effects={EffectType.PROJECTION: []},
    )
    names = ["Quiet", "Late", "Loud", "Silent", "Broken", "Blackout"]
    mad = shared_timeline_project(*names)
    cues = {n: Cue("Musical", n, timeline=True, shared=True) for n in names}
    patched = [(n, cues[n], i, 0) for i, n in enumerate(names)]

    results = {r.effect_id: r for r in fades_to_audio(mad, show, patched)}

    assert results["Quiet"].fade == 2.0 and results["Quiet"].old_fade == 1.0
    assert results["Late"].fade == 1.5  # the silence left after a cue at 0:00.5
    assert trigger_fade(mad, cues["Quiet"]) == 2.0
    for name, reason in [("Loud", "already audible at 0:00.000"), ("Silent", "no audio file"),
                         ("Broken", "can't decode"), ("Blackout", "fires 2 times")]:
        assert results[name].fade is None
        assert reason in results[name].reason
        assert trigger_fade(mad, cues[name]) == 1.0
