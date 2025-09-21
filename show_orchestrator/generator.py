from collections import defaultdict
from pathlib import Path

import mido

from show_orchestrator.models import Event, MidiEvent, Show


class MidiGenerator:
    
    def __init__(self, bpm: int = 120) -> None:
        self.files = {}
        self.bpm = bpm
        self.default_channel = 0
        self.tempo = mido.bpm2tempo(bpm)
        self.used_notes_per_channel = defaultdict(set)

    def _create_midi_file(self, name: str) -> tuple[mido.MidiFile, mido.MidiTrack]:
        mid = mido.MidiFile(type=0)
        track = mido.MidiTrack()
        mid.tracks.append(track)
        self.files[name] = mid
        return mid, track

    def _get_effects_by_id(self, effects: dict) -> dict[str, int]:
        effect_mapping = {}
        effects_without_note = []
        for _, effect_list in effects.items():
            for effect in effect_list:
                note = effect.note
                channel = effect.channel if effect.channel is not None else self.default_channel
                if note is None:
                    effects_without_note.append(effect)
                    continue
                self.used_notes_per_channel[channel].add(note)
                effect_mapping[effect.id] = effect
        for effect in effects_without_note:
            note = 0
            channel = effect.channel if effect.channel is not None else self.default_channel
            used_notes = self.used_notes_per_channel[channel]
            while note in used_notes:
                note += 1
            effect.note = note
            self.used_notes_per_channel[channel].add(note)
            effect_mapping[effect.id] = effect
        return effect_mapping
    
    def _get_midi_events_from_events(self, events: list[Event], effect_mapping: dict) -> list[MidiEvent]:
        midi_events = []
        for event in events:
            timestamp = event.timestamp_seconds
            duration = event.duration_seconds or 0.1
            effect = effect_mapping.get(event.effect_id)
            if effect is None:
                continue
            channel = effect.channel or self.default_channel

            note_on_event = MidiEvent(
                timestamp=timestamp,
                message="note_on",
                channel=channel,
                note=effect.note
            )
            midi_events.append(note_on_event)
            note_off_event = MidiEvent(
                timestamp=timestamp + duration,
                message="note_off",
                channel=channel,
                note=effect.note
            )
            midi_events.append(note_off_event)
        return midi_events

    def generate_midi_files(self, show_data: Show, output_dir: Path) -> dict[str, Path]:
        midi_file_paths = {}
        effect_mapping = self._get_effects_by_id(show_data.effects)
        for audio_track in show_data.audio_tracks:
            track_midi_files = {}
            for effect_type, events in audio_track.events.items():
                mid, track = self._create_midi_file(f"{audio_track.name}_{effect_type}")
                midi_events = self._get_midi_events_from_events(events, effect_mapping)
                midi_events.sort(key=lambda e: e.timestamp)

                current_time = 0
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
                track_midi_files[effect_type] = {
                    "file_path": midi_file_path,
                    "duration": current_time
                }
            midi_file_paths[audio_track.name] = track_midi_files
        return midi_file_paths