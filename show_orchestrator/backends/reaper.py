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
        routing: dict[str, int] | None = None,
    ) -> None:
        """Build the project; save it into `output_dir` with `save_project`.

        Media is copied into `output_dir` and referenced relative to it, so
        the folder can be moved or opened on another machine as a whole.
        Songs follow one another with `song_gap` seconds of silence, and no
        cues, between them. `routing` gives tracks, by name, their MIDI
        hardware output (REAPER's MIDIOUT value; see `read_midi_routing`).
        """
        if song_gap < 0:
            raise ValueError(f"The gap between songs can't be negative ({song_gap} s)")
        audio_files_included = any(track.file_path for track in show.audio_tracks)
        if routing:
            names = project_track_names(show)
            unknown = [name for name in routing if name not in names]
            if unknown:
                raise ValueError(
                    f"The routing names tracks the project doesn't have: {', '.join(map(repr, unknown))}; "
                    f"its tracks are {', '.join(map(repr, names))}"
                )

        tracks = {}
        named_tracks: list[tuple[str, Track]] = []  # (name, track), for the routing

        def add_track(name: str) -> Track:
            track = Track(name=name)
            self.project.add(track)
            named_tracks.append((name, track))
            return track

        if audio_files_included:
            tracks["audio"] = add_track("Audio Files")

        for effect_type in show.effects:
            tracks[effect_type] = add_track(str(effect_type))

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
                        new_track = extra_tracks_by_name[extra_track.name] = add_track(extra_track.name)
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

        for name, reaper_track in named_tracks:
            if routing and name in routing:
                reaper_track.props.append(["MIDIOUT", routing[name]])

    def save_project(self, project_file_path: Path) -> None:
        self.project.write(project_file_path)


def reaper_settings(
    show: Show, gap: float | None = None, routing_profile: str | None = None,
) -> tuple[float, dict[str, int] | None]:
    """The (gap between songs, track routing) to build `show` with.

    A `gap` given here wins over the show's `reaper: gap:`, which wins over
    DEFAULT_SONG_GAP. `routing_profile` picks one of the show's
    `reaper: routing:` profiles (one per machine); None routes nothing.
    """
    settings = show.reaper
    if gap is None:
        gap = settings.gap if settings and settings.gap is not None else DEFAULT_SONG_GAP
    if not routing_profile:
        return gap, None
    profiles = (settings.routing if settings else None) or {}
    if routing_profile not in profiles:
        known = ", ".join(map(repr, profiles)) or "none (add a reaper: routing: section)"
        raise ValueError(f"The show has no routing profile {routing_profile!r}; its profiles: {known}")
    return gap, profiles[routing_profile]


def project_track_names(show: Show) -> list[str]:
    """The names of the tracks `ReaperBackend.create_project` makes for `show`, in order."""
    names = ["Audio Files"] if any(track.file_path for track in show.audio_tracks) else []
    names += [str(effect_type) for effect_type in show.effects]
    for track in show.audio_tracks:
        for extra in track.extra_tracks or []:
            if extra.name not in names:
                names.append(extra.name)
    return names


def read_midi_routing(rpp_text: str) -> list[tuple[str, int]]:
    """(track name, MIDI hardware output) of every track in a REAPER project file.

    The output is REAPER's MIDIOUT value, -1 for none. REAPER stores it as an
    index into the MIDI outputs of the machine's own preferences (with the
    channel), not a device name, so it is only right on the machine, or one
    set up alike, where the project was saved.
    """
    tracks: list[tuple[str, int]] = []
    chunks: list[str] = []
    name, midi_out = None, -1
    for raw in rpp_text.splitlines():
        line = raw.strip()
        if line.startswith("<"):
            chunks.append(line[1:].split(" ", 1)[0])
            if chunks == ["REAPER_PROJECT", "TRACK"]:
                name, midi_out = "", -1
        elif line == ">":
            if chunks == ["REAPER_PROJECT", "TRACK"]:
                tracks.append((name, midi_out))
            if chunks:
                chunks.pop()
        elif chunks == ["REAPER_PROJECT", "TRACK"]:
            key, _, value = line.partition(" ")
            if key == "NAME":
                # REAPER quotes names with spaces with ", or ' / ` when the name holds a "
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'`":
                    value = value[1:-1]
                name = value
            elif key == "MIDIOUT":
                midi_out = int(value.split()[0])
    return tracks
