from csv import DictReader
from pathlib import Path

import yaml

from show_orchestrator.models import Show, AudioTrack, Effect, Event, ExtraAudioTrack


class Parser:
    def __init__(self) -> None:
        self.show = None


    def load_show(self, file_path: Path) -> Show:
        """Load a show definition and the files it includes."""
        show = self._load_one(file_path)
        for include in show.include or []:
            self._merge_include(show, file_path, include)
        self.show = show
        return show

    def _load_one(self, file_path: Path) -> Show:
        if file_path.suffix in [".yaml", ".yml"]:
            return self.load_show_from_yaml(file_path)
        elif file_path.suffix == ".csv":
            return self.load_show_from_csv(file_path)
        raise ValueError("File type not supported")

    def _merge_include(self, show: Show, file_path: Path, include: str) -> None:
        """Add an included file's cues to the songs of the same name (e.g. the lights timeline that
        light-show-simulator writes). Its audio rows only name the songs; only its cues are taken."""
        path = Path(include)
        if not path.is_absolute():
            path = file_path.parent / path
        if not path.exists():
            raise ValueError(f"Included file {include!r} not found")
        included = self._load_one(path)
        if included.include:
            raise ValueError(f"Included file {include!r} includes other files; only one level is supported")
        tracks = {t.name: t for t in show.audio_tracks}
        for track in included.audio_tracks:
            target = tracks.get(track.name)
            if target is None:
                raise ValueError(f"Included file {include!r} has song {track.name!r}, which the show does not have")
            for effect_type, events in track.events.items():
                target.events.setdefault(effect_type, []).extend(events)
        for effect_type, effects in included.effects.items():
            existing = {e.id for effects_of_type in show.effects.values() for e in effects_of_type}
            for effect in effects:
                if effect.id in existing:
                    raise ValueError(f"Included effect {effect.id!r} has the same name as an effect of the show")
                effect.included_from = include
                show.effects.setdefault(effect_type, []).append(effect)

    def load_show_from_yaml(self, file_path: Path) -> Show:
        with open(file_path, 'r', encoding='utf-8') as file:
            data = yaml.safe_load(file)
            self.show = Show(**data)
        return self.show

    def load_show_from_csv(self, file_path: Path) -> Show:
        # An optional 7th column gives an effect's MIDI channel (from 0). A row of type "include" names,
        # in its file column, a CSV whose cues join this show's songs.
        field_names = ["name", "type", "timestamp", "duration", "note", "file", "channel"]
        self.show = Show(
            audio_tracks = [],
            effects = {
                "lights": [],
                "projection": [],
                "homeassistant": []
            }
        )
        effects_by_id = {}
        last_audio_track = None
        with open(file_path, 'r', encoding='utf-8') as file:
            reader = DictReader(file, field_names)
            for row in reader:
                for key in row:
                    if row[key] == "":
                        row[key] = None
                if row["type"] == "include":
                    if not row["file"]:
                        raise ValueError("An include row needs a file")
                    if self.show.include is None:
                        self.show.include = []
                    self.show.include.append(row["file"])
                    continue
                if row["type"] not in ["audio", "lights", "projection", "extra track", "homeassistant"]:
                    continue
                elif row["type"] == "audio":
                    last_audio_track = AudioTrack(
                        name = row["name"],
                        events = {
                            "lights": [],
                            "projection": [],
                            "homeassistant": []
                        },
                        duration = row["duration"],
                        file_path = row["file"]
                    )
                    self.show.audio_tracks.append(last_audio_track)
                elif row["type"] == "extra track":
                    if last_audio_track is None:
                        raise ValueError(
                            f"Row '{row['name']}' has type '{row['type']}' but appears before any audio row"
                        )
                    extra_track = ExtraAudioTrack(
                        name = row["name"],
                        duration = row["duration"],
                        timestamp = row["timestamp"],
                        file_path = row["file"]
                    )
                    if last_audio_track.extra_tracks is None:
                        last_audio_track.extra_tracks = []
                    last_audio_track.extra_tracks.append(extra_track)
                else:
                    if last_audio_track is None:
                        raise ValueError(
                            f"Row '{row['name']}' has type '{row['type']}' but appears before any audio row"
                        )

                    if row["name"] not in effects_by_id:
                        note = None
                        if row["note"] is not None:
                            try:
                                note = int(row["note"])
                            except ValueError:
                                raise ValueError(
                                    f"Row '{row['name']}' has an invalid MIDI note: {row['note']!r}"
                                )
                        channel = None
                        if row.get("channel") is not None:
                            try:
                                channel = int(row["channel"])
                            except ValueError:
                                raise ValueError(
                                    f"Row '{row['name']}' has an invalid MIDI channel: {row['channel']!r}"
                                )
                        effect = Effect(
                            id = row["name"],
                            name = row["name"],
                            note = note,
                            channel = channel,
                        )
                        self.show.effects[row["type"]].append(effect)
                        effects_by_id[row["name"]] = effect
                    
                    last_audio_track.events[row["type"]].append(
                        Event(
                            timestamp = row["timestamp"],
                            duration = row["duration"],
                            effect_id = row["name"]
                        )
                    )
        return self.show
