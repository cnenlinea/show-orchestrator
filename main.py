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
        description="Generate MIDI files and show projects from a YAML definition.",
        formatter_class=argparse.RawTextHelpFormatter
    )

    arg_parser.add_argument(
        "yaml_file",
        type=Path,
        help="Path to the show definition YAML file."
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
        
    yaml_file: Path = args.yaml_file
    yaml_parser = Parser()
    show_data = yaml_parser.load_show_from_yaml(yaml_file)

    midi_generator = MidiGenerator()
    midi_file_paths = midi_generator.generate_midi_files(show_data, args.output_dir)

    backend_name = args.orchestrate
    orchestrator = AVAILABLE_BACKENDS[backend_name]()
    
    orchestrator.create_project(show_data, midi_file_paths, args.output_dir)
    orchestrator.save_project(args.output_dir / f"{yaml_file.stem}.rpp")

if __name__ == "__main__":
    main()