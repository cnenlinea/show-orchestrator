import argparse
import tempfile
from pathlib import Path

import mido
import yaml
from flask import Flask, jsonify, render_template, request

from main import AVAILABLE_BACKENDS
from show_orchestrator.editor import read_document, render_document
from show_orchestrator.generator import DEFAULT_VELOCITY, MidiGenerator
from show_orchestrator.models import EffectType, Show
from show_orchestrator.notes import assign_missing_notes, lock_path_for
from show_orchestrator.parser import Parser

SHOW_SUFFIXES = {".yaml", ".yml", ".csv"}
MEDIA_SUFFIXES = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aif", ".aiff", ".mid", ".midi"}
DEFAULT_OUTPUT_DIR = "build"

# What a broken show definition or a missing media file can raise.
SHOW_ERRORS = (ValueError, TypeError, KeyError, AttributeError, OSError, yaml.YAMLError)


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

    return {
        "duration": position,
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


def save_show_text(file: Path, text: str) -> None:
    """Replace a show file with `text`, unless the result would not load."""
    validate_source(text, file.suffix)
    # Keep the line endings the file already uses.
    newline = "\r\n" if b"\r\n" in file.read_bytes() else "\n"
    file.write_text(text, encoding="utf-8", newline=newline)


def create_app(root: Path) -> Flask:
    app = Flask(__name__)

    def error(message: str, status: int = 400):
        return jsonify(error=message), status

    def show_file(name: str | None) -> Path | None:
        # Only files the listing offers, so a request can't read arbitrary paths.
        return root / name if name in find_show_files(root) else None

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            effect_types=[str(effect_type) for effect_type in EffectType],
            backends=list(AVAILABLE_BACKENDS),
            default_velocity=DEFAULT_VELOCITY,
            default_output_dir=DEFAULT_OUTPUT_DIR,
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

    @app.get("/api/ports")
    def ports():
        return jsonify(ports=mido.get_output_names())

    @app.post("/api/play")
    def play():
        data = request.get_json()
        port_name = data.get("port")
        if port_name not in mido.get_output_names():
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
            velocity = int(data.get("velocity", DEFAULT_VELOCITY))
        except (TypeError, ValueError):
            return error("Velocity must be a number")
        if not 1 <= velocity <= 127:
            return error("Velocity must be between 1 and 127")

        output_dir = root / (data.get("output_dir") or DEFAULT_OUTPUT_DIR)
        lock_path = lock_path_for(file) if data.get("note_lock", True) else None
        project_path = None
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            show_data = Parser().load_show(file)
            midi_files = MidiGenerator(velocity=velocity).generate_midi_files(
                show_data, output_dir, lock_path
            )
            if backend_name is not None:
                backend = AVAILABLE_BACKENDS[backend_name]()
                backend.create_project(show_data, midi_files, output_dir)
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

    # Media paths in show definitions are relative to the working directory, as in main.py.
    create_app(Path.cwd()).run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
