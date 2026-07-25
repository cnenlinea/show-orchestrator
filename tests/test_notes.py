from show_orchestrator.models import Effect, EffectType
from show_orchestrator.notes import assign_missing_notes


def test_pinned_notes_and_channels_are_kept():
    effects = {
        EffectType.LIGHTS: [Effect(id="a", name="a", note=5, channel=3)],
    }
    mapping = assign_missing_notes(effects)
    assert mapping[EffectType.LIGHTS]["a"].note == 5
    assert mapping[EffectType.LIGHTS]["a"].channel == 3


def test_missing_note_gets_lowest_unused_on_channel():
    effects = {
        EffectType.LIGHTS: [
            Effect(id="a", name="a", note=0),
            Effect(id="b", name="b", note=2),
            Effect(id="c", name="c"),
            Effect(id="d", name="d"),
        ],
    }
    mapping = assign_missing_notes(effects)
    assert mapping[EffectType.LIGHTS]["c"].note == 1
    assert mapping[EffectType.LIGHTS]["d"].note == 3


def test_missing_channel_gets_default():
    effects = {EffectType.LIGHTS: [Effect(id="a", name="a")]}
    mapping = assign_missing_notes(effects, default_channel=7)
    assert mapping[EffectType.LIGHTS]["a"].channel == 7


def test_channels_have_independent_note_spaces():
    effects = {
        EffectType.LIGHTS: [Effect(id="a", name="a", note=0, channel=0)],
        EffectType.PROJECTION: [Effect(id="b", name="b", channel=1)],
    }
    mapping = assign_missing_notes(effects)
    assert mapping[EffectType.PROJECTION]["b"].note == 0


def test_notes_unique_within_channel_across_effect_types():
    effects = {
        EffectType.LIGHTS: [Effect(id="a", name="a", note=0)],
        EffectType.PROJECTION: [Effect(id="b", name="b")],
    }
    mapping = assign_missing_notes(effects)
    assert mapping[EffectType.PROJECTION]["b"].note == 1
