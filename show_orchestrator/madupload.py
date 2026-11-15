"""Patch a MadMapper project that arrives as an upload rather than a path: what the web UI does with
madmapper_patch.py's `patch_show`.

Two kinds of upload:
- a `.mad` file: a MadMapper 5 project, or the .mad from inside a MadMapper 6 `.madproject` folder. The patched
  copy comes back as `<name>_midi.mad` (MadMapper 5) or under its own name (MadMapper 6), to put into a copy of
  the folder in place of the original, since the folder's .mad is named after the folder.
- a `.zip` of a `.madproject` folder (as the Finder's Compress makes it). The patched copy comes back as a zip of
  `<name>_midi.madproject`, made as madmapper_patch.py makes it.
"""
from __future__ import annotations

import re
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from show_orchestrator.madbanks import TriggerBanks
from show_orchestrator.madfile import MadFileError, MadProject, get, mad_file_in, save_project, to_plain
from show_orchestrator.madmapper import ShowPatch, patch_show
from show_orchestrator.models import Show

MAX_UNZIPPED_BYTES = 8 * 1024**3
SKIPPED = ("__MACOSX/",)  # the Finder's resource forks
COMPRESSED_MEDIA = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".mpg", ".mpeg", ".mp3", ".m4a", ".aac", ".jpg",
                    ".jpeg", ".png", ".gif", ".zip"}


def parse_fades(text: str) -> list[tuple[str, float]]:
    """`CUE=SECONDS` lines (as madmapper_patch.py's --fade) -> [(cue, seconds)]."""
    fades = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        name, sep, seconds = line.rpartition("=")
        try:
            value = float(seconds)
        except ValueError:
            value = -1.0
        if not sep or not name.strip() or not value >= 0:
            raise ValueError(f"a fade is CUE=SECONDS (e.g. \"0. Triumph=0.2\"), not {line.strip()!r}")
        fades.append((name.strip(), value))
    return fades


def _extract(data: Path, into: Path) -> Path:
    """Unzip a zipped .madproject folder into `into`; returns the folder."""
    try:
        archive = zipfile.ZipFile(data)
    except zipfile.BadZipFile:
        raise MadFileError("the upload is not a zip file") from None
    with archive:
        members = [m for m in archive.infolist() if not m.filename.startswith(SKIPPED)
                   and PurePosixPath(m.filename).name != ".DS_Store"]
        if sum(m.file_size for m in members) > MAX_UNZIPPED_BYTES:
            raise MadFileError("the zip is too large once unpacked")
        for member in members:
            parts = PurePosixPath(member.filename.replace("\\", "/")).parts
            if not parts or member.filename.startswith(("/", "\\")) or ".." in parts or ":" in parts[0]:
                raise MadFileError(f"the zip holds a path outside its folder: {member.filename}")
            target = into.joinpath(*parts)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, open(target, "wb") as out:
                shutil.copyfileobj(source, out)
    folders = [p for p in into.iterdir() if p.is_dir() and p.suffix.lower() == ".madproject"]
    if len(folders) == 1:
        return folders[0]
    if not folders and any(into.glob("*.mad")):  # the folder's contents zipped rather than the folder
        return into
    raise MadFileError("the zip should hold one .madproject folder (zip the folder itself)")


def patched_stem(stem: str) -> str:
    """The name a patched copy gets: `<stem>_midi`, once (a project patched before keeps its name)."""
    return stem if stem.lower().endswith("_midi") else f"{stem}_midi"


def check_project(upload: Path, filename: str) -> None:
    """Raise MadFileError unless `upload` (named `filename`) is a project `patch_upload` can patch."""
    suffix = Path(filename).suffix.lower()
    if suffix == ".mad":
        MadProject.from_bytes(upload.read_bytes())
        return
    if suffix != ".zip":
        raise MadFileError("upload a .mad file, or a .madproject folder zipped")
    with tempfile.TemporaryDirectory() as tmp:
        MadProject.load(mad_file_in(_extract(upload, Path(tmp))))


def _beside(original: str | None, name: str) -> str:
    """The original projectPath with its last part replaced by `name`: where the patched copy would sit if it
    were saved next to the original, as madmapper_patch.py saves it."""
    if not original:
        return name
    return re.sub(r"[^\\/]*$", name, original.rstrip("\\/"), count=1)


def patch_upload(
    upload: Path,
    filename: str,
    show: Show,
    lock_path: Path | None,
    out_dir: Path,
    dry_run: bool = False,
    **options,
) -> tuple[ShowPatch, Path | None]:
    """Patch the project in the uploaded file `upload` (named `filename` by the browser) for `show`, with
    `patch_show`'s `options`, and write the result into `out_dir`; returns the report and the file written (None
    for a dry run). MadFileError or CueMatchError (a ValueError) when the project can't be patched."""
    suffix = Path(filename).suffix.lower()
    stem = Path(filename).stem
    if suffix == ".mad":
        project = MadProject.from_bytes(upload.read_bytes())
        result = patch_show(project, show, lock_path, **options)
        if dry_run:
            return result, None
        mm6 = TriggerBanks(project.root).is_mm6
        output = out_dir / (Path(filename).name if mm6 else f"{patched_stem(stem)}.mad")
        out_dir.mkdir(parents=True, exist_ok=True)
        partial = output.with_name(output.name + ".part")
        project.save(partial)
        partial.replace(output)
        return result, output
    if suffix != ".zip":
        raise MadFileError("upload a .mad file, or a .madproject folder zipped")

    with tempfile.TemporaryDirectory() as tmp:
        folder = _extract(upload, Path(tmp) / "in")
        name = folder.stem if folder.suffix.lower() == ".madproject" else Path(stem).stem
        project = MadProject.load(mad_file_in(folder))
        original_path = get(project.root, "projectPath")
        result = patch_show(project, show, lock_path, **options)
        if dry_run:
            return result, None
        patched = Path(tmp) / "out" / f"{patched_stem(name)}.madproject"
        patched.parent.mkdir()
        save_project(project, folder, patched,
                     project_path=_beside(to_plain(original_path) if original_path else None, patched.name))
        out_dir.mkdir(parents=True, exist_ok=True)
        output = out_dir / f"{patched.name}.zip"
        partial = output.with_name(output.name + ".part")
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(patched.rglob("*")):
                # media is compressed already: stored as it is, which is as small and much faster
                method = zipfile.ZIP_STORED if path.suffix.lower() in COMPRESSED_MEDIA else zipfile.ZIP_DEFLATED
                archive.write(path, path.relative_to(patched.parent).as_posix(), method)
        partial.replace(output)
        return result, output
