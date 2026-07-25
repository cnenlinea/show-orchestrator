from pathlib import Path
from typing import TypedDict

import mido

from show_orchestrator.models import Effect, EffectType, Event, MidiEvent, Show
from show_orchestrator.notes import assign_missing_notes

# Full velocity: cues are triggers, and QLC+ scales note velocity into the
# input value, so anything less arrives as a partial level.
DEFAULT_VELOCITY = 127


class GeneratedMidi(TypedDict):
    file_path: Path
    duration: float


def meta_safe(text: str) -> str:
    """MIDI meta events are latin-1; keep exotic characters from breaking the save."""
    return text.encode("latin-1", errors="replace").decode("latin-1")


class MidiGenerator:

    def __init__(self, bpm: int = 120, velocity: int = DEFAULT_VELOCITY) -> None:
        self.files = {}
        self.bpm = bpm
        self.velocity = velocity
        self.default_channel = 0
        self.tempo = mido.bpm2tempo(bpm)

    def _create_midi_file(self, name: str) -> tuple[mido.MidiFile, mido.MidiTrack]:
        mid = mido.MidiFile(type=0)
        track = mido.MidiTrack()
        mid.tracks.append(track)
        self.files[name] = mid
        return mid, track

    def _get_effects_by_id(
        self,
        effects: dict[EffectType, list[Effect]],
        lock_path: Path | None = None,
    ) -> dict[str, Effect]:
        effect_mapping = assign_missing_notes(effects, self.default_channel, lock_path)
        return {
            effect.id: effect
            for effects_of_type in effect_mapping.values()
            for effect in effects_of_type.values()
        }

    def _get_midi_events_from_events(self, events: list[Event], effect_mapping: dict[str, Effect]) -> list[MidiEvent]:
        midi_events = []
        for event in events:
            timestamp = event.timestamp_seconds
            duration = event.duration_seconds if event.duration_seconds is not None else 0.1
            effect = effect_mapping.get(event.effect_id)
            if effect is None:
                continue

            note_on_event = MidiEvent(
                timestamp=timestamp,
                message="note_on",
                channel=effect.channel,
                note=effect.note,
                label=effect.name
            )
            midi_events.append(note_on_event)
            note_off_event = MidiEvent(
                timestamp=timestamp + duration,
                message="note_off",
                channel=effect.channel,
                note=effect.note
            )
            midi_events.append(note_off_event)
        return midi_events

    def generate_midi_files(
        self,
        show_data: Show,
        output_dir: Path,
        lock_path: Path | None = None,
    ) -> dict[str, dict[EffectType, GeneratedMidi]]:
        midi_file_paths = {}
        effect_mapping = self._get_effects_by_id(show_data.effects, lock_path)
        for audio_track in show_data.audio_tracks:
            track_midi_files = {}
            for effect_type, events in audio_track.events.items():
                midi_events = self._get_midi_events_from_events(events, effect_mapping)
                if not midi_events:
                    continue
                midi_events.sort(key=lambda e: e.timestamp)

                name = f"{audio_track.name}_{effect_type}"
                mid, track = self._create_midi_file(name)
                track.append(mido.MetaMessage("track_name", name=meta_safe(name), time=0))

                current_time = 0
                for event in midi_events:
                    delta_time = event.timestamp - current_time
                    midi_time = mido.second2tick(delta_time, mid.ticks_per_beat, tempo=self.tempo)
                    # Name the cue in the piano roll, then fire it at the same instant.
                    if event.label:
                        track.append(mido.MetaMessage("marker", text=meta_safe(event.label), time=midi_time))
                        midi_time = 0
                    midi_message = mido.Message(event.message,
                                                note=event.note,
                                                channel=event.channel,
                                                velocity=self.velocity if event.message == "note_on" else 0,
                                                time=midi_time)
                    track.append(midi_message)
                    current_time = event.timestamp

                midi_file_path = output_dir / f"{name}.mid"
                mid.save(midi_file_path)
                track_midi_files[effect_type] = GeneratedMidi(
                    file_path=midi_file_path,
                    duration=current_time
                )
            midi_file_paths[audio_track.name] = track_midi_files
        return midi_file_paths
