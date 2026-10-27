# show-orchestrator

Turn a show cue sheet (YAML or CSV) into MIDI files and a ready-to-open [REAPER](https://www.reaper.fm/) project. Each cue is a timestamped effect — lights, projection, or Home Assistant — that becomes a MIDI note, so during the show REAPER (or any DAW/player) drives [MadMapper](https://madmapper.com/) (projection) and [QLC+](https://www.qlcplus.org/) (lights) in sync with the music. The show's audio tracks are laid out on the same timeline, so the same project is used for rehearsals.

```
show.yaml / show.csv
        │
        ▼
   Parser (show_orchestrator/parser.py)
        │  Show model (pydantic, show_orchestrator/models.py)
        ▼
   MidiGenerator (show_orchestrator/generator.py)
        │  one .mid per (song × effect type)
        ▼
   ReaperBackend (show_orchestrator/backends/reaper.py)
        │
        ▼
   build/<show>.rpp  +  audio media  +  .mid clips
```

## Installation

Requires Python 3.11+ (developed on 3.14) and Git (the `reathon` dependency installs from a Git fork).

```console
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

> **Note on reathon:** we install `reathon` from [our fork](https://github.com/MetaGab/reathon) instead of PyPI `0.0.9`, pinned to a commit. The fork recognizes `.mid` files as `SOURCE MIDI` in the generated `.rpp` and ships the `reathon.helper.marker` helper we use for project markers — the PyPI release has neither. To pick up new fork commits, update the SHA in the `reathon @ git+...` line of [requirements.txt](requirements.txt).

## Usage

Generate MIDI files only:

```console
python main.py shows/castillo/castillo_afuera_a.csv -o build
```

Generate MIDI files **and** a REAPER project:

```console
python main.py shows/castillo/castillo_afuera_a.csv -o build --orchestrate reaper
```

This writes `build/<show>.rpp`, one `.mid` per song and effect type, and copies the referenced audio media next to them. Open the `.rpp` in REAPER.

Each generated `.mid` carries standard MIDI meta events: a track name, and a marker naming the effect at every cue — so REAPER's piano roll shows *"Blackout Luz"* rather than an anonymous note 2.

| Option | Purpose |
| --- | --- |
| `-o`, `--output-dir` | Where to write MIDI, media and the project file (default `./build/`) |
| `--orchestrate BACKEND` | Also build a project file; omit to generate MIDI only |
| `--velocity N` | Velocity of generated `note_on` messages (default 127) |
| `--no-note-lock` | Ignore the note lock file — see below |

Cues fire at velocity **127** by default. QLC+ scales note velocity into the input value, so a lower velocity arrives as a partial level rather than a clean trigger; change it with `--velocity` only if your receivers want that.

### Web UI

```console
python web.py
```

Then open <http://127.0.0.1:5000>. Run it from the project root, like `main.py`. Pick a show definition (any `.csv`/`.yaml` in the root or under `shows/`) to see each song's cues on a timeline and every effect with its MIDI note and channel. Choose a MIDI output port and press **▶** (or click a cue) to audition a note, and use **Build** to write the MIDI files and REAPER project with the same options as the CLI. `--host` and `--port` change where it listens.

The **Edit** tab is a graphical editor for the selected show, the same for CSV and YAML files:

- Each song is a card with its name, length and audio file, a list of its cues and extra tracks, and buttons to add rows, sort them by time, and reorder or delete songs.
- A cue is a time, an optional hold, a type and an effect picked from a list; **＋ New effect…** in that list creates one.
- The lanes above each list show the song as you edit it: drag a cue to move it (hold Shift for tenths of a second), or double-click a lane to add a cue there.
- **Effects** at the top lists every effect. Renaming one renames it in all its cues, and a note (and, for YAML, a channel) can be pinned or left automatic.

Fields that are missing or malformed are outlined in red, and saving (button or Ctrl+S) is refused if the result would not load, so the editor can't leave you with a broken show. Saving rewrites the file: a CSV keeps its header and row order, while a YAML file loses any comments and hand formatting. The **Source** tab still edits the file as text, with the same check on save.

### Note mapper GUI

```console
python note_mapper.py shows/castillo/castillo_afuera_a.csv
```

Opens a small Tk window listing every effect with its assigned MIDI note and channel. Pick a MIDI output port in the dropdown and press **Play Note** to audition a cue against MadMapper/QLC+ before the show.

### MadMapper cue triggers

```console
python madmapper_patch.py shows/castillo/castillo_afuera_a.csv path/to/Castillo.mad
```

Instead of mapping every cue's MIDI trigger by hand in MadMapper, this writes them into the project file: every MadMapper cue named like a `projection` effect gets a trigger on that effect's note and channel — the same notes the generated MIDI files play, note lock included. The result is saved as a new file, `<project>_midi.mad` next to the original (or `-o`); the original is never overwritten. Close the project in MadMapper first, so the file on disk is up to date.

- Names match ignoring case, accents and extra spaces (`Muñecas BG` ↔ `munecas bg`). A YAML effect matches by `name`, or by `id` when the name matches no cue.
- A matched cue's existing note triggers are replaced. Its CC triggers, cues that match no effect, and every other mapping (e.g. a CC driving opacity) are left as they are.
- The output lists what was set, effects with no cue of the same name, and cues that were left alone — rename one side to make them match.

| Option | Purpose |
| --- | --- |
| `-o`, `--output` | Where to save the patched project |
| `--type TYPE` | Effect type to map (default `projection`); repeat for several |
| `--device NAME` | MadMapper MIDI input to listen on (default `all`, any input) |
| `--dry-run` | Print the report without saving |
| `--no-note-lock` | Ignore the note lock file |

MadMapper names notes from `C-2` = 0 (so note 60 is `C3`) and numbers channels from 1, so effect channel 0 shows up as `Midi 1`. The `.mad` reader/writer is [show_orchestrator/madfile.py](show_orchestrator/madfile.py); a project loaded and saved unchanged comes back byte-for-byte identical.

## Show definition formats

Timestamps and durations are `MM:SS` or `MM:SS.sss` strings (or plain seconds as numbers). Event timestamps are **relative to the start of their song**, not the whole show.

### CSV

Columns: `name,type,timestamp,duration,note,file`

| `type` | Meaning |
| --- | --- |
| `audio` | Starts a new song block. `duration` is required; `file` is the audio file path (optional — a song can be a silent placeholder). |
| `lights` / `projection` / `homeassistant` | An effect cue in the current song. `timestamp` is when it fires; `duration` is how long the note is held (default 0.1 s); `note` optionally pins a MIDI note. |
| `extra track` | A pre-rendered `.mid`/audio clip placed on its own REAPER track at `timestamp` within the current song. |

Rows with any other `type` (including the header) are ignored. Rows that reuse an effect `name` reuse the same effect (and note). See [example.csv](example.csv).

### YAML

```yaml
audio_tracks:
  - name: "Opening Song"
    duration: "4:02"
    file_path: "shows/media/opening.mp3"
    events:
      lights:
        - timestamp: "0:20"
          effect_id: "fade_in"
          duration: 30
      projection:
        - timestamp: "0:00"
          effect_id: "blackout_projection"
effects:
  lights:
    - id: "fade_in"
      name: "Fade In (All Lights On)"
      note: 2
      channel: 0
  projection:
    - id: "blackout_projection"
      name: "Set Projections to Black"
      channel: 1
```

YAML gives you more control than CSV: effects are declared in a catalog with explicit `note`/`channel`, so lights and projection can live on separate MIDI channels. See [example.yaml](example.yaml).

### Note auto-assignment and the note lock

Effects without a `note` get the lowest MIDI note not already used on their channel (effects without a `channel` default to 0).

On its own that is fragile: adding one effect could renumber every effect after it, while your MadMapper and QLC+ mappings keep pointing at the old notes. So every assignment is recorded in a **`<show>.notes.lock`** file next to the show definition — `shows/castillo/castillo_afuera_a.notes.lock` for the example above — and reused on the next run.

```json
{
  "version": 1,
  "effects": {
    "projection": {
      "Full Blackout": { "note": 1, "channel": 0 }
    }
  }
}
```

Commit this file. The rules are:

- A `note` pinned in the show definition always wins, and updates the lock.
- An effect that already has a locked note keeps it, wherever it moves in the file.
- Notes belonging to deleted effects stay reserved, so a new effect never inherits a note your rig still has mapped — and re-adding the effect gets its original note back.
- Only genuinely new effects draw a fresh note.

Deleting the lock re-assigns everything from scratch; `--no-note-lock` skips it entirely.

## REAPER / show workflow

1. Open the generated `.rpp`. You get one track per effect type (`lights`, `projection`, `homeassistant`), an **Audio Files** track, one track per extra clip, and a marker at the start of each song.
2. Route each effect track's MIDI output to a virtual MIDI port (e.g. [loopMIDI](https://www.tobias-erichsen.de/software/loopmidi.html) on Windows): track I/O → MIDI Hardware Output.
3. Point MadMapper (Cues → MIDI triggers) and QLC+ (Virtual Console / Input profiles) at those ports and map the notes — `madmapper_patch.py` can set the MadMapper cue triggers for you, and the note mapper GUI auditions them.
4. Press play: audio for the cast, MIDI notes for the machines. Rehearse by scrubbing anywhere in the timeline.

## Project layout

```
main.py                      CLI entry point
note_mapper.py               Tk GUI to inspect/audition effect notes
madmapper_patch.py           CLI: set MadMapper cue MIDI triggers from a show
web.py                       Flask web UI: timeline, editor, note audition, build
templates/index.html         the web UI's single page
show_orchestrator/
  parser.py                  YAML/CSV → Show
  models.py                  pydantic models + MM:SS helpers
  notes.py                   note/channel assignment + notes.lock
  editor.py                  show file ⇄ the document the web editor works on
  generator.py               Show → .mid files (mido)
  madfile.py                 MadMapper .mad reader/writer (Qt QDataStream)
  madmapper.py               effects → MadMapper cue MIDI triggers
  backends/reaper.py         Show + .mid files → .rpp (reathon)
tests/                       pytest suite
shows/                       real show definitions + media (git-ignored)
build/                       generated output (git-ignored)
example.yaml / example.csv   format examples
```

## Running the tests

```console
pytest
```

## Adding a backend

`--orchestrate` looks up `AVAILABLE_BACKENDS` in [main.py](main.py). A backend implements `create_project(show, midi_files, output_dir)` and `save_project(path)` — see [show_orchestrator/backends/reaper.py](show_orchestrator/backends/reaper.py).
