from pathlib import Path

from reathon.nodes import Project, Track, Item, Source


from show_orchestrator.models import Show
from reathon.helper import marker


class ReaperBackend:

    def __init__(self) -> None:
        self.project = Project()

    def create_project(self, show: Show, midi_files: dict[str, str], output_dir: Path) -> None:
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
                source = Source(file=str(track.file_path))
                item = Item(
                    source,
                    position=current_position,
                    length=track.duration_seconds
                )
                tracks["audio"].add(item)
                file_path = Path(track.file_path)
                output_audio_path = output_dir / file_path.name
                if not output_audio_path.exists():
                    with open(file_path, 'rb') as src_file, open(output_audio_path, 'wb') as dst_file:
                        dst_file.write(src_file.read())

            if track.extra_tracks:
                for extra_track in track.extra_tracks:
                    new_track = Track(name=extra_track.name)
                    self.project.add(new_track)
                    source = Source(file=str(extra_track.file_path))
                    file_path = Path(extra_track.file_path)
                    output_audio_path = output_dir / file_path.name
                    if not output_audio_path.exists():
                        with open(file_path, 'rb') as src_file, open(output_audio_path, 'wb') as dst_file:
                            dst_file.write(src_file.read())
                    item = Item(
                        source,
                        position=current_position+extra_track.timestamp_seconds,
                        length=extra_track.duration_seconds
                    )
                    new_track.add(item)
                
            midi_file_paths = midi_files.get(track.name, {})
            for effect_type, midi_file_path in midi_file_paths.items():
                source = Source(file=str(midi_file_path["file_path"]))
                item = Item(
                    source,
                    position=current_position,
                    length=midi_file_path["duration"]
                )
                effect_track = tracks.get(effect_type)
                if effect_track:
                    effect_track.add(item)
            
            self.project.props.append(marker(track_index, current_position, track.name))
            track_index += 1
            current_position += track.duration_seconds

    def save_project(self, project_file_path: Path) -> None:
        self.project.write(project_file_path)