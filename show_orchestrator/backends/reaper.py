import shutil
from pathlib import Path

from reathon.nodes import Project, Track, Item, Source
from reathon.helper import marker

from show_orchestrator.generator import GeneratedMidi
from show_orchestrator.models import EffectType, Show


# Formats reathon doesn't know about; Reaper reads these through its video decoder
SOURCE_FORMAT_OVERRIDES = {
    ".m4a": "VIDEO",
    ".mp4": "VIDEO",
}


class ReaperBackend:

    def __init__(self) -> None:
        self.project = Project()

    def _create_source(self, file_path: str | Path) -> Source:
        source = Source(file=str(file_path))
        override = SOURCE_FORMAT_OVERRIDES.get(Path(file_path).suffix.lower())
        if override:
            source.name = f"SOURCE {override}"
        return source

    def _copy_media(self, file_path: Path, output_dir: Path) -> None:
        if not file_path.exists():
            raise FileNotFoundError(f"Media file not found: {file_path}")
        output_path = output_dir / file_path.name
        if not output_path.exists():
            shutil.copy2(file_path, output_path)

    def create_project(
        self,
        show: Show,
        midi_files: dict[str, dict[EffectType, GeneratedMidi]],
        output_dir: Path,
    ) -> None:
        audio_files_included = any(track.file_path for track in show.audio_tracks)

        tracks = {}

        if audio_files_included:
            audio_track = Track(name="Audio Files")
            self.project.add(audio_track)
            tracks["audio"] = audio_track

        for effect_type in show.effects:
            effect_track = Track(name=effect_type)
            self.project.add(effect_track)
            tracks[effect_type] = effect_track

        current_position = 0
        track_index = 0
        for track in show.audio_tracks:
            if track.file_path:
                source = self._create_source(track.file_path)
                item = Item(
                    source,
                    position=current_position,
                    length=track.duration_seconds
                )
                tracks["audio"].add(item)
                self._copy_media(Path(track.file_path), output_dir)

            if track.extra_tracks:
                for extra_track in track.extra_tracks:
                    new_track = Track(name=extra_track.name)
                    self.project.add(new_track)
                    source = self._create_source(extra_track.file_path)
                    self._copy_media(Path(extra_track.file_path), output_dir)
                    item = Item(
                        source,
                        position=current_position+extra_track.timestamp_seconds,
                        length=extra_track.duration_seconds
                    )
                    new_track.add(item)

            midi_file_paths = midi_files.get(track.name, {})
            for effect_type, generated_midi in midi_file_paths.items():
                source = self._create_source(generated_midi["file_path"])
                item = Item(
                    source,
                    position=current_position,
                    length=generated_midi["duration"]
                )
                effect_track = tracks.get(effect_type)
                if effect_track:
                    effect_track.add(item)

            self.project.props.append(marker(track_index, current_position, track.name))
            track_index += 1
            current_position += track.duration_seconds

    def save_project(self, project_file_path: Path) -> None:
        self.project.write(project_file_path)
