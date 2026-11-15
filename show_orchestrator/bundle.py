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

import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from show_orchestrator.backends.reaper import ReaperBackend, reaper_settings
from show_orchestrator.generator import DEFAULT_VELOCITY, MidiGenerator
from show_orchestrator.madupload import SKIPPED, patch_upload
from show_orchestrator.models import Show
from show_orchestrator.notes import lock_path_for
from show_orchestrator.parser import Parser
from show_orchestrator.showfiles import lighting_files, stored_project

# Already compressed: stored as they are, which is as small and much faster.
STORED_SUFFIXES = {".mp3", ".m4a", ".aac", ".ogg", ".flac", ".mp4", ".mpeg", ".mpg", ".mov", ".webm", ".zip"}
# A MadMapper project's videos: in a bundle only when asked for, as they can be most of its size.
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".mpg", ".mpeg", ".mxf", ".hap"}

README = """{name}
{underline}

Extract the whole zip first, then open {name}.rpp from the extracted folder. Opened straight from inside
the zip, REAPER gets the project alone and reports every audio and MIDI file as missing.

{name}.rpp                 the REAPER project; its media is referenced at shows/... below
*.mid                      the cues it plays (and the pieces of split MIDI files)
shows/                     the show definition, its note lock and its media{lighting}{madmapper}
{videos}
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
    files += media_dependencies(root, show)
    return list(dict.fromkeys(files))


def media_dependencies(root: Path, show: Show) -> list[PurePosixPath]:
    """The media `show` plays (each song's file and its extra tracks), relative to root."""
    files = []
    for track in show.audio_tracks:
        if track.file_path:
            files.append(_inside(root, Path(track.file_path), "Media file"))
        for extra in track.extra_tracks or []:
            files.append(_inside(root, Path(extra.file_path), "Media file"))
    return list(dict.fromkeys(files))


def _build_reaper(root: Path, show_file: Path, show: Show, build: Path, velocity: int, gap: float | None,
                  routing: str | None, note_lock: bool) -> Path | None:
    """The show's MIDI files and its REAPER project (referencing the media where the show has it) into `build`;
    returns the note lock used, if any."""
    gap, routing_map = reaper_settings(show, gap, routing)
    lock_path = lock_path_for(root / show_file) if note_lock else None
    # generated first: it may add notes to the lock, which the project then uses
    midi_files = MidiGenerator(velocity=velocity).generate_midi_files(show, build, lock_path)
    backend = ReaperBackend()
    backend.create_project(show, midi_files, build, song_gap=gap, routing=routing_map, media_in_place=True)
    backend.save_project(build / f"{show_file.stem}.rpp")
    return lock_path


def write_reaper_bundle(
    root: Path,
    show_file: Path,
    out: BinaryIO,
    velocity: int = DEFAULT_VELOCITY,
    gap: float | None = None,
    routing: str | None = None,
    note_lock: bool = True,
) -> None:
    """Only what REAPER needs to play the show: the project, its MIDI files and the media it plays, laid out as in
    a full bundle (<show>/<show>.rpp, <show>/*.mid, <show>/shows/.../media). No show files, lighting or MadMapper."""
    show_file = Path(show_file)
    name = show_file.stem
    show = Parser().load_show(root / show_file)
    media = media_dependencies(root, show)
    with tempfile.TemporaryDirectory() as tmp:
        build = Path(tmp)
        _build_reaper(root, show_file, show, build, velocity, gap, routing, note_lock)
        with zipfile.ZipFile(out, "w") as bundle:
            def add(path: Path, arcname: str) -> None:
                stored = path.suffix.lower() in STORED_SUFFIXES
                bundle.write(path, f"{name}/{arcname}", zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED)

            for path in sorted(build.rglob("*")):
                if path.is_file():
                    add(path, path.relative_to(build).as_posix())
            for relative in media:
                add(root / relative, str(relative))


def write_bundle(
    root: Path,
    show_file: Path,
    out: BinaryIO,
    velocity: int = DEFAULT_VELOCITY,
    gap: float | None = None,
    routing: str | None = None,
    note_lock: bool = True,
    include_videos: bool = False,
) -> list[str]:
    """Build `show_file` (relative to root) and write the zip to `out`. `gap` and `routing` as for a build: None
    takes the show's own settings. Media paths are read relative to root, so root must be the working directory.
    The MadMapper project goes in without the videos it holds unless `include_videos`.
    Returns warnings: things the bundle has but not as they should be (a MadMapper project that would not patch)."""
    show_file = Path(show_file)
    name = show_file.stem
    show = Parser().load_show(root / show_file)
    files = show_dependencies(root, show_file, show)
    with tempfile.TemporaryDirectory() as tmp:
        build = Path(tmp)
        lock_path = _build_reaper(root, show_file, show, build, velocity, gap, routing, note_lock)
        if lock_path is not None and lock_path.is_file():  # it may be new: notes assigned for this build
            lock = _inside(root, lock_path, "Note lock")
            if lock not in files:
                files.append(lock)
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

            def add_project_folder(path: Path) -> list[str]:
                """A zipped .madproject's files under madmapper/ (a folder, ready to open, not a zip in the zip);
                returns the videos left out."""
                left_out = []
                with zipfile.ZipFile(path) as source:
                    for member in source.infolist():
                        member_path = PurePosixPath(member.filename.replace("\\", "/"))
                        if (member.is_dir() or member.filename.startswith(SKIPPED) or member_path.name == ".DS_Store"
                                or member_path.is_absolute() or ".." in member_path.parts):
                            continue
                        if not include_videos and member_path.suffix.lower() in VIDEO_SUFFIXES:
                            left_out.append(member_path.name)
                            continue
                        info = zipfile.ZipInfo(f"{name}/madmapper/{member_path}", date_time=member.date_time)
                        info.external_attr = 0o644 << 16
                        info.compress_type = (zipfile.ZIP_STORED if member_path.suffix.lower() in STORED_SUFFIXES
                                              else zipfile.ZIP_DEFLATED)
                        with source.open(member) as data, bundle.open(info, "w", force_zip64=True) as copy:
                            shutil.copyfileobj(data, copy, 1024 * 1024)
                return left_out

            # the rest first, so the README can say which videos were left out
            for path in sorted(build.rglob("*")):
                relative = path.relative_to(build)
                if path.is_file() and relative.parts[0] != ".madmapper":
                    add(path, relative.as_posix())
            for relative in [*files, *lighting]:
                add(root / relative, str(relative))
            left_out = []
            for path, arcname in madmapper:
                if path.suffix.lower() == ".zip":
                    left_out += add_project_folder(path)
                else:
                    add(path, f"madmapper/{arcname}")

            videos = ""
            if left_out:
                videos = (f"\nThe MadMapper project is here without its {len(left_out)} videos (Include videos was off):\n"
                          + "".join(f"  {video}\n" for video in sorted(set(left_out)))
                          + "Copy them into its Media folder, or make the bundle again with Include videos.\n")
            readme = README.format(
                name=name, underline="=" * len(name), show_file=PurePosixPath(show_file.as_posix()), videos=videos,
                lighting="\n  .../lighting/            the QLC+ workspace and fixtures (from ReQLC)" if lighting else "",
                madmapper="\nmadmapper/                 the MadMapper project, patched to the show's notes" if madmapper else "")
            # CRLF: Notepad on an older Windows shows LF-only text as one line
            bundle.writestr(f"{name}/READ ME FIRST.txt", readme.replace("\n", "\r\n"))
    return warnings
