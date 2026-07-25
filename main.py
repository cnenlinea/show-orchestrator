import argparse
from pathlib import Path

from show_orchestrator.parser import Parser
from show_orchestrator.generator import DEFAULT_VELOCITY, MidiGenerator
from show_orchestrator.notes import lock_path_for
from show_orchestrator.backends.reaper import ReaperBackend

AVAILABLE_BACKENDS = {
    "reaper": ReaperBackend,
}


def main():
    arg_parser = argparse.ArgumentParser(
        description="Generate MIDI files and show projects from a YAML/CSV definition.",
        formatter_class=argparse.RawTextHelpFormatter
    )

    arg_parser.add_argument(
        "file",
        type=Path,
        help="Path to the show definition YAML/CSV file."
    )

    arg_parser.add_argument(
        "-o", "--output-dir",
        type=Path,
        default=Path("build"),
        help="Directory to save the generated files (default: ./build/)"
    )

    arg_parser.add_argument(
        "--orchestrate",
        choices=AVAILABLE_BACKENDS.keys(),
        metavar="BACKEND",
        help=(
            "Generate MIDI files AND create a project file for the specified backend.\n"
            f"Available backends: {', '.join(AVAILABLE_BACKENDS.keys())}"
        )
    )

    arg_parser.add_argument(
        "--velocity",
        type=int,
        default=DEFAULT_VELOCITY,
        help=f"Velocity of the generated note_on messages (default: {DEFAULT_VELOCITY})"
    )

    arg_parser.add_argument(
        "--no-note-lock",
        action="store_true",
        help=(
            "Do not read or write the <show>.notes.lock file.\n"
            "Auto-assigned notes may then change when effects are added or removed."
        )
    )

    args = arg_parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    file: Path = args.file
    parser = Parser()
    show_data = parser.load_show(file)

    lock_path = None if args.no_note_lock else lock_path_for(file)
    midi_generator = MidiGenerator(velocity=args.velocity)
    midi_file_paths = midi_generator.generate_midi_files(show_data, args.output_dir, lock_path)

    if args.orchestrate is None:
        print(f"MIDI files written to {args.output_dir}. Pass --orchestrate to also build a project file.")
        return

    orchestrator = AVAILABLE_BACKENDS[args.orchestrate]()
    orchestrator.create_project(show_data, midi_file_paths, args.output_dir)
    orchestrator.save_project(args.output_dir / f"{file.stem}.rpp")

if __name__ == "__main__":
    main()