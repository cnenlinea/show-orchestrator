import argparse
import hashlib
import hmac
import json
import math
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import mido
import yaml
from flask import Flask, g, jsonify, redirect, render_template, request, send_file, send_from_directory, session, url_for

from main import AVAILABLE_BACKENDS
from show_orchestrator.backends.reaper import DEFAULT_SONG_GAP, reaper_settings
from show_orchestrator.bundle import write_bundle, write_reaper_bundle
from show_orchestrator.madmapper import DEFAULT_DEVICE, note_name
from show_orchestrator.madupload import check_project, parse_fades, patch_upload
from show_orchestrator.editor import read_document, render_document
from show_orchestrator.generator import DEFAULT_VELOCITY, MidiGenerator
from show_orchestrator.models import EffectType, Show
from show_orchestrator.notes import assign_missing_notes, lock_path_for
from show_orchestrator.parser import Parser
from show_orchestrator import showfiles

SHOW_SUFFIXES = {".yaml", ".yml", ".csv"}
# YAML files next to the app that are not shows
NOT_SHOWS = {"compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"}
MEDIA_SUFFIXES = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aif", ".aiff", ".mid", ".midi"}
DEFAULT_OUTPUT_DIR = "build"
BUNDLE_DIR = "build/bundles"  # the latest bundle of each show, for downloading
CHUNK_DIR = "build/chunks"  # files arriving in pieces (/api/chunks), until the upload they are for uses them
MAX_CHUNK_BYTES = 64 * 1024**2  # one piece; a proxy may refuse more (Cloudflare: 100 MB a request)
CHUNK_HOURS = 24  # pieces of an upload nobody finished are thrown away after this
MADMAPPER_DIR = "build/madmapper"  # the latest patched copy of each MadMapper project, for downloading
MAX_UPLOAD_BYTES = 2 * 1024**3
# What a program's token (SHOW_ORCHESTRATOR_TOKENS) may use: a show's lighting and its media, nothing else.
TOKEN_ENDPOINTS = {"lighting", "publish_lighting", "lighting_workspace", "show_media", "show_media_file"}

# What a broken show definition or a missing media file can raise.
SHOW_ERRORS = (ValueError, TypeError, KeyError, AttributeError, OSError, yaml.YAMLError)

# Login: with neither set the app is open, as when it runs on the show machine.
USERS_ENV = "SHOW_ORCHESTRATOR_USERS"        # "name:password,name:password"
PASSWORD_ENV = "SHOW_ORCHESTRATOR_PASSWORD"  # one password, for the user "show"
SECRET_ENV = "SHOW_ORCHESTRATOR_SECRET"      # signs the login cookie; default: derived from the users
TOKENS_ENV = "SHOW_ORCHESTRATOR_TOKENS"      # "name:token,name:token": other programs (ReQLC), lighting and media only
HOMES_ENV = "SHOW_ORCHESTRATOR_HOMES"        # "user:/page,...": where a user starts, e.g. "sonido:/reaper"
PASSWORD_USER = "show"
SESSION_DAYS = 30


def users_from_env(environ=os.environ) -> dict[str, str]:
    """The users who may log in, {name: password}, from SHOW_ORCHESTRATOR_USERS and SHOW_ORCHESTRATOR_PASSWORD."""
    users = _pairs(environ.get(USERS_ENV))
    if environ.get(PASSWORD_ENV):
        users.setdefault(PASSWORD_USER, environ[PASSWORD_ENV])
    return users


def tokens_from_env(environ=os.environ) -> dict[str, str]:
    """The programs that may publish lighting and read media, {name: token}, from SHOW_ORCHESTRATOR_TOKENS."""
    return _pairs(environ.get(TOKENS_ENV))


def homes_from_env(environ=os.environ) -> dict[str, str]:
    """Where users start instead of the editor, {name: path}, from SHOW_ORCHESTRATOR_HOMES (paths on this site)."""
    return {name: path for name, path in _pairs(environ.get(HOMES_ENV)).items() if safe_next(path) == path}


def _pairs(text: str | None) -> dict[str, str]:
    """"name:secret,name:secret" (or ; or new lines between them) -> {name: secret}."""
    pairs = {}
    for entry in re.split(r"[,;\n]", text or ""):
        name, sep, secret = entry.strip().partition(":")
        if sep and name and secret:
            pairs[name] = secret
    return pairs


def check_login(users: dict[str, str], name: str | None, password: str | None) -> bool:
    expected = users.get(name or "")
    return expected is not None and hmac.compare_digest((password or "").encode(), expected.encode())


def safe_next(target: str | None) -> str:
    """Where to go after logging in: a path on this site only."""
    return target if target and target.startswith("/") and not target.startswith("//") and "\\" not in target else "/"

def find_show_files(root: Path) -> list[str]:
    """Show definitions next to the app and anywhere under shows/, relative to root."""
    candidates = [
        *(path for path in root.glob("*") if path.name not in NOT_SHOWS),
        *(root / "shows").rglob("*"),
    ]
    return sorted(
        path.relative_to(root).as_posix()
        for path in candidates
        # hidden files: e.g. the copy a save is checked with
        if path.is_file() and path.suffix in SHOW_SUFFIXES and not path.name.startswith(".")
    )


def find_media_files(root: Path) -> list[str]:
    """Audio and MIDI files under shows/, as the paths a show definition would use; none in hidden folders (such as
    the lighting a publish replaced)."""
    return sorted(
        path.relative_to(root).as_posix()
        for path in (root / "shows").rglob("*")
        if path.is_file() and path.suffix.lower() in MEDIA_SUFFIXES
        and not any(part.startswith(".") for part in path.relative_to(root).parts)
    )


def describe_show(show: Show, lock_path: Path | None) -> dict:
    """Flatten a show into what the page draws: effects with notes, songs with cues."""
    effect_mapping = assign_missing_notes(show.effects, 0, lock_path)
    effects_by_id = {
        effect.id: effect
        for effects_of_type in effect_mapping.values()
        for effect in effects_of_type.values()
    }
    cue_counts = dict.fromkeys(effects_by_id, 0)

    songs = []
    position = 0.0
    for track in show.audio_tracks:
        cues = []
        for effect_type, events in track.events.items():
            for event in events:
                effect = effects_by_id.get(event.effect_id)
                if effect is not None:
                    cue_counts[effect.id] += 1
                cues.append({
                    "type": str(effect_type),
                    "time": event.timestamp_seconds,
                    "duration": event.duration_seconds,
                    "effect": effect.name if effect else event.effect_id,
                    "note": effect.note if effect else None,
                    "channel": effect.channel if effect else None,
                })
        cues.sort(key=lambda cue: cue["time"])
        songs.append({
            "name": track.name,
            "start": position,
            "duration": track.duration_seconds,
            "file": track.file_path,
            "extra_tracks": [
                {
                    "name": extra.name,
                    "time": extra.timestamp_seconds,
                    "duration": extra.duration_seconds,
                    "file": extra.file_path,
                }
                for extra in track.extra_tracks or []
            ],
            "cues": cues,
        })
        position += track.duration_seconds

    reaper = show.reaper
    return {
        "duration": position,
        # the show's own build settings (YAML reaper: section), for the build panel
        "gap": reaper.gap if reaper else None,
        "routing_profiles": list((reaper.routing if reaper else None) or {}),
        "default_routing": reaper.default_routing if reaper else None,
        # the show's madmapper: section, for the MadMapper tab's defaults
        "madmapper": show.madmapper.model_dump() if show.madmapper else {},
        "effect_types": [str(effect_type) for effect_type in show.effects],
        "songs": songs,
        "effects": [
            {
                "type": str(effect_type),
                "id": effect.id,
                "name": effect.name,
                "note": effect.note,
                "channel": effect.channel,
                "cues": cue_counts[effect.id],
            }
            for effect_type, effects_of_type in effect_mapping.items()
            for effect in effects_of_type.values()
        ],
    }


def validate_source(text: str, file: Path) -> None:
    """Raise if `text` would not load as the show definition `file`. It is tried next to `file`, so the files
    it includes are found where the show finds them."""
    file.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=file.parent,
                                     prefix=f".{file.stem}.", suffix=f".check{file.suffix}", delete=False) as out:
        out.write(text)
    try:
        Parser().load_show(Path(out.name))
    finally:
        Path(out.name).unlink(missing_ok=True)


def build_options(data) -> tuple[int, float | None]:
    """The velocity and gap a build or bundle asks for; ValueError with a message for the page when bad."""
    velocity = data.get("velocity")
    try:
        velocity = DEFAULT_VELOCITY if velocity in (None, "") else int(velocity)
    except (TypeError, ValueError):
        raise ValueError("Velocity must be a number") from None
    if not 1 <= velocity <= 127:
        raise ValueError("Velocity must be between 1 and 127")
    gap = data.get("gap")
    if gap in (None, ""):
        return velocity, None  # the show's reaper: gap:, else the default
    try:
        gap = float(gap)
    except (TypeError, ValueError):
        raise ValueError("The gap between songs must be a number of seconds") from None
    if not gap >= 0:  # also refuses NaN
        raise ValueError("The gap between songs can't be negative")
    return velocity, gap


def upload_name(filename: str | None) -> str:
    """An uploaded file's name made safe to save: no folders, nothing Windows or a Mac refuses in a name."""
    name = re.split(r"[\\/]", filename or "")[-1]
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip(" .")
    return name


def show_title(file: str | Path, show: Show | None = None) -> str:
    """The name people see: the show's `title:`, else its file name made readable (musical_2026 -> Musical 2026)."""
    if show is not None and show.title and show.title.strip():
        return show.title.strip()
    words = [word for word in re.split(r"[_\-\s]+", Path(file).stem) if word]
    return " ".join(word[:1].upper() + word[1:] for word in words) or Path(file).stem


def show_updated(root: Path, file: str, show: Show) -> str | None:
    """When the files a show's REAPER project is made from last changed here (the show file, its note lock, included
    files, the media it plays), as UTC ISO 8601; None when none can be found."""
    path = root / file
    candidates = [path, lock_path_for(path)]
    candidates += [Path(include) if Path(include).is_absolute() else path.parent / include for include in show.include or []]
    for track in show.audio_tracks:
        candidates += [root / p for p in [track.file_path, *(e.file_path for e in track.extra_tracks or [])] if p]
    times = [candidate.stat().st_mtime for candidate in candidates if candidate.is_file()]
    if not times:
        return None
    return datetime.fromtimestamp(max(times), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def upload_folder(show: str) -> Path:
    """The folder (relative to root) a show's uploaded files go into: its own folder, or shows/ for a show
    in the root."""
    folder = Path(show).parent
    return Path("shows") if folder == Path(".") else folder


class AssembledUpload:
    """A file sent in pieces through /api/chunks, standing in for an uploaded file (werkzeug's FileStorage): it has
    a `filename`, `save()` moves it into place and `read()` reads it."""

    def __init__(self, path: Path, info: Path, filename: str):
        self.path, self.info, self.filename = path, info, filename

    def save(self, target) -> None:
        shutil.move(str(self.path), str(target))
        self.info.unlink(missing_ok=True)

    def read(self) -> bytes:
        data = self.path.read_bytes()
        self.path.unlink(missing_ok=True)
        self.info.unlink(missing_ok=True)
        return data


def save_show_text(file: Path, text: str) -> None:
    """Replace a show file with `text`, unless the result would not load."""
    validate_source(text, file)
    # Keep the line endings the file already uses.
    newline = "\r\n" if b"\r\n" in file.read_bytes() else "\n"
    file.write_text(text, encoding="utf-8", newline=newline)


def create_app(root: Path, users: dict[str, str] | None = None, secret: str | None = None,
               tokens: dict[str, str] | None = None, homes: dict[str, str] | None = None) -> Flask:
    """The web UI over the shows under `root`. `users` ({name: password}; default: the environment's) turns on the
    login; with none the app is open. `tokens` ({name: token}; default: the environment's) let other programs, such
    as ReQLC, publish a show's lighting and read its media with `Authorization: Bearer <token>`, and nothing else.
    `homes` ({name: path}; default: the environment's) sends a user to that page instead of the editor."""
    app = Flask(__name__)
    users = users_from_env() if users is None else users
    tokens = tokens_from_env() if tokens is None else tokens
    homes = homes_from_env() if homes is None else homes
    secret = secret or os.environ.get(SECRET_ENV)
    # Derived from the users when not given, so a restart keeps everyone logged in and a changed password logs out.
    app.secret_key = secret or hashlib.sha256(
        b"show-orchestrator\0" + "\0".join(f"{n}:{p}" for n, p in sorted(users.items())).encode()
    ).hexdigest()
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=SESSION_DAYS),
    )

    def error(message: str, status: int = 400):
        return jsonify(error=message), status

    def show_file(name: str | None) -> Path | None:
        # Only files the listing offers, so a request can't read arbitrary paths.
        return root / name if name in find_show_files(root) else None

    def current_user() -> str | None:
        name = session.get("user")
        if name in users:  # a user removed since the cookie was set is logged out
            return name
        auth = request.authorization  # scripts send HTTP basic instead of logging in
        if auth and auth.type == "basic" and check_login(users, auth.username, auth.password):
            return auth.username
        return None

    def token_client() -> str | None:
        """The program whose token the request carries, if any."""
        auth = request.headers.get("Authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        return next((name for name, expected in tokens.items()
                     if hmac.compare_digest(token.strip().encode(), expected.encode())), None)

    def who() -> str | None:
        """Who is making the request, to note against what it changes."""
        return g.get("client") or current_user()

    def show_by_name(name: str) -> Path | None:
        """A show by its file (as listed) or, for programs, by its name alone (musical_2026) when that is unique."""
        listed = find_show_files(root)
        if name in listed:
            return root / name
        named = [file for file in listed if Path(file).stem == name]
        return root / named[0] if len(named) == 1 else None

    @app.before_request
    def require_login():
        if request.endpoint in TOKEN_ENDPOINTS:
            client = token_client()
            if client:
                g.client = client
                return None
            if request.headers.get("Authorization", "").lower().startswith("bearer "):
                return error("Unknown token", 401)
        if not users or request.endpoint in ("login", "static") or current_user():
            return None
        if request.path.startswith("/api/"):
            # No WWW-Authenticate: the browser would answer the page's fetch calls with its own password box.
            return error("Log in first", 401)
        return redirect(url_for("login", next=request.full_path.rstrip("?")))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        target = safe_next(request.values.get("next"))
        if not users:
            return redirect(target)
        failed = False
        if request.method == "POST":
            name = request.form.get("user", "").strip()
            if check_login(users, name, request.form.get("password")):
                session.clear()
                session["user"] = name
                session.permanent = True
                return redirect(homes.get(name, target) if target == "/" else target)
            failed = True
        return render_template("login.html", next=target, failed=failed, user=request.form.get("user", "")), (401 if failed else 200)

    @app.route("/logout", methods=["GET", "POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def index():
        home = homes.get(current_user() or "")
        if home and home != "/" and "editor" not in request.args:  # the editor stays at /?editor
            return redirect(home)
        return render_template(
            "index.html",
            effect_types=[str(effect_type) for effect_type in EffectType],
            backends=list(AVAILABLE_BACKENDS),
            default_velocity=DEFAULT_VELOCITY,
            default_gap=DEFAULT_SONG_GAP,
            default_output_dir=DEFAULT_OUTPUT_DIR,
            user=current_user() if users else None,
            upload_accept=",".join(sorted(MEDIA_SUFFIXES | SHOW_SUFFIXES)),
        )

    @app.get("/api/shows")
    def shows():
        return jsonify(shows=find_show_files(root))

    @app.get("/api/show")
    def show():
        file = show_file(request.args.get("file"))
        if file is None:
            return error("Unknown show file", 404)
        try:
            show_data = Parser().load_show(file)
            return jsonify(describe_show(show_data, lock_path_for(file)))
        except SHOW_ERRORS as exc:
            return error(str(exc))

    @app.get("/api/source")
    def source():
        file = show_file(request.args.get("file"))
        if file is None:
            return error("Unknown show file", 404)
        return jsonify(text=file.read_text(encoding="utf-8"))

    @app.put("/api/source")
    def save_source():
        data = request.get_json()
        file = show_file(data.get("file"))
        if file is None:
            return error("Unknown show file", 404)
        text = str(data.get("text", "")).replace("\r\n", "\n")
        try:
            save_show_text(file, text)
        except SHOW_ERRORS as exc:
            return error(f"Not saved, the show would not load: {exc}")
        return jsonify(ok=True)

    @app.get("/api/document")
    def document():
        file = show_file(request.args.get("file"))
        if file is None:
            return error("Unknown show file", 404)
        try:
            return jsonify(read_document(file.read_text(encoding="utf-8"), file.suffix))
        except SHOW_ERRORS as exc:
            return error(str(exc))

    @app.put("/api/document")
    def save_document():
        data = request.get_json()
        file = show_file(data.get("file"))
        if file is None:
            return error("Unknown show file", 404)
        try:
            save_show_text(file, render_document(data.get("document") or {}, file.suffix))
        except SHOW_ERRORS as exc:
            return error(f"Not saved, the show would not load: {exc}")
        return jsonify(ok=True)

    @app.get("/api/media")
    def media():
        return jsonify(files=find_media_files(root))

    # --- files too large for one request go in pieces: POST /api/chunks, PUT each piece, then name the chunk in the
    # upload it is for (<field>_chunk=<id> in place of the file)

    def chunk_files(chunk_id: str) -> tuple[Path, Path] | None:
        if not re.fullmatch(r"[0-9a-f]{32}", chunk_id or ""):
            return None
        folder = root / CHUNK_DIR
        return folder / f"{chunk_id}.part", folder / f"{chunk_id}.json"

    def chunk_info(chunk_id: str) -> tuple[Path, Path, dict] | None:
        """A chunked upload of whoever asks: (data, info file, info), or None."""
        files = chunk_files(chunk_id)
        if files is None or not files[0].is_file():
            return None
        try:
            info = json.loads(files[1].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return (*files, info) if info.get("by") == who() else None

    @app.post("/api/chunks")
    def start_chunks():
        """Start an upload in pieces: {name, size} -> {id, piece} (the largest piece to PUT)."""
        data = request.get_json(silent=True) or {}
        name = upload_name(data.get("name"))
        size = data.get("size")
        if not name or not isinstance(size, int) or not 0 < size <= MAX_UPLOAD_BYTES:
            return error(f"Send the file's name and size (at most {MAX_UPLOAD_BYTES // 1024**3} GB)")
        folder = root / CHUNK_DIR
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.iterdir():  # what nobody finished
            if old.stat().st_mtime < time.time() - CHUNK_HOURS * 3600:
                old.unlink(missing_ok=True)
        chunk_id = uuid.uuid4().hex
        part, info = chunk_files(chunk_id)
        part.write_bytes(b"")
        info.write_text(json.dumps({"name": name, "size": size, "by": who()}), encoding="utf-8")
        return jsonify(id=chunk_id, piece=MAX_CHUNK_BYTES)

    @app.put("/api/chunks/<chunk_id>")
    def put_chunk(chunk_id: str):
        """One piece of a chunked upload, at ?offset= (the bytes received so far; a piece sent again is fine)."""
        found = chunk_info(chunk_id)
        if found is None:
            return error("No such upload", 404)
        part, _, info = found
        if (request.content_length or 0) > MAX_CHUNK_BYTES:
            return error(f"A piece can be at most {MAX_CHUNK_BYTES // 1024**2} MB", 413)
        try:
            offset = int(request.args.get("offset", ""))
        except ValueError:
            return error("offset= must be a number")
        have = part.stat().st_size
        piece = request.get_data(cache=False)
        if offset < 0 or offset > have:
            return jsonify(error=f"Expected the piece at {have}", received=have), 409
        if offset + len(piece) > info["size"]:
            return error("The pieces add up to more than the file's size")
        with open(part, "r+b") as out:
            out.seek(offset)
            out.write(piece)
        return jsonify(received=max(have, offset + len(piece)), size=info["size"])

    def received(field: str) -> list:
        """The files sent in a form field: uploaded with the form, or (<field>_chunk=<id>) in pieces before it.
        ValueError when a chunked one is unknown or not complete."""
        files = [upload for upload in request.files.getlist(field) if upload.filename]
        for chunk_id in request.form.getlist(f"{field}_chunk"):
            found = chunk_info(chunk_id)
            if found is None:
                raise ValueError("An upload sent in pieces is gone: send it again")
            part, info_file, info = found
            if part.stat().st_size != info["size"]:
                raise ValueError(f"{info['name']} has not arrived completely: send it again")
            files.append(AssembledUpload(part, info_file, info["name"]))
        return files

    @app.post("/api/upload")
    def upload():
        """Add files to a show: media into its media/ folder, show files (e.g. a lights timeline to include)
        into its own folder. A file of the same name is replaced only with replace=1."""
        show = request.form.get("show")
        if show_file(show) is None:
            return error("Unknown show file", 404)
        try:
            uploads = received("file")
        except ValueError as exc:
            return error(str(exc))
        if not uploads:
            return error("No files sent")
        targets = []
        for upload in uploads:
            name = upload_name(upload.filename)
            suffix = Path(name).suffix.lower()
            if suffix in MEDIA_SUFFIXES:
                folder = upload_folder(show) / "media"
            elif suffix in SHOW_SUFFIXES:
                folder = upload_folder(show)
            else:
                allowed = ", ".join(sorted(MEDIA_SUFFIXES | SHOW_SUFFIXES))
                return error(f"{upload.filename}: only these kinds of files can be added: {allowed}")
            if not Path(name).stem:
                return error(f"{upload.filename!r} is not a usable file name")
            targets.append((upload, folder / name))
        existing = [target.as_posix() for _, target in targets if (root / target).exists()]
        if existing and request.form.get("replace") != "1":
            return jsonify(error="These files already exist: " + ", ".join(existing), exists=existing), 409
        for upload, target in targets:
            path = root / target
            path.parent.mkdir(parents=True, exist_ok=True)
            partial = path.with_name(path.name + ".part")
            upload.save(partial)
            partial.replace(path)  # a cut-off upload never leaves half a file under the real name
        return jsonify(saved=[target.as_posix() for _, target in targets])

    @app.post("/api/bundle")
    def bundle():
        """Build a show into a zip (project, MIDI, show files, media) to download from /api/bundle/<name>."""
        data = request.get_json()
        file = show_file(data.get("file"))
        if file is None:
            return error("Unknown show file", 404)
        try:
            velocity, gap = build_options(data)
        except ValueError as exc:
            return error(str(exc))
        folder = root / BUNDLE_DIR
        folder.mkdir(parents=True, exist_ok=True)
        include_videos = bool(data.get("include_videos"))
        stem = f"{file.stem}_with_videos" if include_videos else file.stem
        target = folder / f"{stem}.zip"
        partial = folder / f"{stem}.zip.{uuid.uuid4().hex[:8]}.part"  # two bundles made at once don't mix
        try:
            with open(partial, "wb") as out:
                warnings = write_bundle(root, file.relative_to(root), out, velocity=velocity, gap=gap,
                                        routing=data.get("routing"), note_lock=data.get("note_lock", True),
                                        include_videos=include_videos)
            partial.replace(target)
        except SHOW_ERRORS as exc:
            partial.unlink(missing_ok=True)
            return error(str(exc))
        return jsonify(download=url_for("download_bundle", name=target.name), size=target.stat().st_size,
                       warnings=warnings)

    @app.get("/api/bundle/<name>")
    def download_bundle(name: str):
        return send_from_directory(root / BUNDLE_DIR, name, as_attachment=True)

    # --- the downloads page: each show and its REAPER project, for whoever runs the show

    @app.get("/reaper")
    def reaper_page():
        shows = []
        for name in find_show_files(root):
            try:
                show = Parser().load_show(root / name)
            except SHOW_ERRORS as exc:
                shows.append({"file": name, "title": show_title(name), "error": str(exc)})
                continue
            seconds = sum(track.duration_seconds for track in show.audio_tracks)
            shows.append({"file": name, "title": show_title(name, show), "songs": len(show.audio_tracks),
                          "minutes": round(seconds / 60), "updated": show_updated(root, name, show)})
        # the same name twice (musical.yaml and musical.csv): tell them apart by type, then by folder
        for tell in (lambda f: Path(f).suffix[1:].upper(), lambda f: Path(f).parent.name or "root"):
            counts: dict[str, int] = {}
            for row in shows:
                counts[row["title"]] = counts.get(row["title"], 0) + 1
            for row in shows:
                if counts[row["title"]] > 1:
                    row["title"] = f"{row['title']} ({tell(row['file'])})"
        shows.sort(key=lambda row: row["title"].lower())
        return render_template("reaper.html", shows=shows, user=current_user() if users else None)

    @app.post("/api/reaper")
    def reaper_bundle():
        """Only what REAPER needs to play a show (project, MIDI, the media it plays) as a zip, built with the
        show's own settings (its gap and default routing), to download from /api/bundle/<name>."""
        file = show_file((request.get_json(silent=True) or {}).get("file"))
        if file is None:
            return error("Unknown show file", 404)
        folder = root / BUNDLE_DIR
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{file.stem}_reaper.zip"
        partial = folder / f"{target.name}.{uuid.uuid4().hex[:8]}.part"
        try:
            with open(partial, "wb") as out:
                write_reaper_bundle(root, file.relative_to(root), out)
            partial.replace(target)
        except SHOW_ERRORS as exc:
            partial.unlink(missing_ok=True)
            return error(str(exc))
        return jsonify(download=url_for("download_bundle", name=target.name), size=target.stat().st_size)

    def project_info(file: Path) -> dict:
        relative = file.relative_to(root)
        return {"project": showfiles.public_project(showfiles.stored_project(root, relative)),
                "previous": [showfiles.public_project(p) for p in showfiles.previous_projects(root, relative)]}

    @app.get("/api/madmapper/project")
    def madmapper_project():
        """The show's MadMapper project (name, size, when and by whom uploaded) and the ones it replaced."""
        file = show_file(request.args.get("show"))
        if file is None:
            return error("Unknown show file", 404)
        return jsonify(project_info(file))

    @app.post("/api/madmapper/project")
    def upload_madmapper_project():
        """Make an uploaded MadMapper project (a .mad, or a .madproject folder zipped) the show's; the one it
        replaces is kept with the earlier ones."""
        file = show_file(request.form.get("show"))
        if file is None:
            return error("Unknown show file", 404)
        try:
            upload = next(iter(received("project")), None)
        except ValueError as exc:
            return error(str(exc))
        if upload is None:
            return error("No MadMapper project sent")
        name = upload_name(upload.filename)
        with tempfile.TemporaryDirectory() as tmp:
            saved = Path(tmp) / "upload"
            upload.save(saved)
            try:
                check_project(saved, name)
            except SHOW_ERRORS as exc:
                return error(f"Not kept: {exc}")
            showfiles.store_project(root, file.relative_to(root), saved, name, who())
        return jsonify(project_info(file))

    @app.get("/api/madmapper/project/download")
    def download_madmapper_project():
        """The show's MadMapper project as it was uploaded, or with previous=<id> one it replaced."""
        file = show_file(request.args.get("show"))
        if file is None:
            return error("Unknown show file", 404)
        relative = file.relative_to(root)
        previous = request.args.get("previous")
        project = (showfiles.previous_project(root, relative, previous) if previous
                   else showfiles.stored_project(root, relative))
        if project is None:
            return error("No such MadMapper project", 404)
        return send_file(project["path"], as_attachment=True, download_name=project["name"])

    @app.post("/api/madmapper")
    def madmapper():
        """Patch the show's MadMapper project, as madmapper_patch.py does: the report, and with dry_run=1 nothing to
        download. A project sent along (a .mad, or a .madproject folder zipped) is patched instead and, once it
        patches, becomes the show's project (not with dry_run, which only previews it)."""
        file = show_file(request.form.get("show"))
        if file is None:
            return error("Unknown show file", 404)
        try:
            upload = next(iter(received("project")), None)
        except ValueError as exc:
            return error(str(exc))
        stored = showfiles.stored_project(root, file.relative_to(root))
        if upload is None and stored is None:
            return error("This show has no MadMapper project yet: upload one first")
        form = request.form
        fades_from_audio = {"on": True, "off": False}.get(form.get("fades_from_audio", ""))
        try:
            threshold = form.get("silence_threshold", "").strip()
            silence_threshold = float(threshold) if threshold else None
            if silence_threshold is not None and not math.isfinite(silence_threshold):
                raise ValueError
        except ValueError:
            return error("The silence threshold must be a number of dBFS")
        effect_types = form.getlist("types") or None
        if effect_types and any(t not in {str(e) for e in EffectType} for t in effect_types):
            return error(f"Unknown effect type in {effect_types}")
        dry_run = form.get("dry_run") == "1"
        lock_path = lock_path_for(file) if form.get("note_lock", "1") != "0" else None
        with tempfile.TemporaryDirectory() as tmp:
            if upload is not None:
                project, name = Path(tmp) / "upload", upload_name(upload.filename)
                upload.save(project)
            else:
                project, name = stored["path"], stored["name"]
            try:
                result, output = patch_upload(
                    project, name, Parser().load_show(file), lock_path,
                    root / MADMAPPER_DIR, dry_run=dry_run,
                    effect_types=effect_types, device=form.get("device", "").strip() or DEFAULT_DEVICE,
                    fades_from_audio=fades_from_audio, silence_threshold=silence_threshold,
                    fades=parse_fades(form.get("fades", "")),
                )
            except SHOW_ERRORS as exc:  # MadFileError and CueMatchError are ValueErrors
                return error(str(exc))
            if upload is not None and not dry_run:
                showfiles.store_project(root, file.relative_to(root), project, name, who())
        report = result.report

        def cue(cue) -> dict:
            return {"cue": cue.name, "bank": cue.bank}

        return jsonify(
            patched=[{**cue(c), "effect": effect_id, "note": note, "note_name": note_name(note), "channel": channel}
                     for effect_id, c, note, channel in report.patched],
            unmatched_effects=[{"effect": e.id, "note": e.note, "channel": e.channel}
                               for e in report.unmatched_effects],
            unmatched_cues=[cue(c) for c in report.unmatched_cues],
            fades_from_audio=result.fades_from_audio,
            silence_threshold=result.silence_threshold,
            audio_fades=[{**cue(f.cue), "old": f.old_fade, "fade": f.fade, "reason": f.reason}
                         for f in result.audio_fades],
            hand_fades=[{**cue(f.cue), "old": f.old_fade, "fade": f.fade} for f in result.hand_fades],
            file=output.name if output else None,
            download=url_for("download_madmapper", name=output.name) if output else None,
            project=name,
        )

    @app.get("/api/madmapper/<name>")
    def download_madmapper(name: str):
        return send_from_directory(root / MADMAPPER_DIR, name, as_attachment=True)

    # --- lighting and media, for ReQLC (a token, or a logged-in user)

    def load_named(name: str):
        """(show file, show) for a show named in the URL, or an error response."""
        file = show_by_name(name)
        if file is None:
            return None, error("Unknown show", 404)
        try:
            return file, Parser().load_show(file)
        except SHOW_ERRORS as exc:
            return None, error(f"The show does not load: {exc}")

    @app.get("/api/shows/<path:name>/lighting")
    def lighting(name: str):
        """The show's lighting: its QLC+ workspace and fixtures, the MIDI files it plays (where, in which songs,
        present or not, with hashes), the ones it doesn't use, and the manifest of the last publish."""
        file, show = load_named(name)
        if file is None:
            return show
        state = showfiles.lighting_state(root, file.relative_to(root), show)
        return jsonify(show=file.relative_to(root).as_posix(), **state)

    @app.put("/api/shows/<path:name>/lighting")
    def publish_lighting(name: str):
        """Publish the show's lighting as a complete set (multipart): `workspace` (one .qxw), `fixture` (.qxf, any
        number), `midi` (.mid, any number) and `manifest` (JSON: where it comes from, e.g. {"source": "reqlc",
        "show": ..., "version": ..., "by": ...}). MIDI files named like ones the show plays replace them; the
        reply lists what changed, what the show plays that wasn't sent, and what was sent that it doesn't play.
        dry_run=1 only reports."""
        file, show = load_named(name)
        if file is None:
            return show
        workspace = request.files.get("workspace")
        if workspace is None or not workspace.filename:
            return error("No workspace sent: the lighting is published as a whole, workspace and MIDI files together")
        try:
            manifest = json.loads(request.form.get("manifest") or "{}")
        except ValueError:
            return error("The manifest is not JSON")
        if not isinstance(manifest, dict):
            return error("The manifest must be a JSON object")
        read = lambda upload: (upload_name(upload.filename), upload.read())  # noqa: E731
        try:
            report = showfiles.publish_lighting(
                root, file.relative_to(root), show,
                read(workspace),
                [read(u) for u in request.files.getlist("fixture") if u.filename],
                [read(u) for u in request.files.getlist("midi") if u.filename],
                manifest, who(), dry_run=request.form.get("dry_run") == "1")
        except (ValueError, OSError) as exc:
            return error(str(exc))
        return jsonify(show=file.relative_to(root).as_posix(), **report)

    @app.get("/api/shows/<path:name>/lighting/workspace")
    def lighting_workspace(name: str):
        """The show's QLC+ workspace, to download."""
        file = show_by_name(name)
        if file is None:
            return error("Unknown show", 404)
        folder = showfiles.lighting_dir(root, file.relative_to(root))
        workspaces = sorted(folder.glob("*.qxw")) if folder.is_dir() else []
        if not workspaces:
            return error("This show has no lighting workspace yet", 404)
        return send_file(workspaces[0], as_attachment=True, download_name=workspaces[0].name)

    def show_media_files(show) -> list[dict]:
        rows = []
        for track in show.audio_tracks:
            for path, kind, track_name in [(track.file_path, "song", track.name),
                                           *((e.file_path, "extra", e.name) for e in track.extra_tracks or [])]:
                if path:
                    full = root / path
                    rows.append({"song": track.name, "track": track_name, "kind": kind, "path": path,
                                 "present": full.is_file(), "size": full.stat().st_size if full.is_file() else None})
        return rows

    @app.get("/api/shows/<path:name>/media")
    def show_media(name: str):
        """The media the show plays, song by song: each song's file and its extra tracks."""
        file, show = load_named(name)
        if file is None:
            return show
        return jsonify(show=file.relative_to(root).as_posix(), files=show_media_files(show))

    @app.get("/api/shows/<path:name>/media/file")
    def show_media_file(name: str):
        """One of the show's media files (path=, as /media lists it), to download; only the show's own."""
        file, show = load_named(name)
        if file is None:
            return show
        path = request.args.get("path")
        if path not in {row["path"] for row in show_media_files(show)}:
            return error("Not one of this show's media files", 404)
        full = (root / path).resolve()
        if not full.is_file() or not full.is_relative_to(root.resolve()):
            return error("Not found", 404)
        return send_file(full, as_attachment=True, download_name=full.name, conditional=True)

    @app.get("/api/ports")
    def ports():
        try:
            return jsonify(ports=mido.get_output_names())
        except (ImportError, OSError, RuntimeError, SystemError):  # no MIDI system at all, e.g. in a container on a server
            return jsonify(ports=[])

    @app.post("/api/play")
    def play():
        data = request.get_json()
        port_name = data.get("port")
        try:
            available = mido.get_output_names()
        except (ImportError, OSError, RuntimeError, SystemError):
            available = []
        if port_name not in available:
            return error("MIDI port not available")
        try:
            note, channel = int(data["note"]), int(data["channel"])
            msg_on = mido.Message("note_on", note=note, velocity=DEFAULT_VELOCITY, channel=channel)
            msg_off = mido.Message("note_off", note=note, velocity=0, channel=channel)
        except (KeyError, TypeError, ValueError) as exc:
            return error(f"Invalid note: {exc}")
        with mido.open_output(port_name) as port:
            port.send(msg_on)
            port.send(msg_off)
        return jsonify(ok=True)

    @app.post("/api/build")
    def build():
        data = request.get_json()
        file = show_file(data.get("file"))
        if file is None:
            return error("Unknown show file", 404)
        backend_name = data.get("orchestrate") or None
        if backend_name is not None and backend_name not in AVAILABLE_BACKENDS:
            return error(f"Unknown backend: {backend_name}")
        try:
            velocity, gap = build_options(data)
        except ValueError as exc:
            return error(str(exc))

        output_dir = root / (data.get("output_dir") or DEFAULT_OUTPUT_DIR)
        lock_path = lock_path_for(file) if data.get("note_lock", True) else None
        project_path = None
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            show_data = Parser().load_show(file)
            # no "routing" key: the show's default_routing; "" (NO_ROUTING): none
            gap, routing = reaper_settings(show_data, gap, data.get("routing"))
            midi_files = MidiGenerator(velocity=velocity).generate_midi_files(
                show_data, output_dir, lock_path
            )
            if backend_name is not None:
                backend = AVAILABLE_BACKENDS[backend_name]()
                backend.create_project(show_data, midi_files, output_dir, song_gap=gap, routing=routing)
                project_path = output_dir / f"{file.stem}.rpp"
                backend.save_project(project_path)
        except SHOW_ERRORS as exc:
            return error(str(exc))

        return jsonify(
            output_dir=str(output_dir),
            project=str(project_path) if project_path else None,
            midi_files=[
                generated["file_path"].name
                for song_files in midi_files.values()
                for generated in song_files.values()
            ],
        )

    return app


def main():
    arg_parser = argparse.ArgumentParser(
        description="Web UI to inspect a show, audition its notes and build its files."
    )
    arg_parser.add_argument("--host", default="127.0.0.1", help="Address to listen on (default: 127.0.0.1)")
    arg_parser.add_argument("--port", type=int, default=5000, help="Port to listen on (default: 5000)")
    args = arg_parser.parse_args()

    if not users_from_env() and args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"Warning: listening on {args.host} with no login; set {USERS_ENV} or {PASSWORD_ENV} to require one.")
    # Media paths in show definitions are relative to the working directory, as in main.py.
    create_app(Path.cwd()).run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
