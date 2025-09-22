import argparse
from pathlib import Path

from show_orchestrator.parser import Parser
from show_orchestrator.generator import MidiGenerator
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

    args = arg_parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
        
    file: Path = args.file
    parser = Parser()
    show_data = parser.load_show(file)

    midi_generator = MidiGenerator()
    midi_file_paths = midi_generator.generate_midi_files(show_data, args.output_dir)

    backend_name = args.orchestrate
    orchestrator = AVAILABLE_BACKENDS[backend_name]()
    
    orchestrator.create_project(show_data, midi_file_paths, args.output_dir)
    orchestrator.save_project(args.output_dir / f"{file.stem}.rpp")

if __name__ == "__main__":
    main()