import argparse
from pathlib import Path

from show_orchestrator.madfile import (
    MadFileError, MadProject, mad_file_in, output_path_for, save_project,
)
from show_orchestrator.audio import DEFAULT_SILENCE_DB
from show_orchestrator.madmapper import DEFAULT_DEVICE, CueMatchError, note_name, patch_show
from show_orchestrator.models import EffectType
from show_orchestrator.notes import lock_path_for
from show_orchestrator.parser import Parser


def parse_fade(text: str) -> tuple[str, float]:
    """`NAME=SECONDS` for --fade."""
    name, sep, seconds = text.rpartition("=")
    try:
        value = float(seconds)
    except ValueError:
        value = -1.0
    if not sep or not name.strip() or value < 0:
        raise argparse.ArgumentTypeError(f"expected CUE=SECONDS (e.g. \"0. Triumph=0.2\"), not {text!r}")
    return name.strip(), value


def main():
    arg_parser = argparse.ArgumentParser(
        description=(
            "Set the MIDI trigger of every MadMapper cue (MadMapper 6: timeline) named\n"
            "like a show effect to that effect's note, and save the result as a new project."
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
        help="MadMapper 5 .mad file or MadMapper 6 .madproject folder to read cues from."
    )

    arg_parser.add_argument(
        "-o", "--output",
        type=Path,
        help="Where to save the patched project (default: <project>_midi next to it)"
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
        "--fades-from-audio",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Fade each cue in over the silence that follows it in its song's audio,\n"
            "so the picture arrives with the music. Only cues whose effect fires once,\n"
            "while the audio is silent, change; the others keep their fade.\n"
            "Default: the show's madmapper: fades_from_audio:, else off."
        )
    )

    arg_parser.add_argument(
        "--silence-threshold",
        type=float,
        default=None,
        metavar="DB",
        help=(
            "With --fades-from-audio, audio quieter than this counts as silence\n"
            f"(default: the show's madmapper: silence_threshold:, else {DEFAULT_SILENCE_DB:g} dBFS)"
        )
    )

    arg_parser.add_argument(
        "--fade",
        dest="fades",
        action="append",
        type=parse_fade,
        default=[],
        metavar="CUE=SECONDS",
        help=(
            "Set a cue's fade, e.g. --fade \"0. Triumph=0.2\"; repeat for several.\n"
            "Adds to the show's madmapper: fades: (and wins for the same cue);\n"
            "applied last, so these win over --fades-from-audio."
        )
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
    output: Path = args.output or output_path_for(args.project, "_midi")
    if output.resolve() == args.project.resolve():
        arg_parser.error("the output must be a new project, not the one being read")

    show = Parser().load_show(args.file)
    lock_path = None if args.no_note_lock else lock_path_for(args.file)
    try:
        project = MadProject.load(mad_file_in(args.project))
        result = patch_show(
            project, show, lock_path, args.effect_types, args.device,
            fades_from_audio=args.fades_from_audio, silence_threshold=args.silence_threshold, fades=args.fades,
        )
    except (CueMatchError, MadFileError) as error:
        arg_parser.error(f"{error}; nothing saved")
    report = result.report

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

    if result.fades_from_audio:
        changed = [f for f in result.audio_fades if f.fade is not None]
        print(f"\nFades from the audio (silence below {result.silence_threshold:g} dBFS):")
        for fade in changed:
            old = "unset" if fade.old_fade is None else f"{fade.old_fade:g} s"
            print(f"  {fade.cue.name:<30} fade {old} -> {fade.fade:g} s")
        for fade in result.audio_fades:
            if fade.fade is None:
                old = "" if fade.old_fade is None else f" {fade.old_fade:g} s"
                print(f"  {fade.cue.name:<30} kept{old}: {fade.reason}")
        print(f"{len(changed)} fades set.")

    if result.hand_fades:
        print("\nFades set by hand (--fade and the show's madmapper: fades:):")
        for fade in result.hand_fades:
            old = "unset" if fade.old_fade is None else f"{fade.old_fade:g} s"
            print(f"  {fade.cue.name:<30} fade {old} -> {fade.fade:g} s")

    if args.dry_run:
        print("Dry run: nothing saved.")
        return
    try:
        saved = save_project(project, args.project, output)
    except MadFileError as error:
        arg_parser.error(str(error))
    print(f"Saved {saved}")


if __name__ == "__main__":
    main()
