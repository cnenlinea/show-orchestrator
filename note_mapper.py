import argparse
import tkinter
import tkinter.messagebox
from tkinter import ttk
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
        self.root.geometry("500x400")
        self.root.rowconfigure(2, weight=1)
        self.root.columnconfigure(0, weight=1)
        self.root.title("Note Mapper")
        self.label = tkinter.Label(root, text="Note Mapper Tool")
        self.label.grid(row=0, column=0, pady=10)
        self.available_ports = mido.get_output_names() or ["N/A"]
        self.midi_port_dropdown = ttk.Combobox(root, values=self.available_ports)
        self.midi_port_dropdown.set(self.available_ports[0])
        self.midi_port_dropdown.grid(row=1, column=0, pady=5)

    def load_show(self, file_path: str) -> Show:
        parser = Parser()
        self.show = parser.load_show(file_path)
        return self.show

    def _map_show_effects_to_notes(self) -> dict[str, int]:
        if not self.show:
            return
        effects_without_note = defaultdict(list)
        used_notes_per_channel = defaultdict(set)
        for effect_type, effect_list in self.show.effects.items():
            for effect in effect_list:
                note = effect.note
                if effect.channel is None:
                    effect.channel = self.default_channel
                if note is None:
                    effects_without_note[effect_type].append(effect)
                    continue
                used_notes_per_channel[effect.channel].add(note)
                self.effect_mapping[effect_type][effect.id] = effect
        for effect_type, effect_list in effects_without_note.items():
            for effect in effect_list:
                note = 0
                used_notes = used_notes_per_channel[effect.channel]
                while note in used_notes:
                    note += 1
                effect.note = note
                used_notes_per_channel[effect.channel].add(note)
                self.effect_mapping[effect_type][effect.id] = effect
        return self.effect_mapping

    def run(self) -> None:
        self._map_show_effects_to_notes()
        container = tkinter.Frame(self.root)
        container.grid(row=2, column=0, pady=10, padx=10, sticky="nsew")
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)
        canvas = tkinter.Canvas(container)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        scrollable_frame = tkinter.Frame(canvas)
        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(
                scrollregion=canvas.bbox("all")
            )
        )
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        current_row = 0
        for effect_type, effect_dict in self.effect_mapping.items():
            effect_type_label = tkinter.Label(scrollable_frame, text=f"Effect Type: {effect_type}", font=("Arial", 14, "bold"))
            effect_type_label.grid(row=current_row, column=0, columnspan=2, pady=10)
            current_row += 1
            for effect_id, effect in effect_dict.items():
                effect_info = f"Effect ID: {effect_id}, Note: {effect.note}, Channel: {effect.channel}"
                effect_label = tkinter.Label(scrollable_frame, text=effect_info)
                effect_button = tkinter.Button(scrollable_frame, text="Play Note", 
                                               command=lambda n=effect.note, c=effect.channel: self._play_midi_note(n, c))
                effect_label.grid(row=current_row, column=0, padx=5, pady=5, sticky="w")
                effect_button.grid(row=current_row, column=1, padx=5, pady=5, sticky="w")
                current_row += 1

        self.root.mainloop()

    def _play_midi_note(self, note: int, channel: int) -> None:
        current_port = self.midi_port_dropdown.get()
        if current_port == "N/A":
            tkinter.messagebox.showwarning(title="No MIDI port", message="No MIDI port available for operation")
            return
        msg_on = mido.Message('note_on', note=note, velocity=127, channel=channel, time=0)
        msg_off = mido.Message('note_off', note=note, velocity=127, channel=channel, time=0)
        with mido.open_output(current_port) as port:
            port.send(msg_on)
            port.send(msg_off)


if __name__ == "__main__":
    arg_parser = argparse.ArgumentParser(
        description="A simple GUI tool for mapping notes to effects.",
        formatter_class=argparse.RawTextHelpFormatter
    )
    arg_parser.add_argument(
        "file",
        type=Path,
        help="Path to the show definition YAML/CSV file."
    )
    args = arg_parser.parse_args()
    root = tkinter.Tk()
    app = NoteMapper(root)
    app.show = app.load_show(args.file)
    app.run()