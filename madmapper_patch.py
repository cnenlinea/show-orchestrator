import argparse
from pathlib import Path

from show_orchestrator.madfile import MadProject
from show_orchestrator.madmapper import (
    DEFAULT_DEVICE, CueMatchError, note_name, patch_cue_triggers,
)
from show_orchestrator.models import EffectType
from show_orchestrator.notes import assign_missing_notes, lock_path_for
from show_orchestrator.parser import Parser


def main():
    arg_parser = argparse.ArgumentParser(
        description=(
            "Set the MIDI trigger of every MadMapper cue named like a show effect\n"
            "to that effect's note, and save the result as a new .mad file."
        ),
        formatter_class=argparse.RawTextHelpFormatter
    )

    arg_parser.add_argument(
        "file",
        type=Path,
        help="Path to the show definition YAML/CSV file."
    )

    arg_parser.add_argument(
        "project",
        type=Path,
        help="MadMapper project (.mad) to read cues from."
    )

    arg_parser.add_argument(
        "-o", "--output",
        type=Path,
        help="Where to save the patched project (default: <project>_midi.mad next to it)"
    )

    arg_parser.add_argument(
        "--type",
        dest="effect_types",
        action="append",
        choices=[str(effect_type) for effect_type in EffectType],
        help=(
            "Effect type whose effects become cue triggers; repeat for several.\n"
            f"Default: {EffectType.PROJECTION}"
        )
    )

    arg_parser.add_argument(
        "--device",
        default=DEFAULT_DEVICE,
        help=f"MadMapper MIDI input to listen on (default: {DEFAULT_DEVICE!r}, any input)"
    )

    arg_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without saving anything."
    )

    arg_parser.add_argument(
        "--no-note-lock",
        action="store_true",
        help=(
            "Do not read or write the <show>.notes.lock file.\n"
            "Auto-assigned notes may then differ from the ones in your MIDI files."
        )
    )

    args = arg_parser.parse_args()
    output: Path = args.output or args.project.with_name(f"{args.project.stem}_midi.mad")
    if output.resolve() == args.project.resolve():
        arg_parser.error("the output must be a new file, not the project being read")

    show = Parser().load_show(args.file)
    lock_path = None if args.no_note_lock else lock_path_for(args.file)
    effect_mapping = assign_missing_notes(show.effects, 0, lock_path)
    effect_types = args.effect_types or [str(EffectType.PROJECTION)]
    effects = [
        effect
        for effect_type in effect_types
        for effect in effect_mapping.get(EffectType(effect_type), {}).values()
    ]

    project = MadProject.load(args.project)
    try:
        report = patch_cue_triggers(project, effects, args.device)
    except CueMatchError as error:
        arg_parser.error(str(error))

    for effect_id, cue, note, channel in report.patched:
        print(f"  {cue.name:<30} <- {effect_id} (note {note} = {note_name(note)}, channel {channel})")
    if report.unmatched_effects:
        print(f"\nEffects with no cue of the same name ({len(report.unmatched_effects)}):")
        for effect in report.unmatched_effects:
            print(f"  {effect.id} (note {effect.note})")
    if report.unmatched_cues:
        print(f"\nCues left as they were ({len(report.unmatched_cues)}):")
        for cue in report.unmatched_cues:
            print(f"  {cue.name}")

    print(f"\n{len(report.patched)} cue triggers set.")
    if args.dry_run:
        print("Dry run: nothing saved.")
        return
    project.save(output)
    print(f"Saved {output}")


if __name__ == "__main__":
    main()
