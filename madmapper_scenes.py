import argparse
import csv
from pathlib import Path

from show_orchestrator.madfile import (
    MadFileError, MadProject, mad_file_in, output_path_for, save_project,
)
from show_orchestrator.madscenes import Scene, ScaffoldError, scaffold_scenes


def read_scenes(csv_path: Path) -> list[Scene]:
    """Scenes from a `scene,media` CSV; media paths are relative to the CSV.

    An empty media cell makes a scene whose surfaces show no media.
    """
    scenes = []
    with open(csv_path, newline="", encoding="utf-8-sig") as file:
        for line, row in enumerate(csv.DictReader(file), start=2):
            name = (row.get("scene") or "").strip()
            media = (row.get("media") or "").strip()
            if not name and not media:
                continue
            if not name:
                raise ScaffoldError(f"{csv_path.name} line {line}: media without a scene name")
            media_path = None
            if media:
                media_path = Path(media)
                if not media_path.is_absolute():
                    media_path = csv_path.parent / media_path
            scenes.append(Scene(name, media_path))
    return scenes


def main():
    arg_parser = argparse.ArgumentParser(
        description=(
            "Scaffold scenes in a MadMapper project: per scene, a group holding copies\n"
            "of a template quad or group that show the scene's video/image, and a cue\n"
            "(MadMapper 5) or timeline (MadMapper 6) that shows that scene, hides all the\n"
            "others and restarts its video. The result is saved as a new project next to\n"
            "the original."
        ),
        formatter_class=argparse.RawTextHelpFormatter
    )

    arg_parser.add_argument(
        "project",
        type=Path,
        help="MadMapper 5 .mad file or MadMapper 6 .madproject folder."
    )

    arg_parser.add_argument(
        "scenes",
        type=Path,
        help="CSV with columns scene,media (media paths relative to the CSV; may be empty)."
    )

    arg_parser.add_argument(
        "-t", "--template",
        required=True,
        help="Quad to copy into every scene, or group whose surfaces are all copied."
    )

    arg_parser.add_argument(
        "--fade",
        type=float,
        help="Transition time of the new cues/timelines, in seconds (default: as the project's)"
    )

    arg_parser.add_argument(
        "--blackout",
        metavar="NAME",
        help="Also add a cue/timeline by this name that hides every top-level surface."
    )

    arg_parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Don't make scene triggers restart their video from the beginning."
    )

    arg_parser.add_argument(
        "--bank",
        help="Cue/timeline bank for the new triggers (default: the active bank)."
    )

    arg_parser.add_argument(
        "-o", "--output",
        type=Path,
        help="Where to save the result (default: <project>_scenes next to it)"
    )

    args = arg_parser.parse_args()
    project_path: Path = args.project
    output: Path = args.output or output_path_for(project_path, "_scenes")
    if output.resolve() == project_path.resolve():
        arg_parser.error("the output must be a new project, not the one being read")

    try:
        scenes = read_scenes(args.scenes)
        if not scenes:
            arg_parser.error(f"no scenes in {args.scenes}")
        project = MadProject.load(mad_file_in(project_path))
        report = scaffold_scenes(
            project, scenes, args.template, args.bank,
            fade=args.fade, blackout=args.blackout, restart_videos=not args.no_restart,
        )
        saved = save_project(project, project_path, output)
    except (ScaffoldError, MadFileError) as error:
        arg_parser.error(str(error))

    for name, group_id, surface_ids, media_id in report.scenes:
        media = "no media" if media_id is None else f"media {media_id}"
        print(f"  {name:<30} group {group_id}, surfaces {surface_ids}, {media}")
    for path in report.reused_media:
        print(f"Already in the project, reused: {path.name}")
    for path in report.unknown_durations:
        print(f"Couldn't read the length of {path.name} (is ffprobe installed?); "
              "check its loop end in MadMapper")
    blackout = f" plus {report.blackout!r}" if report.blackout else ""
    print(f"\n{len(report.scenes)} scenes added, each with a {report.trigger_kind}{blackout}, "
          f"in bank {report.bank!r}.")
    print(f"Saved {saved}")


if __name__ == "__main__":
    main()
