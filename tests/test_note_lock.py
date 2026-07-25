import json
from pathlib import Path

from show_orchestrator.models import Effect, EffectType
from show_orchestrator.notes import assign_missing_notes, lock_path_for


def lights(*effects: Effect) -> dict[EffectType, list[Effect]]:
    return {EffectType.LIGHTS: list(effects)}


def read_lock(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["effects"]


def test_lock_path_replaces_show_suffix():
    assert lock_path_for(Path("shows/castillo/a.csv")) == Path("shows/castillo/a.notes.lock")
    assert lock_path_for(Path("musical.yaml")) == Path("musical.notes.lock")


def test_lock_is_written_with_assignments(tmp_path):
    lock = tmp_path / "show.notes.lock"
    assign_missing_notes(lights(Effect(id="a", name="a")), lock_path=lock)

    assert read_lock(lock) == {"lights": {"a": {"note": 0, "channel": 0}}}


def test_locked_note_survives_a_new_effect_appearing_first(tmp_path):
    lock = tmp_path / "show.notes.lock"
    assign_missing_notes(lights(Effect(id="b", name="b")), lock_path=lock)

    # "a" now sorts ahead of "b" in the show file; without the lock it would
    # take note 0 and silently renumber "b".
    mapping = assign_missing_notes(
        lights(Effect(id="a", name="a"), Effect(id="b", name="b")),
        lock_path=lock,
    )
    assert mapping[EffectType.LIGHTS]["b"].note == 0
    assert mapping[EffectType.LIGHTS]["a"].note == 1


def test_removed_effect_keeps_its_note_reserved(tmp_path):
    lock = tmp_path / "show.notes.lock"
    assign_missing_notes(
        lights(Effect(id="a", name="a"), Effect(id="b", name="b")),
        lock_path=lock,
    )

    # "a" is deleted from the show; its note must not be handed to a newcomer.
    mapping = assign_missing_notes(
        lights(Effect(id="b", name="b"), Effect(id="c", name="c")),
        lock_path=lock,
    )
    assert mapping[EffectType.LIGHTS]["b"].note == 1
    assert mapping[EffectType.LIGHTS]["c"].note == 2
    assert read_lock(lock)["lights"]["a"] == {"note": 0, "channel": 0}


def test_removed_effect_gets_its_note_back_when_re_added(tmp_path):
    lock = tmp_path / "show.notes.lock"
    assign_missing_notes(
        lights(Effect(id="a", name="a"), Effect(id="b", name="b")),
        lock_path=lock,
    )
    assign_missing_notes(lights(Effect(id="b", name="b")), lock_path=lock)

    mapping = assign_missing_notes(
        lights(Effect(id="b", name="b"), Effect(id="a", name="a")),
        lock_path=lock,
    )
    assert mapping[EffectType.LIGHTS]["a"].note == 0


def test_pinned_note_wins_over_lock_and_displaces_it(tmp_path):
    lock = tmp_path / "show.notes.lock"
    assign_missing_notes(lights(Effect(id="a", name="a")), lock_path=lock)

    # The show now pins note 0 to a different effect.
    mapping = assign_missing_notes(
        lights(Effect(id="a", name="a"), Effect(id="pinned", name="pinned", note=0)),
        lock_path=lock,
    )
    assert mapping[EffectType.LIGHTS]["pinned"].note == 0
    assert mapping[EffectType.LIGHTS]["a"].note == 1
    assert read_lock(lock)["lights"]["a"] == {"note": 1, "channel": 0}


def test_channel_change_reallocates_note(tmp_path):
    lock = tmp_path / "show.notes.lock"
    assign_missing_notes(lights(Effect(id="a", name="a", channel=0)), lock_path=lock)

    mapping = assign_missing_notes(lights(Effect(id="a", name="a", channel=1)), lock_path=lock)
    assert mapping[EffectType.LIGHTS]["a"].channel == 1
    assert read_lock(lock)["lights"]["a"] == {"note": 0, "channel": 1}


def test_no_lock_path_writes_nothing(tmp_path):
    assign_missing_notes(lights(Effect(id="a", name="a")))
    assert list(tmp_path.iterdir()) == []


def test_lock_is_not_rewritten_when_unchanged(tmp_path):
    lock = tmp_path / "show.notes.lock"
    effects = lights(Effect(id="a", name="a"))
    assign_missing_notes(effects, lock_path=lock)
    first_mtime = lock.stat().st_mtime_ns

    assign_missing_notes(lights(Effect(id="a", name="a")), lock_path=lock)
    assert lock.stat().st_mtime_ns == first_mtime


def test_lock_round_trips_non_ascii_effect_ids(tmp_path):
    lock = tmp_path / "show.notes.lock"
    assign_missing_notes(lights(Effect(id="Proyección", name="Proyección")), lock_path=lock)

    assert "Proyección" in lock.read_text(encoding="utf-8")
    mapping = assign_missing_notes(
        lights(Effect(id="nuevo", name="nuevo"), Effect(id="Proyección", name="Proyección")),
        lock_path=lock,
    )
    assert mapping[EffectType.LIGHTS]["Proyección"].note == 0
    assert mapping[EffectType.LIGHTS]["nuevo"].note == 1
