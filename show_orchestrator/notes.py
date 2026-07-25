import json
from collections import defaultdict
from pathlib import Path

from show_orchestrator.models import Effect, EffectType

LOCK_VERSION = 1
LOCK_SUFFIX = ".notes.lock"

LockEntries = dict[str, dict[str, dict[str, int]]]


def lock_path_for(show_file: Path) -> Path:
    """Path of the note lock that belongs to a show definition."""
    return show_file.with_suffix(LOCK_SUFFIX)


def load_note_lock(lock_path: Path | None) -> LockEntries:
    if lock_path is None or not lock_path.exists():
        return {}
    with open(lock_path, encoding="utf-8") as lock_file:
        return json.load(lock_file).get("effects", {})


def save_note_lock(
    lock_path: Path,
    effect_mapping: dict[EffectType, dict[str, Effect]],
    previous: LockEntries,
) -> None:
    """Write current assignments, keeping entries for effects no longer in the show."""
    entries: LockEntries = {
        effect_type: dict(locked) for effect_type, locked in previous.items()
    }
    for effect_type, effects_of_type in effect_mapping.items():
        locked = entries.setdefault(str(effect_type), {})
        for effect_id, effect in effects_of_type.items():
            locked[effect_id] = {"note": effect.note, "channel": effect.channel}

    payload = {
        "version": LOCK_VERSION,
        "effects": {
            effect_type: dict(sorted(locked.items()))
            for effect_type, locked in sorted(entries.items())
        },
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if lock_path.exists() and lock_path.read_text(encoding="utf-8") == text:
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(text, encoding="utf-8")


def assign_missing_notes(
    effects: dict[EffectType, list[Effect]],
    default_channel: int = 0,
    lock_path: Path | None = None,
) -> dict[EffectType, dict[str, Effect]]:
    """Give every effect a channel and a note, keeping user-defined notes.

    Effects without a channel get `default_channel`. Effects without a note keep
    the note recorded in the lock file, or take the lowest note still free on
    their channel. When `lock_path` is given, assignments are read from and
    written back to it so notes stay stable as the show changes. Returns the
    effects grouped by type and indexed by effect id.
    """
    locked = load_note_lock(lock_path)
    effect_mapping: dict[EffectType, dict[str, Effect]] = defaultdict(dict)
    used_notes_per_channel: dict[int, set[int]] = defaultdict(set)
    effects_without_note: list[tuple[EffectType, Effect]] = []

    # Notes pinned in the show definition always win.
    for effect_type, effect_list in effects.items():
        for effect in effect_list:
            if effect.channel is None:
                effect.channel = default_channel
            if effect.note is None:
                effects_without_note.append((effect_type, effect))
                continue
            used_notes_per_channel[effect.channel].add(effect.note)
            effect_mapping[effect_type][effect.id] = effect

    # Reserve every note the lock already handed out, including notes belonging
    # to effects that have since left the show, so they are never reused.
    usable_locks: dict[tuple[str, str], dict[str, int]] = {}
    for effect_type, locked_effects in locked.items():
        for effect_id, entry in locked_effects.items():
            if entry["note"] in used_notes_per_channel[entry["channel"]]:
                continue  # a pinned note claimed it; this effect gets a new one
            used_notes_per_channel[entry["channel"]].add(entry["note"])
            usable_locks[(effect_type, effect_id)] = entry

    for effect_type, effect in effects_without_note:
        entry = usable_locks.get((str(effect_type), effect.id))
        if entry is not None and entry["channel"] == effect.channel:
            effect.note = entry["note"]
        else:
            note = 0
            used_notes = used_notes_per_channel[effect.channel]
            while note in used_notes:
                note += 1
            effect.note = note
            used_notes.add(note)
        effect_mapping[effect_type][effect.id] = effect

    effect_mapping = dict(effect_mapping)
    if lock_path is not None:
        save_note_lock(lock_path, effect_mapping, locked)
    return effect_mapping
