"""A show as one zip: its REAPER project, the MIDI files, the show definition with its note lock and included
files, and every media file, so it opens on a machine that has none of them.

    <show>/<show>.rpp                    the project; open it in REAPER
    <show>/*.mid                         the generated cues (and split MIDI pieces)
    <show>/shows/<...>/<show>.yaml       the show definition, its .notes.lock and included files,
    <show>/shows/<...>/media/...         and its media, at the paths the show gives them
    <show>/shows/<...>/lighting/...      the QLC+ workspace and fixtures ReQLC published (see showfiles.py)
    <show>/madmapper/<project>           the show's MadMapper project, patched to the show's notes
    <show>/READ ME FIRST.txt             how to open it

The project references the media at those same paths (relative to its folder), so nothing is stored twice, and
the folder is also a show-orchestrator root: `python main.py shows/<...>/<show>.yaml` run inside it rebuilds the
project.
"""
from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from show_orchestrator.backends.reaper import ReaperBackend, reaper_settings
from show_orchestrator.generator import DEFAULT_VELOCITY, MidiGenerator
from show_orchestrator.madupload import patch_upload
from show_orchestrator.models import Show
from show_orchestrator.notes import lock_path_for
from show_orchestrator.parser import Parser
from show_orchestrator.showfiles import lighting_files, stored_project

# Already compressed: stored as they are, which is as small and much faster.
STORED_SUFFIXES = {".mp3", ".m4a", ".aac", ".ogg", ".flac", ".mp4", ".mpeg", ".mpg", ".mov", ".webm", ".zip"}

README = """{name}
{underline}

Extract the whole zip first, then open {name}.rpp from the extracted folder. Opened straight from inside
the zip, REAPER gets the project alone and reports every audio and MIDI file as missing.

{name}.rpp                 the REAPER project; its media is referenced at shows/... below
*.mid                      the cues it plays (and the pieces of split MIDI files)
shows/                     the show definition, its note lock and its media{lighting}{madmapper}

The folder is also a show-orchestrator root: `python main.py {show_file}` run inside it builds the project again.
"""


def _inside(root: Path, path: Path, what: str) -> PurePosixPath:
    """`path` (relative to root, or absolute) as a path relative to root; refused when it is outside it."""
    full = (path if path.is_absolute() else root / path).resolve()
    try:
        relative = full.relative_to(root.resolve())
    except ValueError:
        raise ValueError(f"{what} {path} is outside the show folders, so it can't go into a bundle; "
                         f"move it under shows/ and change the show to match") from None
    if not full.is_file():
        raise FileNotFoundError(f"{what} not found: {path}")
    return PurePosixPath(relative.as_posix())


def show_dependencies(root: Path, show_file: Path, show: Show) -> list[PurePosixPath]:
    """Every file `show` needs, relative to root: the definition, its note lock, included files and media.
    Media paths, like main.py's, are relative to root (the working directory); includes to the show file."""
    files = [_inside(root, show_file, "Show file")]
    lock = lock_path_for(root / show_file)
    if lock.is_file():
        files.append(_inside(root, lock, "Note lock"))
    for include in show.include or []:
        path = Path(include)
        files.append(_inside(root, path if path.is_absolute() else (root / show_file).parent / path, "Included file"))
    for track in show.audio_tracks:
        if track.file_path:
            files.append(_inside(root, Path(track.file_path), "Media file"))
        for extra in track.extra_tracks or []:
            files.append(_inside(root, Path(extra.file_path), "Media file"))
    return list(dict.fromkeys(files))


def write_bundle(
    root: Path,
    show_file: Path,
    out: BinaryIO,
    velocity: int = DEFAULT_VELOCITY,
    gap: float | None = None,
    routing: str | None = None,
    note_lock: bool = True,
) -> list[str]:
    """Build `show_file` (relative to root) and write the zip to `out`. `gap` and `routing` as for a build: None
    takes the show's own settings. Media paths are read relative to root, so root must be the working directory.
    Returns warnings: things the bundle has but not as they should be (a MadMapper project that would not patch)."""
    show_file = Path(show_file)
    name = show_file.stem
    show = Parser().load_show(root / show_file)
    files = show_dependencies(root, show_file, show)
    gap, routing_map = reaper_settings(show, gap, routing)
    with tempfile.TemporaryDirectory() as tmp:
        build = Path(tmp)
        lock_path = lock_path_for(root / show_file) if note_lock else None
        # generated first: it may add notes to the lock, which then goes into the zip as the project uses it
        midi_files = MidiGenerator(velocity=velocity).generate_midi_files(show, build, lock_path)
        if lock_path is not None and lock_path.is_file():
            lock = _inside(root, lock_path, "Note lock")
            if lock not in files:
                files.append(lock)
        backend = ReaperBackend()
        backend.create_project(show, midi_files, build, song_gap=gap, routing=routing_map, media_in_place=True)
        backend.save_project(build / f"{name}.rpp")
        warnings = []

        lighting = [f for f in lighting_files(root, show_file) if f not in files]
        madmapper = []  # (file, name in the zip)
        project = stored_project(root, show_file)
        if project:
            try:
                _, patched = patch_upload(project["path"], project["name"], show, lock_path, build / ".madmapper")
                madmapper.append((patched, patched.name))
            except (ValueError, OSError) as exc:  # MadFileError and CueMatchError are ValueErrors
                warnings.append(f"The MadMapper project {project['name']} could not be patched ({exc}); "
                                f"the bundle has it as it was uploaded.")
                madmapper.append((project["path"], project["name"]))

        with zipfile.ZipFile(out, "w") as bundle:
            def add(path: Path, arcname: str) -> None:
                stored = path.suffix.lower() in STORED_SUFFIXES
                bundle.write(path, f"{name}/{arcname}", zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED)

            readme = README.format(
                name=name, underline="=" * len(name), show_file=PurePosixPath(show_file.as_posix()),
                lighting="\n  .../lighting/            the QLC+ workspace and fixtures (from ReQLC)" if lighting else "",
                madmapper="\nmadmapper/                 the MadMapper project, patched to the show's notes" if madmapper else "")
            # CRLF: Notepad on an older Windows shows LF-only text as one line
            bundle.writestr(f"{name}/READ ME FIRST.txt", readme.replace("\n", "\r\n"))
            for path in sorted(build.rglob("*")):
                relative = path.relative_to(build)
                if path.is_file() and relative.parts[0] != ".madmapper":
                    add(path, relative.as_posix())
            for relative in [*files, *lighting]:
                add(root / relative, str(relative))
            for path, arcname in madmapper:
                add(path, f"madmapper/{arcname}")
    return warnings
