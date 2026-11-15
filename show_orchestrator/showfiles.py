"""Files a show keeps besides its definition and media: the MadMapper project its projections run in, and its
lighting, the QLC+ workspace and the lights' MIDI files that ReQLC publishes. All under the show's folder:

    <home>/madmapper/<project>                the MadMapper project to patch (.mad, or a .madproject folder zipped)
    <home>/madmapper/project.json             its name, size, when and by whom it was uploaded
    <home>/madmapper/previous/<stamp>/        the projects it replaced, the last KEEP_PREVIOUS of them
    <home>/lighting/<workspace>.qxw           the QLC+ workspace
    <home>/lighting/fixtures/*.qxf            its own fixture definitions
    <home>/lighting/midi/*.mid                MIDI files published that the show doesn't use (yet)
    <home>/lighting/manifest.json             where the set came from (ReQLC show, version, who) and its files
    <home>/lighting/.previous/                the set it replaced, with the show's MIDI files it overwrote

The lights' MIDI files themselves go where the show definition has them (e.g. luces_midi/MS 3 Tap.mid): a published
file named like one the show uses replaces it, so the show plays what ReQLC sent without editing the show.

<home> is the show's folder when it is named after the show (shows/musical_2026/musical_2026.yaml), else a folder
named after the show inside it (shows/castillo/castillo_afuera_a/), so shows sharing a folder keep their own files.
"""
from __future__ import annotations

import hashlib
import io
import json
import functools
import shutil
import threading
import uuid
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import mido

from show_orchestrator.models import Show

KEEP_PREVIOUS = 5
PROJECT_INFO = "project.json"
MANIFEST = "manifest.json"
MIDI_SUFFIXES = {".mid", ".midi"}
MAX_LIGHTING_BYTES = 200 * 1024**2


_writing = threading.Lock()


def _one_at_a_time(function):
    """Changes to these files run one at a time: two uploads at once would move each other's files away."""
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        with _writing:
            return function(*args, **kwargs)
    return wrapper


def show_home(show_file: Path | str) -> PurePosixPath:
    """The folder (relative to root) a show's own files go in."""
    show_file = PurePosixPath(Path(show_file).as_posix())
    folder = show_file.parent
    if folder == PurePosixPath("."):
        folder = PurePosixPath("shows")
    return folder if folder.name == show_file.stem else folder / show_file.stem


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write(path: Path, data: bytes) -> None:
    """Write through a .part file, so a cut-off write never leaves half a file under the real name."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    partial.write_bytes(data)
    partial.replace(path)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: dict) -> None:
    _write(path, json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8"))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --- the MadMapper project

def madmapper_dir(root: Path, show_file: Path | str) -> Path:
    return root / show_home(show_file) / "madmapper"


def _project_in(folder: Path) -> dict | None:
    """The project stored in `folder` (its info, with `path`), or None."""
    info = _read_json(folder / PROJECT_INFO)
    if not info or not info.get("name"):
        return None
    path = folder / info["name"]
    if not path.is_file():
        return None
    return {**info, "path": path, "size": path.stat().st_size}


def stored_project(root: Path, show_file: Path | str) -> dict | None:
    """The show's MadMapper project: {name, size, uploaded_at, by, path}, or None."""
    return _project_in(madmapper_dir(root, show_file))


def previous_projects(root: Path, show_file: Path | str) -> list[dict]:
    """The projects the current one replaced, newest first, each with its `id` (to download it by)."""
    folder = madmapper_dir(root, show_file) / "previous"
    found = []
    for entry in sorted(folder.iterdir(), reverse=True) if folder.is_dir() else []:
        project = _project_in(entry) if entry.is_dir() else None
        if project:
            found.append({**project, "id": entry.name})
    return found


def previous_project(root: Path, show_file: Path | str, project_id: str) -> dict | None:
    return next((p for p in previous_projects(root, show_file) if p["id"] == project_id), None)


@_one_at_a_time
def store_project(root: Path, show_file: Path | str, source: Path, name: str, by: str | None) -> dict:
    """Make `source` (an upload, checked already) the show's MadMapper project under `name`; the one it replaces
    goes to previous/, which keeps the last KEEP_PREVIOUS."""
    folder = madmapper_dir(root, show_file)
    folder.mkdir(parents=True, exist_ok=True)
    current = _project_in(folder)
    if current:
        stamp = (current.get("uploaded_at") or _now()).replace("-", "").replace(":", "").replace("T", "-").rstrip("Z")
        target = folder / "previous" / stamp
        while target.exists():
            target = target.with_name(target.name + "-1")
        target.mkdir(parents=True)
        current["path"].replace(target / current["name"])
        (folder / PROJECT_INFO).replace(target / PROJECT_INFO)
        for old in sorted((folder / "previous").iterdir(), reverse=True)[KEEP_PREVIOUS:]:
            shutil.rmtree(old, ignore_errors=True)
    for stray in folder.iterdir():  # anything else left in the folder (e.g. a project.json without its file)
        if stray.is_file():
            stray.unlink()
    partial = folder / (name + ".part")
    shutil.copyfile(source, partial)
    partial.replace(folder / name)
    info = {"name": name, "uploaded_at": _now(), "by": by}
    _write_json(folder / PROJECT_INFO, info)
    return stored_project(root, show_file)


def public_project(project: dict | None) -> dict | None:
    """A stored project's info for the API (without its path on the server)."""
    return None if project is None else {k: v for k, v in project.items() if k != "path"}


# --- the lighting

def lighting_dir(root: Path, show_file: Path | str) -> Path:
    return root / show_home(show_file) / "lighting"


@dataclass
class WantedMidi:
    """A MIDI file the show plays: where it goes and in which songs."""
    path: str  # as the show gives it, relative to root
    songs: list[str] = field(default_factory=list)
    tracks: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return PurePosixPath(self.path).name


def wanted_midi(show: Show) -> list[WantedMidi]:
    """Every MIDI file the show plays (a song's file, its extra tracks), once each, in show order."""
    wanted: dict[str, WantedMidi] = {}

    def add(path: str | None, song: str, track: str) -> None:
        if path and PurePosixPath(path.replace("\\", "/")).suffix.lower() in MIDI_SUFFIXES:
            entry = wanted.setdefault(path, WantedMidi(path))
            if song not in entry.songs:
                entry.songs.append(song)
            if track not in entry.tracks:
                entry.tracks.append(track)

    for track in show.audio_tracks:
        add(track.file_path, track.name, track.name)
        for extra in track.extra_tracks or []:
            add(extra.file_path, track.name, extra.name)
    return list(wanted.values())


def _target(root: Path, path: str) -> Path:
    """A show's MIDI path as a file under root; ValueError when it points outside."""
    full = (root / path).resolve()
    try:
        full.relative_to(root.resolve())
    except ValueError:
        raise ValueError(f"the show's MIDI file {path} is outside the show folders") from None
    return full


def _describe(path: Path) -> dict | None:
    if not path.is_file():
        return None
    return {"name": path.name, "size": path.stat().st_size, "sha256": _file_hash(path)}


def lighting_state(root: Path, show_file: Path | str, show: Show) -> dict:
    """What the show has: its workspace, fixtures, the MIDI files it plays (present or not) and those it doesn't
    use, and the manifest of the last publish."""
    folder = lighting_dir(root, show_file)
    workspaces = sorted(folder.glob("*.qxw")) if folder.is_dir() else []
    midi = []
    for want in wanted_midi(show):
        try:
            described = _describe(_target(root, want.path))
        except ValueError:
            described = None
        midi.append({"name": want.name, "path": want.path, "songs": want.songs, "tracks": want.tracks,
                     "present": described is not None,
                     **({"size": described["size"], "sha256": described["sha256"]} if described else {})})
    return {
        "home": str(show_home(show_file)),
        "workspace": _describe(workspaces[0]) if workspaces else None,
        "fixtures": [_describe(p) for p in sorted((folder / "fixtures").glob("*.qxf"))],
        "midi": midi,
        "unused_midi": [_describe(p) for p in sorted((folder / "midi").glob("*")) if p.suffix.lower() in MIDI_SUFFIXES],
        "manifest": _read_json(folder / MANIFEST),
    }


def lighting_files(root: Path, show_file: Path | str) -> list[PurePosixPath]:
    """The stored lighting files (workspace, fixtures, unused MIDI, manifest) relative to root, for a bundle.
    The MIDI files the show plays are in it already, as the show's media."""
    folder = lighting_dir(root, show_file)
    if not folder.is_dir():
        return []
    files = [*folder.glob("*.qxw"), *(folder / "fixtures").glob("*.qxf"), *(folder / "midi").glob("*"),
             folder / MANIFEST]
    return [PurePosixPath(p.relative_to(root).as_posix()) for p in sorted(files) if p.is_file()]


def _local_tag(element: ElementTree.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _check_xml(data: bytes, root_tag: str, name: str, what: str) -> None:
    try:
        element = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise ValueError(f"{name} is not a {what}: {exc}") from None
    if _local_tag(element) != root_tag:
        raise ValueError(f"{name} is not a {what} (its root is <{_local_tag(element)}>, not <{root_tag}>)")


def _check_midi(data: bytes, name: str) -> None:
    try:
        mido.MidiFile(file=io.BytesIO(data))
    except (OSError, ValueError, EOFError, KeyError, IndexError) as exc:
        raise ValueError(f"{name} is not a MIDI file: {exc}") from None


def _status(old: Path, data: bytes) -> str:
    if not old.is_file():
        return "new"
    return "unchanged" if _file_hash(old) == sha256(data) else "changed"


@_one_at_a_time
def publish_lighting(
    root: Path,
    show_file: Path | str,
    show: Show,
    workspace: tuple[str, bytes],
    fixtures: list[tuple[str, bytes]],
    midi: list[tuple[str, bytes]],
    manifest: dict,
    by: str | None,
    dry_run: bool = False,
) -> dict:
    """Replace the show's lighting with a complete set: the workspace, its fixture definitions and the MIDI files
    (names only, no folders). A MIDI file named like one the show plays replaces it where the show has it; others
    are kept in lighting/midi/. The set replaced is kept in lighting/.previous/. Returns what changed; ValueError
    for a set that doesn't check out, before anything is written."""
    names = [workspace[0], *(n for n, _ in fixtures), *(n for n, _ in midi)]
    for name in names:
        if not name or "/" in name or "\\" in name or name.startswith("."):
            raise ValueError(f"not a usable file name: {name!r}")
    if len({n.lower() for n, _ in midi}) != len(midi) or len({n.lower() for n, _ in fixtures}) != len(fixtures):
        raise ValueError("the same file name is sent twice")
    if sum(len(d) for d in (workspace[1], *(d for _, d in fixtures), *(d for _, d in midi))) > MAX_LIGHTING_BYTES:
        raise ValueError(f"the lighting is larger than {MAX_LIGHTING_BYTES // 1024**2} MB")
    if not workspace[0].lower().endswith(".qxw"):
        raise ValueError(f"the workspace must be a .qxw file, not {workspace[0]}")
    _check_xml(workspace[1], "Workspace", workspace[0], "QLC+ workspace")
    for name, data in fixtures:
        if not name.lower().endswith(".qxf"):
            raise ValueError(f"fixture definitions are .qxf files, not {name}")
        _check_xml(data, "FixtureDefinition", name, "QLC+ fixture definition")
    for name, data in midi:
        if PurePosixPath(name).suffix.lower() not in MIDI_SUFFIXES:
            raise ValueError(f"only MIDI files (.mid) go with the workspace, not {name}")
        _check_midi(data, name)

    folder = lighting_dir(root, show_file)
    by_name: dict[str, list[WantedMidi]] = {}
    for want in wanted_midi(show):
        by_name.setdefault(want.name.lower(), []).append(want)
    targets = {want.path: _target(root, want.path) for wants in by_name.values() for want in wants}

    old_workspaces = sorted(folder.glob("*.qxw")) if folder.is_dir() else []
    report: dict = {
        "dry_run": dry_run,
        "workspace": {"name": workspace[0], "status": "new" if not old_workspaces else (
            "unchanged" if old_workspaces[0].name == workspace[0] and _file_hash(old_workspaces[0]) == sha256(workspace[1])
            else "changed")},
        "fixtures": [{"name": n, "status": _status(folder / "fixtures" / n, d)} for n, d in fixtures],
        "removed_fixtures": sorted(p.name for p in (folder / "fixtures").glob("*.qxf")
                                   if p.name.lower() not in {n.lower() for n, _ in fixtures}),
        "midi": [],
        "missing": [],
    }
    writes: list[tuple[Path, bytes]] = []
    sent = set()
    for name, data in midi:
        wants = by_name.get(name.lower())
        if not wants:
            report["midi"].append({"name": name, "path": None, "songs": [], "status": "unused"})
            continue
        sent.add(name.lower())
        for want in wants:
            target = targets[want.path]
            status = _status(target, data)
            report["midi"].append({"name": name, "path": want.path, "songs": want.songs, "status": status})
            if status != "unchanged":
                writes.append((target, data))
    for key, wants in by_name.items():
        if key not in sent:
            for want in wants:
                report["missing"].append({"name": want.name, "path": want.path, "songs": want.songs,
                                          "present": targets[want.path].is_file()})
    stored_manifest = {
        **{k: v for k, v in manifest.items() if k not in ("files", "published_at", "published_by")},
        "published_at": _now(),
        "published_by": by,
        "files": {
            "workspace": {"name": workspace[0], "sha256": sha256(workspace[1]), "size": len(workspace[1])},
            "fixtures": [{"name": n, "sha256": sha256(d), "size": len(d)} for n, d in fixtures],
            "midi": [{"name": n, "sha256": sha256(d), "size": len(d),
                      "paths": [w.path for w in by_name.get(n.lower(), [])]} for n, d in midi],
        },
    }
    report["manifest"] = stored_manifest
    if dry_run:
        return report

    # The set being replaced, and the show's MIDI files about to be overwritten, go to .previous/ first.
    folder.mkdir(parents=True, exist_ok=True)
    previous_new = folder / f".previous-{uuid.uuid4().hex[:8]}"
    previous_new.mkdir()
    for entry in folder.iterdir():
        if entry.name.startswith("."):
            continue
        if entry.is_dir():  # fixtures/ and midi/ are written afresh
            shutil.move(str(entry), previous_new / entry.name)
        else:
            shutil.copy2(entry, previous_new / entry.name)
    for target, _ in writes:
        if target.is_file():
            keep = previous_new / "replaced" / target.relative_to(root.resolve()).as_posix()
            keep.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, keep)

    for old in old_workspaces:
        old.unlink(missing_ok=True)
    (folder / MANIFEST).unlink(missing_ok=True)
    _write(folder / workspace[0], workspace[1])
    for name, data in fixtures:
        _write(folder / "fixtures" / name, data)
    for name, data in midi:
        if name.lower() not in by_name:
            _write(folder / "midi" / name, data)
    for target, data in writes:
        _write(target, data)
    _write_json(folder / MANIFEST, stored_manifest)

    old_previous = folder / ".previous"
    if old_previous.exists():
        shutil.rmtree(old_previous)
    previous_new.rename(old_previous)
    return report
