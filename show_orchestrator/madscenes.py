"""Scaffold scenes in a MadMapper project.

For every scene this adds a surface group holding copies of a template (a quad,
or every surface of a template group) that show the scene's video or image,
and a trigger that shows that scene's group, hides every other scene's group
and restarts the scene's video. Optionally it adds a blackout trigger that
hides every top-level surface. In MadMapper 5 a trigger is a cue; in MadMapper
6, which replaced cues with timelines, it is a timeline with one keyframe per
setting.

How the project stores these:
- `surfaces` is a flat list of quads, groups...; `surfacesRelations[i]` is the
  list index of surface i's group, or -1 at the top level.
- `visuals.visuals` lists the media; a surface shows the one whose `id` is its
  `visualId` (-1 for none).
- MadMapper 5 cues sit in `cueBanksV4.cueBanks[].cues`, a grid where `{}` is an
  empty slot; each cue entry sets one parameter, e.g. `/surfaces/12/visible`.
- MadMapper 6 timelines sit in `timelineBanks.timelineBanks[].timelines`; each
  track names its parameter by an id that is the FNV-1a hash of the
  parameter's address, registered in the bank's `attributeRegistryMap`.
  MadMapper 6 calls media `/media/<id>` where MadMapper 5 said `/medias/<id>`.

New items copy an existing one of the same kind where the project has one, so
they pick up the project's own settings, and fall back to built-in defaults
captured from real MadMapper projects otherwise.
"""
import copy
import random
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from show_orchestrator.madfile import (
    T_BOOL, T_DOUBLE, T_FLOAT, T_IMAGE, T_INT, T_LIST, T_MAP,
    T_STRING, T_TRANSFORM, T_UINT, T_ULONGLONG, T_USER, T_BYTEARRAY, T_COLOR,
    T_POINTF, MadProject, Variant, VariantMap, get, set_value,
)
from show_orchestrator.madbanks import TriggerBanks, timeline_transition

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp", ".tga"}
MOVIE_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".mpg", ".mpeg", ".wmv"}

# Media ids count up from 1, but MadMapper 6 also keeps a few internal media
# (timeline media tracks) with ids above a billion; those don't count.
MAX_REGULAR_VISUAL_ID = 1 << 24

_UNIT_SQUARE = (  # QPolygonF of a group's whole-media UVs
    b"\x00\x00\x00\x04" + b"\x00" * 16 + b"?\xf0" + b"\x00" * 14
    + b"?\xf0" + b"\x00" * 6 + b"?\xf0" + b"\x00" * 14 + b"?\xf0" + b"\x00" * 6
)
_IDENTITY_TRANSFORM = b"".join(
    (b"?\xf0" + b"\x00" * 6) if row == col else b"\x00" * 8
    for row in range(3) for col in range(3)
)
_DEFAULT_COLOR = b"\x01\xff\xff" + b"\x00" * 8  # QColor: opaque black, MadMapper's default
_HSV_WHITE = (
    b"MadColor\x00",
    b"\x00\x00\x00\x06\x00h\x00s\x00v" + b"\x00" * 16 + (b"?\xf0" + b"\x00" * 6) * 2,
)


class ScaffoldError(ValueError):
    pass


@dataclass
class Scene:
    name: str
    media: Path | None = None  # None: the scene's surfaces show no media


@dataclass
class ScaffoldReport:
    # (scene name, group surface id, ids of the surfaces in it, media id or None)
    scenes: list[tuple[str, int, list[int], int | None]] = field(default_factory=list)
    blackout: str | None = None
    reused_media: list[Path] = field(default_factory=list)
    unknown_durations: list[Path] = field(default_factory=list)
    trigger_kind: str = "cue"  # "cue" (MadMapper 5) or "timeline" (MadMapper 6)
    bank: str = ""


@dataclass
class _Setting:
    """One parameter a trigger sets, e.g. a surface's visibility."""
    address: str  # /surfaces/12/visible
    owner: str  # /surfaces/12
    display_path: str  # /Surfaces/Intro/Visible
    value: Variant
    instant: bool = False  # jump to the value instead of fading with the trigger


def fnv1a64(text: str) -> int:
    """The id MadMapper 6 gives a parameter address in timeline tracks."""
    value = 0xCBF29CE484222325
    for byte in text.encode("utf-8"):
        value = ((value ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return value


def probe_media(path: Path) -> tuple[float | None, bool]:
    """(duration in seconds, has audio) of a movie, via ffprobe when installed."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
             "-of", "default=noprint_wrappers=1", str(path)],
            capture_output=True, text=True, timeout=30, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None, False
    duration, has_audio = None, False
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        if key == "duration":
            try:
                duration = float(value)
            except ValueError:
                pass
        elif key == "codec_type" and value == "audio":
            has_audio = True
    return duration, has_audio


def _qmap(pairs: list[tuple[str, Variant]], ascending: bool) -> Variant:
    """A map with its keys in the order this MadMapper version writes them."""
    return (T_MAP, sorted(pairs, key=lambda pair: pair[0], reverse=not ascending), 0)


def _string(text: str) -> Variant:
    return (T_STRING, text, 0)


def _value(var: Variant | None):
    return None if var is None else var[1]


class _Project:
    """The parts of a loaded project the scaffolder edits."""

    def __init__(self, project: MadProject) -> None:
        self.root = project.root
        self.banks = TriggerBanks(self.root)
        if self.banks.layout is None:
            raise ScaffoldError("the project has neither cue banks nor timeline banks")
        self.is_mm6 = self.banks.is_mm6
        # MadMapper 5 writes map keys in descending order, MadMapper 6 ascending.
        self.ascending = self.is_mm6
        self.surfaces = get(self.root, "surfaces")[1]
        self.relations = get(self.root, "surfacesRelations")[1]
        self.visuals = get(get(self.root, "visuals")[1], "visuals")[1]

    # -- surfaces ------------------------------------------------------------

    def surface_index(self, name: str) -> int:
        found = [
            i for i, surface in enumerate(self.surfaces)
            if _value(get(surface[1], "name")) == name
        ]
        if not found:
            raise ScaffoldError(f"no surface named {name!r} in the project")
        if len(found) > 1:
            raise ScaffoldError(f"several surfaces are named {name!r}; rename one")
        return found[0]

    def template(self, name: str) -> tuple[Variant | None, list[Variant]]:
        """(the template group or None, the surfaces to copy into each scene)."""
        index = self.surface_index(name)
        surface = self.surfaces[index]
        if _value(get(surface[1], "type")) != "group":
            return None, [surface]
        children = [
            self.surfaces[i] for i, parent in enumerate(self.relations) if parent[1] == index
        ]
        if not children:
            raise ScaffoldError(f"template group {name!r} is empty")
        if any(_value(get(child[1], "type")) == "group" for child in children):
            raise ScaffoldError(f"template group {name!r} holds groups; only one level is copied")
        return surface, children

    def surface_names(self) -> set[str]:
        return {_value(get(surface[1], "name")) for surface in self.surfaces}

    def next_surface_id(self) -> int:
        return max((_value(get(s[1], "surfaceId")) for s in self.surfaces), default=0) + 1

    def add_surface(self, surface: Variant, parent_index: int) -> int:
        self.surfaces.append(surface)
        self.relations.append((T_INT, parent_index, 0))
        return len(self.surfaces) - 1

    def top_level_surfaces(self) -> list[tuple[int, str]]:
        return [
            (_value(get(surface[1], "surfaceId")), _value(get(surface[1], "name")))
            for surface, parent in zip(self.surfaces, self.relations) if parent[1] == -1
        ]

    def group_template(self) -> Variant:
        for surface in self.surfaces:
            if _value(get(surface[1], "type")) == "group":
                return surface
        return _qmap([
            ("blendMode", (T_UINT, 2, 0)),
            ("collapsedParameterGroups", (T_MAP, [], 0)),
            ("customSettings", (T_MAP, [], 0)),
            ("expanded", (T_BOOL, False, 0)),
            ("feathering", (T_DOUBLE, 0.0, 0)),
            ("geometryPrecision", (T_DOUBLE, 0.5, 0)),
            ("isFixtureGroup", (T_BOOL, False, 0)),
            ("locked", (T_BOOL, False, 0)),
            ("modulation", (T_USER, _HSV_WHITE, 0)),
            ("name", _string("Group")),
            ("opacity", (T_DOUBLE, 1.0, 0)),
            ("perspective", (T_TRANSFORM, _IDENTITY_TRANSFORM, 0)),
            ("perspectiveUv", (T_TRANSFORM, _IDENTITY_TRANSFORM, 0)),
            ("position", (T_POINTF, (0.0, 0.0), 0)),
            ("positionUv", (T_POINTF, (0.5, 0.5), 0)),
            *([("previewed", (T_BOOL, True, 0))] if self.is_mm6 else []),
            ("rotation", (T_DOUBLE, 0.0, 0)),
            ("rotationUv", (T_DOUBLE, 0.0, 0)),
            ("scale", (T_DOUBLE, 1.0, 0)),
            ("scaleUv", (T_DOUBLE, 1.0, 0)),
            ("surfaceId", (T_INT, 0, 0)),
            ("surfaceType", (T_INT, 0, 0)),
            ("type", _string("group")),
            ("uvFlip", (T_UINT, 0, 0)),
            ("uvs", (T_BYTEARRAY, _UNIT_SQUARE, 0)),
            ("visible", (T_BOOL, False, 0)),
            ("visualId", (T_INT, -1, 0)),
        ], self.ascending)

    # -- media ---------------------------------------------------------------

    def media_by_url(self) -> dict[str, int]:
        return {
            _value(get(visual[1], "url")): _value(get(visual[1], "id"))
            for visual in self.visuals
        }

    def media(self, media_id: int) -> VariantMap:
        for visual in self.visuals:
            if _value(get(visual[1], "id")) == media_id:
                return visual[1]
        raise ScaffoldError(f"no media with id {media_id}")

    def add_media(self, path: Path, report: ScaffoldReport) -> int:
        url = path.resolve().as_posix()
        existing = self.media_by_url().get(url)
        if existing is not None:
            report.reused_media.append(path)
            return existing

        suffix = path.suffix.lower()
        if suffix in IMAGE_EXTENSIONS:
            media_type, mm_type = "Image", "Images"
        elif suffix in MOVIE_EXTENSIONS:
            media_type, mm_type = "Movie", "Movies"
        else:
            raise ScaffoldError(f"{path.name}: not a video or image type MadMapper takes")

        same_type = [v for v in self.visuals if _value(get(v[1], "type")) == media_type]
        if same_type:
            visual = copy.deepcopy(same_type[-1])
        else:
            visual = self._default_media(media_type, mm_type)
        pairs = visual[1]

        regular_ids = [
            _value(get(v[1], "id")) for v in self.visuals
            if _value(get(v[1], "id")) < MAX_REGULAR_VISUAL_ID
        ]
        media_id = max(regular_ids, default=0) + 1
        grid_pos = max(
            (_value(get(v[1], "gridPos")) for v in self.visuals
             if _value(get(v[1], "mmtype")) == mm_type),
            default=-1,
        ) + 1
        set_value(pairs, "id", (T_INT, media_id, 0))
        set_value(pairs, "gridPos", (T_INT, grid_pos, 0))
        set_value(pairs, "url", _string(url))
        set_value(pairs, "name", _string(path.name))
        set_value(pairs, "instanceThumbnail", (T_IMAGE, b"\x00\x00\x00\x00", 0))
        set_value(pairs, "instanceThumbnailId", (T_INT, random.randint(-2**31, 2**31 - 1), 0))

        if media_type == "Movie":
            attributes = get(pairs, "attributes")[1]
            duration, has_audio = probe_media(path)
            if duration is None:
                report.unknown_durations.append(path)
            else:
                set_value(attributes, "loop_end", (T_DOUBLE, duration, 0))
            set_value(attributes, "loop_start", (T_INT, 0, 0))
            set_value(attributes, "HAS_AUDIO_TRACK", (T_BOOL, has_audio, 0))
        self.visuals.append(visual)
        return media_id

    def _default_media(self, media_type: str, mm_type: str) -> Variant:
        attributes = [
            ("HAS_AUDIO_TRACK", (T_BOOL, False, 0)),
            ("audio_enabled", (T_BOOL, False, 0)),
            ("audio_level", (T_INT, 1, 0)),
        ]
        if media_type == "Movie":
            attributes += [
                ("Color Profile", _string("Auto")),
                ("USE_HARDWARE_ACCELERATION", (T_BOOL, False, 0)),
                ("is_playing", (T_BOOL, True, 0)),
                ("loop_end", (T_DOUBLE, 0.0, 0)),
                ("loop_start", (T_INT, 0, 0)),
                ("looping_mode", _string("PlayOnce")),
                ("speed", (T_INT, 1, 0)),
            ]
        return _qmap([
            ("attributes", _qmap(attributes, self.ascending)),
            ("collapsedParameterGroups", (T_MAP, [], 0)),
            ("gridPos", (T_INT, 0, 0)),
            ("id", (T_INT, 0, 0)),
            ("instanceThumbnail", (T_IMAGE, b"\x00\x00\x00\x00", 0)),
            ("instanceThumbnailId", (T_INT, 0, 0)),
            ("mmtype", _string(mm_type)),
            ("name", _string("")),
            ("syphonAppName", _string("")),
            ("type", _string(media_type)),
            ("url", _string("")),
        ], self.ascending)

    # -- settings a trigger can make --------------------------------------------

    def visibility(self, surface_id: int, name: str, visible: bool) -> _Setting:
        return _Setting(
            f"/surfaces/{surface_id}/visible", f"/surfaces/{surface_id}",
            f"/Surfaces/{name}/Visible", (T_BOOL, visible, 0),
        )

    def restart(self, media_id: int) -> list[_Setting]:
        """Play a movie forward from its start, as MadMapper cues restart videos."""
        name = _value(get(self.media(media_id), "name"))
        root, label = ("media", "Media") if self.is_mm6 else ("medias", "Medias")
        owner = f"/{root}/{media_id}"
        return [
            _Setting(f"{owner}/play_mode", owner, f"/{label}/{name}/Play Mode",
                     _string("Forward")),
            _Setting(f"{owner}/position_sec", owner, f"/{label}/{name}/Position (Seconds)",
                     (T_DOUBLE, 0.0, 0), instant=True),
        ]

    # -- trigger banks ----------------------------------------------------------

    def bank(self, name: str | None) -> VariantMap:
        bank = self.banks.bank(name)
        if bank is None:
            names = ", ".join(repr(self.banks.bank_name(b)) for b in self.banks.banks)
            raise ScaffoldError(f"no bank named {name!r}; the project has {names}")
        return bank

    def trigger_template(self, bank: VariantMap) -> Variant | None:
        """An existing non-scene cue/timeline to copy settings from."""
        for entry in self.banks.triggers(bank):
            scene_flag = get(entry[1], "scene" if self.is_mm6 else "is_scene_cue")
            if not _value(scene_flag):
                return entry
        return None

    def cue(self, template: Variant | None, name: str, settings: list[_Setting],
            fade: float | None) -> Variant:
        """A MadMapper 5 cue making `settings`."""
        if template is not None:
            cue = copy.deepcopy(template)
        else:
            cue = _qmap([
                ("color", (T_COLOR, _DEFAULT_COLOR, 0)),
                ("comment", _string("")),
                ("entries", (T_LIST, [], 0)),
                ("is_scene_cue", (T_BOOL, False, 0)),
                ("name", _string("")),
                ("transition_settings", self._cue_transition(0.5, instant=False)),
            ], self.ascending)
        entries = [
            _qmap([
                ("display_path", _string(setting.display_path)),
                ("ownerUrl", _string(setting.owner)),
                ("transition_mode", (T_INT, 0 if setting.instant else 1, 0)),
                ("transition_settings", self._cue_transition(0.0, setting.instant)),
                ("url", _string(setting.address)),
                ("value", setting.value),
            ], self.ascending)
            for setting in settings
        ]
        set_value(cue[1], "name", _string(name))
        set_value(cue[1], "is_scene_cue", (T_BOOL, False, 0))
        set_value(cue[1], "comment", _string(""))
        set_value(cue[1], "entries", (T_LIST, entries, 0))
        if fade is not None:
            set_value(cue[1], "transition_settings", self._cue_transition(fade, instant=False))
        return cue

    def _cue_transition(self, duration: float, instant: bool) -> Variant:
        params = [("curve", (T_INT, 2, 0))] if instant else []
        return _qmap([
            ("duration", (T_FLOAT, duration, 0)),
            ("params", _qmap(params, self.ascending)),
            ("type", (T_ULONGLONG, 0, 0)),
        ], self.ascending)

    def timeline(self, template: Variant | None, bank: VariantMap, uid: int, name: str,
                 settings: list[_Setting], fade: float | None) -> Variant:
        """A MadMapper 6 timeline slot making `settings`, one keyframe each."""
        if template is not None:
            timeline = copy.deepcopy(get(template[1], "timeline"))
        else:
            timeline = _default_timeline()
        registry = self.banks.registry(bank)
        registered = {key for key, _ in registry}
        tracks = []
        for setting in settings:
            track_id = fnv1a64(setting.address)
            if str(track_id) not in registered:
                registry.append((str(track_id), _qmap([
                    ("dp", _string(setting.display_path)),
                    ("ou", _string(setting.owner)),
                    ("u", _string(setting.address)),
                ], True)))
                registered.add(str(track_id))
            track = [
                ("id", (T_ULONGLONG, track_id, 0)),
                ("it", (T_LIST, [_qmap([
                    ("tp", _string("vkf")),
                    ("v", setting.value),
                ], True)], 0)),
            ]
            if setting.instant:
                track.append(("tr", _qmap([("md", (T_INT, 0, 0))], True)))
            tracks.append(_qmap(track, True))
        registry.sort(key=lambda pair: pair[0])
        pairs = timeline[1]
        set_value(pairs, "name", _string(name))
        set_value(pairs, "uid", (T_INT, uid, 0))
        set_value(pairs, "animated", (T_BOOL, False, 0))
        set_value(pairs, "tracks", (T_LIST, tracks, 0))
        set_value(pairs, "clips", (T_LIST, [], 0))
        set_value(pairs, "markers", (T_LIST, [], 0))
        if fade is not None or get(pairs, "transition_settings") is None:
            set_value(pairs, "transition_settings", timeline_transition(1.0 if fade is None else fade))
        return _qmap([
            ("scene", (T_BOOL, False, 0)),
            ("timeline", timeline),
            ("uid", (T_INT, uid, 0)),
        ], True)


def _default_timeline() -> Variant:
    """A static timeline shell as MadMapper 6 writes one converted from a cue."""
    return _qmap([
        ("animated", (T_BOOL, False, 0)),
        ("clips", (T_LIST, [], 0)),
        ("color", (T_COLOR, _DEFAULT_COLOR, 0)),
        ("display_settings", _qmap([
            ("audioTrackDisplaySettings", (T_LIST, [], 0)),
            ("audioTrackForBackground", (T_ULONGLONG, 0, 0)),
            ("bpm", (T_DOUBLE, 120.0, 0)),
            ("collapsedNodes", (T_LIST, [], 0)),
            ("fps", (T_DOUBLE, 60.0, 0)),
            ("pixelDuration", (T_DOUBLE, 0.005, 0)),
            ("signatureDen", (T_INT, 4, 0)),
            ("signatureNum", (T_INT, 4, 0)),
            ("timeRangeLeft", (T_DOUBLE, 0.0, 0)),
            ("trackDisplaySettings", (T_LIST, [], 0)),
            ("trackOrderPerType", (T_MAP, [], 0)),
            ("viewVerticalOffset", (T_INT, 0, 0)),
            ("viewVerticalZoom", (T_DOUBLE, 1.0, 0)),
        ], True)),
        ("launch_settings", _qmap([
            ("quantizeDen", (T_INT, 4, 0)),
            ("quantizeNum", (T_INT, 0, 0)),
        ], True)),
        ("loop_settings", _qmap([
            ("enabled", (T_BOOL, False, 0)),
            ("end", (T_DOUBLE, 4.0, 0)),
            ("start", (T_DOUBLE, 0.0, 0)),
        ], True)),
        ("markers", (T_LIST, [], 0)),
        ("name", _string("")),
        ("playback_settings", _qmap([
            ("ltcChannelNumber", (T_INT, 0, 0)),
            ("ltcFramesPerSecond", (T_DOUBLE, 30.0, 0)),
            ("mode", _string("FixedSpeed")),
            ("speed", (T_FLOAT, 1.0, 0)),
            ("syncSource", _string("LTC")),
            ("syncSourceDeviceName", _string("")),
            ("timeCodeOffset", (T_DOUBLE, 0.0, 0)),
        ], True)),
        ("tracks", (T_LIST, [], 0)),
        ("transition_settings", timeline_transition(1.0)),
        ("uid", (T_INT, 0, 0)),
    ], True)


def scaffold_scenes(
    project: MadProject,
    scenes: list[Scene],
    template: str,
    bank_name: str | None = None,
    fade: float | None = None,
    blackout: str | None = None,
    restart_videos: bool = True,
) -> ScaffoldReport:
    """Add a group with copies of `template` and a trigger for every scene.

    `template` names a quad, copied once into each scene group, or a group,
    whose surfaces are all copied (and whose own settings the scene groups
    take). The copies show the scene's media, or none, and are named
    `<scene> - <template surface>`; scene groups start hidden.

    Each scene's trigger (a cue in MadMapper 5, a timeline in MadMapper 6) is
    named after the scene, shows its group, hides the other scene groups and,
    with `restart_videos`, plays its video from the start. `fade` sets the
    trigger's transition time in seconds. A `blackout` trigger by that name
    hides every top-level surface. Raises ScaffoldError, leaving the project
    unchanged, if a name is taken or the bank has too few free slots.
    """
    mad = _Project(project)
    report = ScaffoldReport(trigger_kind="timeline" if mad.is_mm6 else "cue", blackout=blackout)

    names = [scene.name for scene in scenes] + ([blackout] if blackout else [])
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ScaffoldError(f"names used twice: {', '.join(duplicates)}")
    template_group, template_surfaces = mad.template(template)
    bank = mad.bank(bank_name)
    report.bank = _value(get(bank, "bank_name"))
    slots = mad.banks.slots(bank)

    taken_surfaces = mad.surface_names()
    taken_triggers = {mad.banks.name(entry) for entry in mad.banks.triggers(bank)}
    for scene in scenes:
        new_names = [scene.name] + [
            f"{scene.name} - {_value(get(s[1], 'name'))}" for s in template_surfaces
        ]
        for name in new_names:
            if name in taken_surfaces:
                raise ScaffoldError(f"the project already has a surface named {name!r}")
        if scene.media is not None and not scene.media.is_file():
            raise ScaffoldError(f"{scene.name}: media file not found: {scene.media}")
    for name in names:
        if name in taken_triggers:
            raise ScaffoldError(f"bank {report.bank!r} already has a {report.trigger_kind} named {name!r}")
    free_slots = [i for i, slot in enumerate(slots) if mad.banks.is_free(slot)]
    if len(free_slots) < len(names):
        raise ScaffoldError(
            f"bank {report.bank!r} has {len(free_slots)} free slots for {len(names)} "
            f"{report.trigger_kind}s; add rows to it in MadMapper or pick another bank"
        )

    # Undo everything if a scene fails part-way, so the project is unchanged.
    surfaces_before = list(mad.surfaces)
    relations_before = list(mad.relations)
    visuals_before = list(mad.visuals)
    try:
        group_template = template_group or mad.group_template()
        groups: list[tuple[int, str, int | None]] = []
        for scene in scenes:
            media_id = mad.add_media(scene.media, report) if scene.media else None
            visual_id = (T_INT, -1 if media_id is None else media_id, 0)

            group = copy.deepcopy(group_template)
            group_id = mad.next_surface_id()
            set_value(group[1], "name", _string(scene.name))
            set_value(group[1], "surfaceId", (T_INT, group_id, 0))
            set_value(group[1], "visualId", visual_id)
            set_value(group[1], "visible", (T_BOOL, False, 0))
            set_value(group[1], "expanded", (T_BOOL, False, 0))
            group_index = mad.add_surface(group, -1)

            surface_ids = []
            for original in template_surfaces:
                surface = copy.deepcopy(original)
                surface_id = mad.next_surface_id()
                original_name = _value(get(original[1], "name"))
                set_value(surface[1], "name", _string(f"{scene.name} - {original_name}"))
                set_value(surface[1], "surfaceId", (T_INT, surface_id, 0))
                set_value(surface[1], "visualId", visual_id)
                set_value(surface[1], "visible", (T_BOOL, True, 0))
                mad.add_surface(surface, group_index)
                surface_ids.append(surface_id)

            groups.append((group_id, scene.name, media_id))
            report.scenes.append((scene.name, group_id, surface_ids, media_id))
    except Exception:
        mad.surfaces[:] = surfaces_before
        mad.relations[:] = relations_before
        mad.visuals[:] = visuals_before
        raise

    triggers: list[tuple[str, list[_Setting]]] = []
    for group_id, scene_name, media_id in groups:
        settings = [mad.visibility(gid, gname, gid == group_id) for gid, gname, _ in groups]
        if restart_videos and media_id is not None and \
                _value(get(mad.media(media_id), "type")) == "Movie":
            settings += mad.restart(media_id)
        triggers.append((scene_name, settings))
    if blackout:
        triggers.append((blackout, [
            mad.visibility(surface_id, name, False) for surface_id, name in mad.top_level_surfaces()
        ]))

    trigger_template = mad.trigger_template(bank)
    next_uid = max(mad.banks.uids(), default=0) + 1 if mad.is_mm6 else 0
    for slot_index, (name, settings) in zip(free_slots, triggers):
        if mad.is_mm6:
            mad.banks.put(bank, slot_index, mad.timeline(trigger_template, bank, next_uid, name, settings, fade))
            next_uid += 1
        else:
            mad.banks.put(bank, slot_index, mad.cue(trigger_template, name, settings, fade))
    return report
