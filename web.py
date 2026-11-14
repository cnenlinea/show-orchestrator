import argparse
import hashlib
import hmac
import math
import os
import re
import tempfile
from datetime import timedelta
from pathlib import Path

import mido
import yaml
from flask import Flask, jsonify, redirect, render_template, request, send_from_directory, session, url_for

from main import AVAILABLE_BACKENDS
from show_orchestrator.backends.reaper import DEFAULT_SONG_GAP, reaper_settings
from show_orchestrator.bundle import write_bundle
from show_orchestrator.madmapper import DEFAULT_DEVICE, note_name
from show_orchestrator.madupload import parse_fades, patch_upload
from show_orchestrator.editor import read_document, render_document
from show_orchestrator.generator import DEFAULT_VELOCITY, MidiGenerator
from show_orchestrator.models import EffectType, Show
from show_orchestrator.notes import assign_missing_notes, lock_path_for
from show_orchestrator.parser import Parser

SHOW_SUFFIXES = {".yaml", ".yml", ".csv"}
MEDIA_SUFFIXES = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aif", ".aiff", ".mid", ".midi"}
DEFAULT_OUTPUT_DIR = "build"
BUNDLE_DIR = "build/bundles"  # the latest bundle of each show, for downloading
MADMAPPER_DIR = "build/madmapper"  # the latest patched copy of each MadMapper project, for downloading
MAX_UPLOAD_BYTES = 2 * 1024**3

# What a broken show definition or a missing media file can raise.
SHOW_ERRORS = (ValueError, TypeError, KeyError, AttributeError, OSError, yaml.YAMLError)

# Login: with neither set the app is open, as when it runs on the show machine.
USERS_ENV = "SHOW_ORCHESTRATOR_USERS"        # "name:password,name:password"
PASSWORD_ENV = "SHOW_ORCHESTRATOR_PASSWORD"  # one password, for the user "show"
SECRET_ENV = "SHOW_ORCHESTRATOR_SECRET"      # signs the login cookie; default: derived from the users
PASSWORD_USER = "show"
SESSION_DAYS = 30


def users_from_env(environ=os.environ) -> dict[str, str]:
    """The users who may log in, {name: password}, from SHOW_ORCHESTRATOR_USERS and SHOW_ORCHESTRATOR_PASSWORD."""
    users = {}
    for entry in re.split(r"[,;\n]", environ.get(USERS_ENV) or ""):
        name, sep, password = entry.strip().partition(":")
        if sep and name and password:
            users[name] = password
    if environ.get(PASSWORD_ENV):
        users.setdefault(PASSWORD_USER, environ[PASSWORD_ENV])
    return users


def check_login(users: dict[str, str], name: str | None, password: str | None) -> bool:
    expected = users.get(name or "")
    return expected is not None and hmac.compare_digest((password or "").encode(), expected.encode())


def safe_next(target: str | None) -> str:
    """Where to go after logging in: a path on this site only."""
    return target if target and target.startswith("/") and not target.startswith("//") and "\\" not in target else "/"

def find_show_files(root: Path) -> list[str]:
    """Show definitions next to the app and anywhere under shows/, relative to root."""
    candidates = [*root.glob("*"), *(root / "shows").rglob("*")]
    return sorted(
        path.relative_to(root).as_posix()
        for path in candidates
        if path.is_file() and path.suffix in SHOW_SUFFIXES
    )


def find_media_files(root: Path) -> list[str]:
    """Audio and MIDI files under shows/, as the paths a show definition would use."""
    return sorted(
        path.relative_to(root).as_posix()
        for path in (root / "shows").rglob("*")
        if path.is_file() and path.suffix.lower() in MEDIA_SUFFIXES
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


def validate_source(text: str, suffix: str) -> None:
    """Raise if `text` would not load as a show definition of the given file type."""
    with tempfile.TemporaryDirectory() as temp_dir:
        candidate = Path(temp_dir) / f"show{suffix}"
        candidate.write_text(text, encoding="utf-8", newline="")
        Parser().load_show(candidate)


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


def upload_folder(show: str) -> Path:
    """The folder (relative to root) a show's uploaded files go into: its own folder, or shows/ for a show
    in the root."""
    folder = Path(show).parent
    return Path("shows") if folder == Path(".") else folder


def save_show_text(file: Path, text: str) -> None:
    """Replace a show file with `text`, unless the result would not load."""
    validate_source(text, file.suffix)
    # Keep the line endings the file already uses.
    newline = "\r\n" if b"\r\n" in file.read_bytes() else "\n"
    file.write_text(text, encoding="utf-8", newline=newline)


def create_app(root: Path, users: dict[str, str] | None = None, secret: str | None = None) -> Flask:
    """The web UI over the shows under `root`. `users` ({name: password}; default: the environment's) turns on the
    login; with none the app is open."""
    app = Flask(__name__)
    users = users_from_env() if users is None else users
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

    @app.before_request
    def require_login():
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
                return redirect(target)
            failed = True
        return render_template("login.html", next=target, failed=failed, user=request.form.get("user", "")), (401 if failed else 200)

    @app.route("/logout", methods=["GET", "POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def index():
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

    @app.post("/api/upload")
    def upload():
        """Add files to a show: media into its media/ folder, show files (e.g. a lights timeline to include)
        into its own folder. A file of the same name is replaced only with replace=1."""
        show = request.form.get("show")
        if show_file(show) is None:
            return error("Unknown show file", 404)
        uploads = request.files.getlist("file")
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
        target = folder / f"{file.stem}.zip"
        partial = folder / f"{file.stem}.zip.part"
        try:
            with open(partial, "wb") as out:
                write_bundle(root, file.relative_to(root), out, velocity=velocity, gap=gap,
                             routing=data.get("routing"), note_lock=data.get("note_lock", True))
            partial.replace(target)
        except SHOW_ERRORS as exc:
            partial.unlink(missing_ok=True)
            return error(str(exc))
        return jsonify(download=url_for("download_bundle", name=target.name), size=target.stat().st_size)

    @app.get("/api/bundle/<name>")
    def download_bundle(name: str):
        return send_from_directory(root / BUNDLE_DIR, name, as_attachment=True)

    @app.post("/api/madmapper")
    def madmapper():
        """Patch an uploaded MadMapper project (a .mad, or a .madproject folder zipped) for a show, as
        madmapper_patch.py does: the report, and with dry_run=1 nothing to download."""
        file = show_file(request.form.get("show"))
        if file is None:
            return error("Unknown show file", 404)
        upload = request.files.get("project")
        if upload is None or not upload.filename:
            return error("No MadMapper project sent")
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
            received = Path(tmp) / "upload"
            upload.save(received)
            try:
                result, output = patch_upload(
                    received, upload_name(upload.filename), Parser().load_show(file), lock_path,
                    root / MADMAPPER_DIR, dry_run=dry_run,
                    effect_types=effect_types, device=form.get("device", "").strip() or DEFAULT_DEVICE,
                    fades_from_audio=fades_from_audio, silence_threshold=silence_threshold,
                    fades=parse_fades(form.get("fades", "")),
                )
            except SHOW_ERRORS as exc:  # MadFileError and CueMatchError are ValueErrors
                return error(str(exc))
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
        )

    @app.get("/api/madmapper/<name>")
    def download_madmapper(name: str):
        return send_from_directory(root / MADMAPPER_DIR, name, as_attachment=True)

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
