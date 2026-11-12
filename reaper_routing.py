import argparse
import sys
from pathlib import Path

import yaml

from show_orchestrator.backends.reaper import project_track_names, read_midi_routing
from show_orchestrator.parser import Parser


def main():
    arg_parser = argparse.ArgumentParser(
        description=(
            "Read the MIDI output routing out of a REAPER project saved on one machine and\n"
            "print it as a reaper: routing: profile for a YAML show, so builds made with\n"
            "--routing PROFILE come out routed the same way on that machine."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    arg_parser.add_argument("project", type=Path, help="A .rpp saved by REAPER, with the tracks routed by hand.")
    arg_parser.add_argument(
        "-p", "--profile", required=True,
        help="Name for this machine's routing, e.g. mac or foh-pc.",
    )
    arg_parser.add_argument(
        "--show", type=Path,
        help="The show definition, to warn about routed tracks its builds don't have.",
    )
    args = arg_parser.parse_args()

    tracks = read_midi_routing(args.project.read_text(encoding="utf-8", errors="replace"))
    routed: dict[str, int] = {}
    for name, midi_out in tracks:
        if midi_out < 0:
            continue
        if name in routed and routed[name] != midi_out:
            arg_parser.error(f"two tracks named {name!r} go to different MIDI outputs; rename one in REAPER")
        routed[name] = midi_out
    unrouted = [name for name, midi_out in tracks if midi_out < 0 and name not in routed]

    print(yaml.safe_dump({"reaper": {"routing": {args.profile: routed}}},
                         sort_keys=False, allow_unicode=True, width=1000), end="")
    if unrouted:
        print(f"# not routed in {args.project.name}: {', '.join(unrouted)}")

    if args.show:
        built = project_track_names(Parser().load_show(args.show))
        missing = [name for name in routed if name not in built]
        if missing:
            print(f"warning: builds of {args.show.name} have no track named "
                  f"{', '.join(map(repr, missing))}; leave those out (its tracks: {', '.join(built)})",
                  file=sys.stderr)
    if not routed:
        print("warning: no track in the project is routed to a MIDI output", file=sys.stderr)


if __name__ == "__main__":
    main()
