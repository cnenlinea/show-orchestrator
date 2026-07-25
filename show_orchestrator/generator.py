from pathlib import Path
from typing import TypedDict

import mido

from show_orchestrator.models import Effect, EffectType, Event, MidiEvent, Show
from show_orchestrator.notes import assign_missing_notes


class GeneratedMidi(TypedDict):
    file_path: Path
    duration: float


class MidiGenerator:

    def __init__(self, bpm: int = 120) -> None:
        self.files = {}
        self.bpm = bpm
        self.default_channel = 0
        self.tempo = mido.bpm2tempo(bpm)

    def _create_midi_file(self, name: str) -> tuple[mido.MidiFile, mido.MidiTrack]:
        mid = mido.MidiFile(type=0)
        track = mido.MidiTrack()
        mid.tracks.append(track)
        self.files[name] = mid
        return mid, track

    def _get_effects_by_id(self, effects: dict[EffectType, list[Effect]]) -> dict[str, Effect]:
        effect_mapping = assign_missing_notes(effects, self.default_channel)
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
                note=effect.note
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

    def generate_midi_files(self, show_data: Show, output_dir: Path) -> dict[str, dict[EffectType, GeneratedMidi]]:
        midi_file_paths = {}
        effect_mapping = self._get_effects_by_id(show_data.effects)
        for audio_track in show_data.audio_tracks:
            track_midi_files = {}
            for effect_type, events in audio_track.events.items():
                mid, track = self._create_midi_file(f"{audio_track.name}_{effect_type}")
                midi_events = self._get_midi_events_from_events(events, effect_mapping)
                midi_events.sort(key=lambda e: e.timestamp)

                current_time = 0
                if not midi_events:
                    continue
                for event in midi_events:
                    delta_time = event.timestamp - current_time
                    midi_time = mido.second2tick(delta_time, mid.ticks_per_beat, tempo=self.tempo)
                    midi_message = mido.Message(event.message,
                                                note=event.note,
                                                channel=event.channel,
                                                time=midi_time)
                    track.append(midi_message)
                    current_time = event.timestamp

                midi_file_path = output_dir / f"{audio_track.name}_{effect_type}.mid"
                mid.save(midi_file_path)
                track_midi_files[effect_type] = GeneratedMidi(
                    file_path=midi_file_path,
                    duration=current_time
                )
            midi_file_paths[audio_track.name] = track_midi_files
        return midi_file_paths
