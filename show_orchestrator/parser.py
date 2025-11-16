from csv import DictReader
from pathlib import Path

import yaml

from show_orchestrator.models import Show, AudioTrack, Effect, Event


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
                "projection": []
            }
        )
        effects_by_id = {}
        with open(file_path, 'r', encoding='utf-8') as file:
            reader = DictReader(file, field_names)
            for row in reader:
                for key in row:
                    if row[key] == "":
                        row[key] = None
                if row["type"] not in ["audio", "lights", "projection"]:
                    continue
                elif row["type"] == "audio":
                    last_audio_track = AudioTrack(
                        name = row["name"],
                        events = {
                            "lights": [],
                            "projection": []
                        },
                        duration = row["duration"],
                        file_path = row["file"]
                    )
                    self.show.audio_tracks.append(last_audio_track)
                else:
                    if last_audio_track is None:
                        continue

                    if row["name"] not in effects_by_id:
                        effect = Effect(
                            id = row["name"],
                            name = row["name"],
                            note = int(row.get("note")) if row.get("note") else None,
                        )
                        self.show.effects[row["type"]].append(effect)
                    
                    last_audio_track.events[row["type"]].append(
                        Event(
                            timestamp = row["timestamp"],
                            duration = row["duration"],
                            effect_id = row["name"]
                        )
                    )
        return self.show
