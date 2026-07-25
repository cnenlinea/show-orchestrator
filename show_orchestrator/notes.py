from collections import defaultdict

from show_orchestrator.models import Effect, EffectType


def assign_missing_notes(
    effects: dict[EffectType, list[Effect]],
    default_channel: int = 0,
) -> dict[EffectType, dict[str, Effect]]:
    """Give every effect a channel and a note, keeping user-defined notes.

    Effects without a channel get `default_channel`. Effects without a note get
    the lowest note not already used on their channel. Returns the effects
    grouped by type and indexed by effect id.
    """
    effect_mapping: dict[EffectType, dict[str, Effect]] = defaultdict(dict)
    used_notes_per_channel: dict[int, set[int]] = defaultdict(set)
    effects_without_note: list[tuple[EffectType, Effect]] = []

    for effect_type, effect_list in effects.items():
        for effect in effect_list:
            if effect.channel is None:
                effect.channel = default_channel
            if effect.note is None:
                effects_without_note.append((effect_type, effect))
                continue
            used_notes_per_channel[effect.channel].add(effect.note)
            effect_mapping[effect_type][effect.id] = effect

    for effect_type, effect in effects_without_note:
        note = 0
        used_notes = used_notes_per_channel[effect.channel]
        while note in used_notes:
            note += 1
        effect.note = note
        used_notes.add(note)
        effect_mapping[effect_type][effect.id] = effect

    return dict(effect_mapping)
