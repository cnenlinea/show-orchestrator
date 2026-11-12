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
    ".mpeg": "VIDEO",
    ".mpg": "VIDEO",
}

# Seconds of nothing between one song's end and the next song's start, so the
# operator can pause before the next song's cues fire.
DEFAULT_SONG_GAP = 0.5


class ReaperBackend:

    def __init__(self) -> None:
        self.project = Project()

    def _create_source(self, file_path: str | Path) -> Source:
        source = Source(file=str(file_path))
        override = SOURCE_FORMAT_OVERRIDES.get(Path(file_path).suffix.lower())
        if override:
            source.name = f"SOURCE {override}"
        return source

    def _copy_media(self, file_path: Path, output_dir: Path) -> Path:
        """Copy media next to the project; returns its path relative to the project."""
        if not file_path.exists():
            raise FileNotFoundError(f"Media file not found: {file_path}")
        output_path = output_dir / file_path.name
        if not output_path.exists():
            shutil.copy2(file_path, output_path)
        return Path(file_path.name)

    @staticmethod
    def _relative_to_project(file_path: Path, output_dir: Path) -> Path:
        """`file_path` relative to the project folder when it is inside it."""
        try:
            return Path(file_path).resolve().relative_to(output_dir.resolve())
        except ValueError:
            return Path(file_path).resolve()

    def create_project(
        self,
        show: Show,
        midi_files: dict[str, dict[EffectType, GeneratedMidi]],
        output_dir: Path,
        song_gap: float = DEFAULT_SONG_GAP,
    ) -> None:
        """Build the project; save it into `output_dir` with `save_project`.

        Media is copied into `output_dir` and referenced relative to it, so
        the folder can be moved or opened on another machine as a whole.
        Songs follow one another with `song_gap` seconds of silence, and no
        cues, between them.
        """
        if song_gap < 0:
            raise ValueError(f"The gap between songs can't be negative ({song_gap} s)")
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

        # Extra clips with the same name share one REAPER track, e.g. a lights MIDI file per song.
        extra_tracks_by_name: dict[str, Track] = {}

        current_position = 0
        track_index = 0
        for track in show.audio_tracks:
            if track.file_path:
                media = self._copy_media(Path(track.file_path), output_dir)
                source = self._create_source(media)
                item = Item(
                    source,
                    position=current_position,
                    length=track.duration_seconds
                )
                tracks["audio"].add(item)

            if track.extra_tracks:
                for extra_track in track.extra_tracks:
                    new_track = extra_tracks_by_name.get(extra_track.name)
                    if new_track is None:
                        new_track = Track(name=extra_track.name)
                        self.project.add(new_track)
                        extra_tracks_by_name[extra_track.name] = new_track
                    media = self._copy_media(Path(extra_track.file_path), output_dir)
                    source = self._create_source(media)
                    item = Item(
                        source,
                        position=current_position+extra_track.timestamp_seconds,
                        length=extra_track.duration_seconds
                    )
                    new_track.add(item)

            midi_file_paths = midi_files.get(track.name, {})
            for effect_type, generated_midi in midi_file_paths.items():
                source = self._create_source(
                    self._relative_to_project(generated_midi["file_path"], output_dir)
                )
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
            current_position += track.duration_seconds + song_gap

    def save_project(self, project_file_path: Path) -> None:
        self.project.write(project_file_path)
