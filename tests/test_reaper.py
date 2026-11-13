import pytest

from show_orchestrator.backends.reaper import ReaperBackend
from show_orchestrator.models import AudioTrack, Effect, EffectType, ExtraAudioTrack, Show


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


def test_extra_tracks_with_the_same_name_share_a_reaper_track(tmp_path):
    for name in ("a.mid", "b.mid", "click.mid"):
        (tmp_path / name).write_bytes(b"")

    def song(name, extras):
        return AudioTrack(
            name=name, duration="0:10", events={EffectType.LIGHTS: []},
            extra_tracks=[
                ExtraAudioTrack(name=extra, timestamp="0:00", duration="0:10",
                                file_path=str(tmp_path / file))
                for extra, file in extras
            ],
        )

    show = Show(
        audio_tracks=[
            song("One", [("Luces", "a.mid")]),
            song("Two", [("Luces", "b.mid"), ("Click", "click.mid")]),
        ],
        effects={EffectType.LIGHTS: []},
    )
    output_dir = tmp_path / "build"
    output_dir.mkdir()
    backend = ReaperBackend()
    backend.create_project(show, {}, output_dir)
    rpp_path = output_dir / "test.rpp"
    backend.save_project(rpp_path)

    content = rpp_path.read_text()
    assert content.count('NAME "Luces"') == 1
    assert content.count('NAME "Click"') == 1
    luces = content.split('NAME "Luces"')[1].split("<TRACK")[0]
    assert 'FILE "a.mid"' in luces and 'FILE "b.mid"' in luces
    assert "POSITION 10.5\n" in luces  # the second song, after the default 0.5 s gap


def test_missing_media_raises(tmp_path):
    show = make_show(file_path=str(tmp_path / "missing.mp3"))
    backend = ReaperBackend()
    with pytest.raises(FileNotFoundError, match="missing.mp3"):
        backend.create_project(show, {}, tmp_path)


def two_song_show(tmp_path):
    for name in ("one.mp3", "two.mp3", "lights.mid"):
        (tmp_path / name).write_bytes(b"")
    return Show(
        audio_tracks=[
            AudioTrack(name=name, duration="0:10", file_path=str(tmp_path / file),
                       events={EffectType.LIGHTS: []},
                       extra_tracks=[ExtraAudioTrack(name="Luces", timestamp="0:01", duration="0:05",
                                                     file_path=str(tmp_path / "lights.mid"))])
            for name, file in (("One", "one.mp3"), ("Two", "two.mp3"))
        ],
        effects={EffectType.LIGHTS: []},
    )


def song_positions(rpp: str) -> tuple[list[str], list[str]]:
    lines = [line.strip() for line in rpp.splitlines()]
    markers = [line.split()[2] for line in lines if line.startswith("MARKER")]
    positions = [line.split()[1] for line in lines if line.startswith("POSITION")]
    return markers, positions


@pytest.mark.parametrize("gap, second_song", [(None, "10.5"), (2, "12"), (0, "10")])
def test_songs_are_separated_by_the_gap(tmp_path, gap, second_song):
    output_dir = tmp_path / "build"
    output_dir.mkdir()
    midi_files = {
        name: {EffectType.LIGHTS: {"file_path": output_dir / f"{name}_lights.mid", "duration": 3.0}}
        for name in ("One", "Two")
    }
    backend = ReaperBackend()
    kwargs = {} if gap is None else {"song_gap": gap}
    backend.create_project(two_song_show(tmp_path), midi_files, output_dir, **kwargs)
    backend.save_project(output_dir / "show.rpp")

    markers, positions = song_positions((output_dir / "show.rpp").read_text())
    assert [float(m) for m in markers] == [0, float(second_song)]
    # audio, then the song's extra clip at 0:01 and its cues: all move with the song
    second = float(second_song)
    assert sorted(float(p) for p in positions) == sorted([0, second, 1, second + 1, 0, second])


def test_a_negative_gap_is_refused(tmp_path):
    with pytest.raises(ValueError, match="negative"):
        ReaperBackend().create_project(two_song_show(tmp_path), {}, tmp_path, song_gap=-1)


# reaper: split_midi: moving a MIDI track of an extra track's file onto a REAPER track of its own

def write_lights_file(path, lyrics=True):
    """A file like the musical's: a tempo track, lights on channel 2, ReWorship lyric cues on channel 10."""
    import mido

    midi = mido.MidiFile(type=1, ticks_per_beat=480)
    midi.tracks.append(mido.MidiTrack([
        mido.MetaMessage("track_name", name="QLC+"),
        mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(100), time=0),
    ]))
    midi.tracks.append(mido.MidiTrack([
        mido.MetaMessage("track_name", name="Lights"),
        mido.Message("note_on", channel=1, note=0, velocity=100, time=960),
        mido.Message("note_off", channel=1, note=0, time=120),
    ]))
    if lyrics:
        midi.tracks.append(mido.MidiTrack([
            mido.MetaMessage("track_name", name="Letras ReWorship"),
            mido.Message("program_change", channel=9, program=4, time=0),
            mido.Message("note_on", channel=9, note=0, velocity=100, time=480),
            mido.Message("note_off", channel=9, note=0, time=120),
            mido.Message("note_on", channel=9, note=1, velocity=100, time=1800),
            mido.Message("note_off", channel=9, note=1, time=120),
        ]))
    midi.save(path)


def timed_notes(path):
    """(seconds, channel, note) of every note on in a .mid, and the names of its tracks."""
    import mido

    midi = mido.MidiFile(path)
    seconds, notes = 0.0, []
    for message in midi:
        seconds += message.time
        if message.type == "note_on":
            notes.append((round(seconds, 6), message.channel, message.note))
    names = [next((m.name for m in t if m.type == "track_name"), "") for t in midi.tracks]
    return notes, names


def test_split_midi_moves_a_named_track_to_a_file_of_its_own(tmp_path):
    from show_orchestrator.backends.reaper import split_midi_file

    source = tmp_path / "song.mid"
    write_lights_file(source)
    pieces = split_midi_file(source, {"Letras ReWorship": "Letras"})
    assert [target for target, _ in pieces] == ["Letras", None]
    for target, midi in pieces:
        midi.save(tmp_path / f"{target}.mid")

    original, _ = timed_notes(source)
    lyrics, lyric_tracks = timed_notes(tmp_path / "Letras.mid")
    rest, rest_tracks = timed_notes(tmp_path / "None.mid")
    assert lyrics == [n for n in original if n[1] == 9] and len(lyrics) == 2
    assert rest == [n for n in original if n[1] != 9] and len(rest) == 1
    assert lyrics[0][0] == pytest.approx(0.6)  # 480 ticks, one beat at 100 BPM (0.5 s at 120): the tempo map came along
    assert lyric_tracks == ["", "Letras ReWorship"] and rest_tracks == ["QLC+", "Lights"]
    import mido
    assert any(m.type == "program_change" for m in mido.MidiFile(tmp_path / "Letras.mid"))


def test_split_midi_leaves_files_it_names_nothing_in(tmp_path):
    from show_orchestrator.backends.reaper import split_midi_file

    source = tmp_path / "song.mid"
    write_lights_file(source, lyrics=False)
    assert split_midi_file(source, {"Letras ReWorship": "Letras"}) is None
    assert split_midi_file(source, None) is None
    (tmp_path / "song.mp3").write_bytes(b"audio")
    assert split_midi_file(tmp_path / "song.mp3", {"Letras ReWorship": "Letras"}) is None


def test_split_midi_puts_lyrics_on_their_own_routable_track(tmp_path):
    from show_orchestrator.backends.reaper import project_track_names, read_midi_routing
    from show_orchestrator.models import ReaperSettings

    write_lights_file(tmp_path / "one.mid")
    write_lights_file(tmp_path / "two.mid", lyrics=False)

    def song(name, file):
        return AudioTrack(
            name=name, duration="0:10", events={EffectType.LIGHTS: []},
            extra_tracks=[ExtraAudioTrack(name="Luces", timestamp="0:00", duration="0:10",
                                          file_path=str(tmp_path / file))],
        )

    show = Show(
        audio_tracks=[song("One", "one.mid"), song("Two", "two.mid")],
        effects={EffectType.LIGHTS: []},
        reaper=ReaperSettings(split_midi={"Letras ReWorship": "Letras"}),
    )
    assert project_track_names(show) == ["lights", "Luces", "Letras"]
    output_dir = tmp_path / "build"
    output_dir.mkdir()
    backend = ReaperBackend()
    backend.create_project(show, {}, output_dir, routing={"Luces": 64, "Letras": 65})
    backend.save_project(output_dir / "show.rpp")

    content = (output_dir / "show.rpp").read_text()
    assert read_midi_routing(content) == [("lights", -1), ("Luces", 64), ("Letras", 65)]
    luces = content.split('NAME "Luces"')[1].split("<TRACK")[0]
    letras = content.split('NAME "Letras"')[1]
    assert 'FILE "one_Luces.mid"' in luces and 'FILE "two.mid"' in luces
    assert 'FILE "one_Letras.mid"' in letras and "two" not in letras
    assert sorted(p.name for p in output_dir.glob("*.mid")) == ["one_Letras.mid", "one_Luces.mid", "two.mid"]


def test_rebuilding_picks_up_media_that_changed_since(tmp_path):
    import os

    media_dir = tmp_path / "Pistas"
    media_dir.mkdir()
    song = media_dir / "song.mp3"
    song.write_bytes(b"first mix")
    output_dir = tmp_path / "build"
    output_dir.mkdir()

    def build():
        ReaperBackend().create_project(make_show(str(song)), {}, output_dir)
        return output_dir / "song.mp3"

    copy = build()
    assert copy.read_bytes() == b"first mix"

    song.write_bytes(b"second mix, longer")
    assert build().read_bytes() == b"second mix, longer"

    song.write_bytes(b"third  mix, longer")  # same size as the copy: the bytes still tell
    os.utime(song, (copy.stat().st_atime, copy.stat().st_mtime + 5))
    assert build().read_bytes() == b"third  mix, longer"

    # Unchanged media is not copied again (copying over the read-only copy would fail).
    before = copy.stat().st_mtime_ns
    copy.chmod(0o444)
    try:
        build()
    finally:
        copy.chmod(0o644)
    assert copy.stat().st_mtime_ns == before
