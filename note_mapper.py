import argparse
import tkinter
from collections import defaultdict
from pathlib import Path

import mido

from show_orchestrator.parser import Parser
from show_orchestrator.models import Show


class NoteMapper:

    def __init__(self, root: tkinter.Tk) -> None:
        self.root = root
        self.show = None
        self.default_channel = 0
        self.effect_mapping = defaultdict(dict)
        self.root.title("Note Mapper")
        self.label = tkinter.Label(root, text="Note Mapper Tool")
        self.label.pack(pady=20)

    def load_show(self, file_path: str) -> Show:
        parser = Parser()
        self.show = parser.load_show_from_yaml(file_path)
        return self.show

    def _map_show_effects_to_notes(self) -> dict[str, int]:
        if not self.show:
            return
        effects_without_note = defaultdict(list)
        used_notes_per_channel = defaultdict(set)
        for effect_type, effect_list in self.show.effects.items():
            for effect in effect_list:
                note = effect.note
                channel = effect.channel if effect.channel is not None else self.default_channel
                if note is None:
                    effects_without_note[effect_type].append(effect)
                    continue
                used_notes_per_channel[channel].add(note)
                self.effect_mapping[effect_type][effect.id] = effect
        for effect_type, effect_list in effects_without_note.items():
            for effect in effect_list:
                note = 0
                channel = effect.channel if effect.channel is not None else self.default_channel
                used_notes = used_notes_per_channel[channel]
                while note in used_notes:
                    note += 1
                effect.note = note
                used_notes_per_channel[channel].add(note)
                self.effect_mapping[effect_type][effect.id] = effect
        return self.effect_mapping

    def run(self) -> None:
        self._map_show_effects_to_notes()

        effect_frame = tkinter.Frame(self.root)
        effect_frame.pack(pady=10)

        current_row = 0
        for effect_type, effect_dict in self.effect_mapping.items():
            effect_type_label = tkinter.Label(effect_frame, text=f"Effect Type: {effect_type}", font=("Arial", 14, "bold"))
            effect_type_label.grid(row=current_row, column=0, columnspan=2, pady=10)
            current_row += 1
            for effect_id, effect in effect_dict.items():
                effect_info = f"Effect ID: {effect_id}, Note: {effect.note}, Channel: {effect.channel}"
                effect_label = tkinter.Label(effect_frame, text=effect_info)
                effect_button = tkinter.Button(effect_frame, text="Play Note", 
                                               command=lambda n=effect.note, c=effect.channel: self._play_midi_note(n, c))
                effect_label.grid(row=current_row, column=0, padx=5, pady=5, sticky="w")
                effect_button.grid(row=current_row, column=1, padx=5, pady=5, sticky="w")
                current_row += 1

        self.root.mainloop()

    def _play_midi_note(self, note: int, channel: int) -> None:
        msg_on = mido.Message('note_on', note=note, velocity=64, channel=channel)
        msg_off = mido.Message('note_off', note=note, velocity=64, channel=channel)
        with mido.open_output() as port:
            print(f"Playing note {note} on channel {channel}")
            port.send(msg_on)
            port.send(msg_off)


if __name__ == "__main__":
    arg_parser = argparse.ArgumentParser(
        description="A simple GUI tool for mapping notes to effects.",
        formatter_class=argparse.RawTextHelpFormatter
    )
    arg_parser.add_argument(
        "yaml_file",
        type=Path,
        help="Path to the show definition YAML file."
    )
    args = arg_parser.parse_args()
    root = tkinter.Tk()
    app = NoteMapper(root)
    app.show = app.load_show(args.yaml_file)
    app.run()