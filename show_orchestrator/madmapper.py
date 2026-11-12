"""Set MadMapper cue MIDI triggers from a show's effects.

MadMapper keeps its MIDI controls in the project's `mappings` list, as
alternating (target URL, settings) entries. A cue trigger targets
`/cues/<bank>/cues/by_name/<cue>` and its settings name the MIDI source, e.g.
`/midiin/all/1/C#-2`: any MIDI input, channel 1 (MadMapper counts from 1, mido
from 0) and note 1 (MadMapper names notes from C-2 = 0).

MadMapper 6 replaced cues with timelines; a trigger there targets
`/timelines/<bank>/by_name/<timeline>/play_from_beginning`, which is how
MadMapper 6 converts a MadMapper 5 cue trigger, and in current MadMapper 6
`/show/<bank>/by_name/<timeline>/play_from_beginning` (see madbanks.py).
Below, "cue" means any of them.
"""
import copy
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from show_orchestrator.madfile import (
    T_BOOL, T_DOUBLE, T_INT, T_LIST, T_MAP, T_STRING, T_USER,
    MadProject, Variant, get, set_value, to_plain,
)
from show_orchestrator.madbanks import (
    MM5, MM6, MM6_SHARED, TRIGGER_URL_PREFIXES, TriggerBanks, trigger_url,
)
from show_orchestrator.audio import DEFAULT_SILENCE_DB, AudioError, silence_after
from show_orchestrator.models import Effect, Show

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
DEFAULT_DEVICE = "all"
MIDI_SOURCE = re.compile(r"^/midiin/(?P<device>.+)/(?P<channel>\d+)/(?P<control>[^/]+)$")
CC_CONTROL = re.compile(r"^cc\d+$")

# Settings of a cue trigger made in MadMapper 5's UI, in the key order it
# writes. Used when the project has no cue trigger to copy settings from.
_IDENTITY_CURVE_POINTS = b"\x00\x00\x00\x04" + b"\x00" * 16 + (b"?\xf0" + b"\x00" * 6) * 2
_IDENTITY_CURVE = (b"QVector<float>\x00", _IDENTITY_CURVE_POINTS)
DEFAULT_TRIGGER_SETTINGS: Variant = (T_MAP, [
    ("toggle", (T_BOOL, False, 0)),
    ("targetMin", (T_DOUBLE, 0.0, 0)),
    ("targetMax", (T_DOUBLE, 1.0, 0)),
    ("sourceMin", (T_DOUBLE, 0.0, 0)),
    ("sourceMax", (T_DOUBLE, 1.0, 0)),
    ("sensitivity", (T_DOUBLE, 1.0, 0)),
    ("filterSettings", (T_MAP, [
        ("filterType", (T_STRING, "Average", 0)),
        ("filterParam0", (T_DOUBLE, 0.2, 0)),
        ("active", (T_BOOL, False, 0)),
    ], 0)),
    ("feedback", (T_BOOL, True, 0)),
    ("enabled", (T_BOOL, True, 0)),
    ("curveSettings", (T_MAP, [
        ("mode", (T_STRING, "Catmull Rom", 0)),
        ("curve", (T_USER, _IDENTITY_CURVE, 0)),
        ("active", (T_BOOL, False, 0)),
    ], 0)),
    ("circular", (T_BOOL, False, 0)),
    ("channelInfos", (T_MAP, [], 0)),
], 0)


def _sorted_map(var: Variant) -> Variant:
    """`var` with every map's keys ascending, the order MadMapper 6 writes."""
    type_id, value, is_null = var
    if type_id != T_MAP:
        return var
    return (T_MAP, sorted(((k, _sorted_map(v)) for k, v in value), key=lambda p: p[0]), is_null)


def default_trigger_settings(mm6: bool) -> Variant:
    if not mm6:
        return DEFAULT_TRIGGER_SETTINGS
    settings = copy.deepcopy(DEFAULT_TRIGGER_SETTINGS)
    curve_settings = get(settings[1], "curveSettings")[1]
    set_value(curve_settings, "curve", (T_USER, (b"QList<float>\x00", _IDENTITY_CURVE_POINTS), 0))
    return _sorted_map(settings)


class CueMatchError(ValueError):
    pass


@dataclass(frozen=True)
class Cue:
    bank: str
    name: str
    timeline: bool = False  # a MadMapper 6 timeline rather than a cue
    shared: bool = False  # in current MadMapper 6's shared timeline list

    @property
    def layout(self) -> str:
        return MM6_SHARED if self.shared else MM6 if self.timeline else MM5

    @property
    def url(self) -> str:
        return trigger_url(self.layout, self.bank, self.name)


@dataclass
class PatchReport:
    # (effect id, cue, note, channel) for every trigger written
    patched: list[tuple[str, Cue, int, int]] = field(default_factory=list)
    unmatched_effects: list[Effect] = field(default_factory=list)
    unmatched_cues: list[Cue] = field(default_factory=list)


def note_name(note: int) -> str:
    """MadMapper's name for a MIDI note number: 0 -> C-2, 60 -> C3."""
    if not 0 <= note <= 127:
        raise ValueError(f"MIDI note out of range: {note}")
    return f"{NOTE_NAMES[note % 12]}{note // 12 - 2}"


def normalize_name(name: str) -> str:
    """Compare names ignoring case, accents and spacing: 'Muñecas  BG' ~ 'munecas bg'."""
    decomposed = unicodedata.normalize("NFKD", name)
    without_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(without_accents.casefold().split())


def list_cues(project: MadProject) -> list[Cue]:
    """Every named cue (or MadMapper 6 timeline), bank by bank, in grid order."""
    banks = TriggerBanks(project.root)
    cues = []
    for bank in banks.banks:
        bank_name = banks.bank_name(bank)
        for entry in banks.triggers(bank):
            name = banks.name(entry)
            if name:
                cues.append(Cue(bank_name, name, timeline=banks.is_mm6,
                                shared=banks.layout == MM6_SHARED))
    return cues


def trigger_entries(project: MadProject, cue: Cue) -> list[Variant]:
    """The cue or timeline entries `cue` names (one per slot it fills)."""
    banks = TriggerBanks(project.root)
    bank = banks.bank(cue.bank)
    if bank is None:
        return []
    return [entry for entry in banks.triggers(bank) if banks.name(entry) == cue.name]


def trigger_fade(project: MadProject, cue: Cue) -> float | None:
    """The transition (fade) time of `cue`, in seconds."""
    entries = trigger_entries(project, cue)
    return TriggerBanks(project.root).fade(entries[0]) if entries else None


def set_trigger_fade(project: MadProject, cue: Cue, seconds: float) -> None:
    """Set the transition (fade) time of `cue`, in seconds."""
    banks = TriggerBanks(project.root)
    for entry in trigger_entries(project, cue):
        banks.set_fade(entry, seconds)


def _midi_source(settings: Variant) -> re.Match | None:
    channel_infos = get(settings[1], "channelInfos")
    if channel_infos is None:
        return None
    return MIDI_SOURCE.match(to_plain(channel_infos).get("url") or "")


def _channel_infos(note: int, channel: int, device: str, mm6: bool = False) -> Variant:
    control = note_name(note)
    midi_channel = channel + 1
    infos = (T_MAP, [
        ("url", (T_STRING, f"/midiin/{device}/{midi_channel}/{control}", 0)),
        ("path", (T_STRING, f"Midi {midi_channel}/{control}", 0)),
        ("name", (T_STRING, f"{midi_channel}/{control}", 0)),
        ("device", (T_MAP, [
            ("url", (T_STRING, f"/midiin/{device}", 0)),
            ("path", (T_STRING, "Midi Input", 0)),
            ("name", (T_STRING, device, 0)),
            ("ct", (T_INT, 1, 0)),
        ], 0)),
    ], 0)
    return _sorted_map(infos) if mm6 else infos


def match_effects_to_cues(
    effects: Iterable[Effect], cues: list[Cue]
) -> tuple[dict[Cue, Effect], list[Effect]]:
    """Pair effects with the cues of the same name.

    An effect matches by its name, or by its id when the name matches no cue.
    Names in several banks match every one of them. Returns the matches and the
    effects that matched nothing; two effects claiming one cue is an error.
    """
    cues_by_name: dict[str, list[Cue]] = defaultdict(list)
    for cue in cues:
        cues_by_name[normalize_name(cue.name)].append(cue)

    matches: dict[Cue, Effect] = {}
    unmatched: list[Effect] = []
    for effect in effects:
        found = cues_by_name.get(normalize_name(effect.name)) or cues_by_name.get(
            normalize_name(effect.id)
        )
        if not found:
            unmatched.append(effect)
            continue
        for cue in found:
            other = matches.get(cue)
            if other is not None and other.id != effect.id:
                raise CueMatchError(
                    f"effects {other.id!r} and {effect.id!r} both match cue {cue.name!r}"
                )
            matches[cue] = effect
    return matches, unmatched


def patch_cue_triggers(
    project: MadProject,
    effects: Iterable[Effect],
    device: str = DEFAULT_DEVICE,
) -> PatchReport:
    """Give every cue named like an effect a MIDI trigger on that effect's note.

    Effects must already have a note and channel (see `assign_missing_notes`).
    A matched cue's existing note triggers are replaced; its CC triggers, cues
    that match no effect, and all other mappings are left as they are.
    """
    cues = list_cues(project)
    matches, unmatched_effects = match_effects_to_cues(effects, cues)
    report = PatchReport(unmatched_effects=unmatched_effects)
    matched_urls = {cue.url: cue for cue in matches}

    mappings = get(project.root, "mappings")
    if mappings is None:
        mappings = (T_LIST, [], 0)
        set_value(project.root, "mappings", mappings)
    entries = mappings[1]

    mm6 = TriggerBanks(project.root).is_mm6
    template = default_trigger_settings(mm6)
    kept: list[Variant] = []
    for i in range(0, len(entries), 2):
        target, settings = entries[i], entries[i + 1]
        source = _midi_source(settings)
        is_cue_trigger = source is not None and target[1].startswith(TRIGGER_URL_PREFIXES)
        if is_cue_trigger:
            template = settings
        is_note = source is not None and not CC_CONTROL.match(source["control"])
        if is_cue_trigger and is_note and target[1] in matched_urls:
            continue  # replaced below
        kept += [target, settings]

    for cue, effect in matches.items():
        settings = copy.deepcopy(template)
        set_value(settings[1], "channelInfos",
                  _channel_infos(effect.note, effect.channel, device, mm6))
        set_value(settings[1], "enabled", (T_BOOL, True, 0))
        kept += [(T_STRING, cue.url, 0), settings]
        report.patched.append((effect.id, cue, effect.note, effect.channel))
    entries[:] = kept

    report.unmatched_cues = [cue for cue in cues if cue not in matches]
    return report


MIN_SILENCE_FOR_FADE = 0.25  # seconds; less than this and the music is already playing


@dataclass
class FadeResult:
    effect_id: str
    cue: Cue
    old_fade: float | None
    fade: float | None  # the fade set, or None when the cue was left as it was
    reason: str = ""  # why it was left as it was


def _clock(seconds: float) -> str:
    return f"{int(seconds // 60)}:{seconds % 60:06.3f}"


def fades_to_audio(
    project: MadProject,
    show: Show,
    patched: Iterable[tuple[str, Cue, int, int]],
    threshold_db: float = DEFAULT_SILENCE_DB,
) -> list[FadeResult]:
    """Fade each patched cue in over the silence that follows it in its song.

    A cue whose effect fires exactly once in the show, at a moment its song's
    audio is silent (quieter than `threshold_db` dBFS), gets a fade lasting
    until the audio becomes audible, to a tenth of a second. Every other cue
    keeps its fade: an effect used several times (e.g. a blackout), a song
    without audio, or audio already playing when the cue fires.
    """
    results = []
    for effect_id, cue, _note, _channel in patched:
        old_fade = trigger_fade(project, cue)

        def keep(reason: str) -> None:
            results.append(FadeResult(effect_id, cue, old_fade, None, reason))

        fired = [
            (track, event)
            for track in show.audio_tracks
            for events in track.events.values()
            for event in events
            if event.effect_id == effect_id
        ]
        if len(fired) != 1:
            keep(f"fires {len(fired)} times" if fired else "never fires")
            continue
        track, event = fired[0]
        if not track.file_path:
            keep(f"song {track.name!r} has no audio file")
            continue
        at = event.timestamp_seconds
        try:
            silence = silence_after(Path(track.file_path), at, threshold_db)
        except AudioError as error:
            keep(str(error))
            continue
        if silence < MIN_SILENCE_FOR_FADE:
            keep(f"{track.name!r} is already audible at {_clock(at)}")
            continue
        fade = round(silence, 1)
        set_trigger_fade(project, cue, fade)
        results.append(FadeResult(effect_id, cue, old_fade, fade))
    return results
