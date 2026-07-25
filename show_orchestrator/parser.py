from csv import DictReader
from pathlib import Path

import yaml

from show_orchestrator.models import Show, AudioTrack, Effect, Event, ExtraAudioTrack


class Parser:
    def __init__(self) -> None:
        self.show = None


    def load_show(self, file_path: Path) -> Show:
        if file_path.suffix in [".yaml", ".yml"]:
            return self.load_show_from_yaml(file_path)
        elif file_path.suffix == ".csv":
            return self.load_show_from_csv(file_path)
        raise ValueError("File type not supported")

    def load_show_from_yaml(self, file_path: Path) -> Show:
        with open(file_path, 'r', encoding='utf-8') as file:
            data = yaml.safe_load(file)
            self.show = Show(**data)
        return self.show

    def load_show_from_csv(self, file_path: Path) -> Show:
        field_names = ["name", "type", "timestamp", "duration", "note", "file"]
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
                        effect = Effect(
                            id = row["name"],
                            name = row["name"],
                            note = note,
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
