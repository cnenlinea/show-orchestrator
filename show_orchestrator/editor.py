"""Format-neutral document the graphical editor works on.

A document is plain JSON-able data, the same shape for CSV and YAML shows:

    {
      "format": "csv" | "yaml",
      "preamble": [[cell, ...], ...],      # CSV rows before the first song (the header)
      "songs": [{"name", "duration", "file", "rows": [row, ...]}],
      "effects": [{"type", "id", "name", "note", "channel"}],
    }

where a row is one of

    {"kind": "cue", "type", "time", "duration", "effect"}     # effect is an effect id
    {"kind": "extra", "name", "time", "duration", "file"}
    {"kind": "ignored", "cells": [...]}                       # CSV row the parser skips

Times and durations stay the text the file holds. Reading is deliberately more
forgiving than the Parser, so a show that does not load can still be opened and
repaired; whether the result loads is checked when it is saved.
"""
import csv
import io

import yaml

from show_orchestrator.models import MMSS_PATTERN, EffectType, to_seconds

EFFECT_TYPES = [str(effect_type) for effect_type in EffectType]
CSV_AUDIO = "audio"
CSV_EXTRA = "extra track"
CSV_COLUMNS = 6


def read_document(text: str, suffix: str) -> dict:
    if suffix == ".csv":
        return _read_csv(text)
    return _read_yaml(text)


def render_document(document: dict, suffix: str) -> str:
    """Text of the show file that `document` describes."""
    _check(document)
    if suffix == ".csv":
        return _write_csv(document)
    return _write_yaml(document)


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _optional_int(value, label: str, highest: int) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a whole number, not {value!r}")
    if not 0 <= number <= highest:
        raise ValueError(f"{label} must be between 0 and {highest}")
    return number


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def _format_mmss(seconds: float) -> str:
    minutes, rest = divmod(round(seconds, 3), 60)
    whole = int(rest)
    fraction = f"{rest - whole:.3f}"[1:].rstrip("0").rstrip(".")
    return f"{int(minutes)}:{whole:02d}{fraction}"


def _check(document: dict) -> None:
    """Catch what the Parser would accept silently or report cryptically."""
    for effect in document.get("effects") or []:
        name = _text(effect.get("name"))
        if not name:
            raise ValueError("Every effect needs a name")
        if effect.get("type") not in EFFECT_TYPES:
            raise ValueError(f"Effect '{name}' has an unknown type: {effect.get('type')!r}")
        _optional_int(effect.get("note"), f"The note of effect '{name}'", 127)
        _optional_int(effect.get("channel"), f"The channel of effect '{name}'", 15)

    for song in document.get("songs") or []:
        song_name = _text(song.get("name"))
        if not song_name:
            raise ValueError("Every song needs a name")
        for row in song.get("rows") or []:
            if row.get("kind") != "cue":
                continue
            if row.get("type") not in EFFECT_TYPES:
                raise ValueError(f"Song '{song_name}': unknown cue type {row.get('type')!r}")
            if not _text(row.get("effect")):
                raise ValueError(
                    f"Song '{song_name}': the cue at {_text(row.get('time')) or '?'} has no effect"
                )


def _read_csv(text: str) -> dict:
    document = {"format": "csv", "preamble": [], "songs": [], "effects": []}
    effect_names = set()
    song = None
    for cells in csv.reader(io.StringIO(text, newline="")):
        if not cells:
            continue
        name, row_type, timestamp, duration, note, file = (
            cell.strip() for cell in (cells + [""] * CSV_COLUMNS)[:CSV_COLUMNS]
        )
        if row_type == CSV_AUDIO:
            song = {"name": name, "duration": duration, "file": file, "rows": []}
            document["songs"].append(song)
        elif row_type not in (*EFFECT_TYPES, CSV_EXTRA):
            if song is None:
                document["preamble"].append(cells)
            else:
                song["rows"].append({"kind": "ignored", "cells": cells})
        elif song is None:
            raise ValueError(
                f"Row '{name}' has type '{row_type}' but appears before any audio row"
            )
        elif row_type == CSV_EXTRA:
            song["rows"].append({
                "kind": "extra", "name": name, "time": timestamp, "duration": duration, "file": file,
            })
        else:
            # As in the Parser, the first row naming an effect decides its type and note.
            if name not in effect_names:
                effect_names.add(name)
                document["effects"].append({
                    "type": row_type,
                    "id": name,
                    "name": name,
                    "note": _optional_int(note, f"The note of row '{name}'", 127),
                    "channel": None,
                })
            song["rows"].append({
                "kind": "cue", "type": row_type, "time": timestamp, "duration": duration, "effect": name,
            })
    return document


def _csv_timestamp(text: str) -> str:
    # CSV timestamps must be MM:SS; accept plain seconds typed into the editor.
    if text and not MMSS_PATTERN.match(text) and _is_number(text):
        return _format_mmss(float(text))
    return text


def _write_csv(document: dict) -> str:
    notes = {
        (effect["type"], _text(effect.get("id"))): effect.get("note")
        for effect in document.get("effects") or []
    }
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerows(document.get("preamble") or [])
    for song in document.get("songs") or []:
        writer.writerow([
            _text(song.get("name")), CSV_AUDIO, "", _text(song.get("duration")), "", _text(song.get("file")),
        ])
        for row in song.get("rows") or []:
            timestamp = _csv_timestamp(_text(row.get("time")))
            duration = _text(row.get("duration"))
            if row.get("kind") == "ignored":
                writer.writerow(row.get("cells") or [])
            elif row.get("kind") == "extra":
                writer.writerow([
                    _text(row.get("name")), CSV_EXTRA, timestamp, duration, "", _text(row.get("file")),
                ])
            else:
                effect_id = _text(row.get("effect"))
                note = notes.get((row["type"], effect_id))
                writer.writerow([
                    effect_id, row["type"], timestamp, duration, "" if note is None else note, "",
                ])
    return output.getvalue()


def _sort_seconds(row: dict) -> float:
    try:
        return to_seconds(row["time"] if ":" in row["time"] else float(row["time"]))
    except ValueError:
        return float("inf")


def _read_yaml(text: str) -> dict:
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError("The YAML file must hold 'audio_tracks' and 'effects' sections")

    effects = [
        {
            "type": str(effect_type),
            "id": _text(effect.get("id")),
            "name": _text(effect.get("name")),
            "note": effect.get("note"),
            "channel": effect.get("channel"),
        }
        for effect_type, effects_of_type in (data.get("effects") or {}).items()
        for effect in effects_of_type or []
    ]

    songs = []
    for track in data.get("audio_tracks") or []:
        cues = [
            {
                "kind": "cue",
                "type": str(effect_type),
                "time": _text(event.get("timestamp")),
                "duration": _text(event.get("duration")),
                "effect": _text(event.get("effect_id")),
            }
            for effect_type, events in (track.get("events") or {}).items()
            for event in events or []
        ]
        # The file groups cues by type; the editor shows them as one running order.
        cues.sort(key=_sort_seconds)
        extras = [
            {
                "kind": "extra",
                "name": _text(extra.get("name")),
                "time": _text(extra.get("timestamp")),
                "duration": _text(extra.get("duration")),
                "file": _text(extra.get("file_path")),
            }
            for extra in track.get("extra_tracks") or []
        ]
        songs.append({
            "name": _text(track.get("name")),
            "duration": _text(track.get("duration")),
            "file": _text(track.get("file_path")),
            "rows": extras + cues,
        })
    return {"format": "yaml", "preamble": [], "songs": songs, "effects": effects}


def _yaml_time(text: str) -> str | int | float:
    """MM:SS stays a string, plain seconds become a number, as the models expect."""
    if MMSS_PATTERN.match(text) or not _is_number(text):
        return text
    seconds = float(text)
    return int(seconds) if seconds.is_integer() else seconds


def _write_yaml(document: dict) -> str:
    tracks = []
    for song in document.get("songs") or []:
        track = {"name": _text(song.get("name")), "duration": _yaml_time(_text(song.get("duration")))}
        if _text(song.get("file")):
            track["file_path"] = _text(song.get("file"))
        events: dict[str, list] = {}
        extra_tracks = []
        for row in song.get("rows") or []:
            if row.get("kind") == "extra":
                extra_tracks.append({
                    "name": _text(row.get("name")),
                    "timestamp": _yaml_time(_text(row.get("time"))),
                    "duration": _yaml_time(_text(row.get("duration"))),
                    "file_path": _text(row.get("file")),
                })
            elif row.get("kind") == "cue":
                event = {
                    "timestamp": _yaml_time(_text(row.get("time"))),
                    "effect_id": _text(row.get("effect")),
                }
                if _text(row.get("duration")):
                    event["duration"] = _yaml_time(_text(row.get("duration")))
                events.setdefault(row["type"], []).append(event)
        track["events"] = events
        if extra_tracks:
            track["extra_tracks"] = extra_tracks
        tracks.append(track)

    effects: dict[str, list] = {}
    for effect in document.get("effects") or []:
        entry = {"id": _text(effect.get("id")), "name": _text(effect.get("name"))}
        for key in ("note", "channel"):
            if effect.get(key) not in (None, ""):
                entry[key] = int(effect[key])
        effects.setdefault(effect["type"], []).append(entry)

    return yaml.safe_dump(
        {"audio_tracks": tracks, "effects": effects},
        sort_keys=False, allow_unicode=True, width=1000,
    )
