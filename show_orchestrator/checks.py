"""Checks that keep the lights channels to the lights timelines and every note to one effect.

Lights come only from the timelines that light-show-simulator writes and a
show includes (`<variant>.lights.csv`); nobody adds lights cues by hand here.
So, once a show includes a file:

- a `lights` row that does not come from an included file is refused,
- an effect of another type on a lights channel is refused.

And always: two effects may not share a note and channel, whatever their type.
Channels count from 0, as everywhere in show-orchestrator: the default lights
channels 1 and 2 are MIDI channels 2 and 3.
"""
from show_orchestrator.models import Effect, EffectType, Show

LIGHTS_CHANNELS = (1, 2)


class ShowCheckError(ValueError):
    pass


def check_show(show: Show, lights_channels: tuple[int, ...] = LIGHTS_CHANNELS) -> None:
    problems = []
    if show.include:
        for effect in show.effects.get(EffectType.LIGHTS, []):
            if effect.included_from is None:
                problems.append(f"lights effect {effect.id!r} is not from an included lights file")
    for effect_type, effects in show.effects.items():
        if effect_type == EffectType.LIGHTS:
            continue
        for effect in effects:
            if effect.channel in lights_channels:
                problems.append(f"{effect_type} effect {effect.id!r} uses lights channel {effect.channel}")
    if problems:
        raise ShowCheckError("; ".join(problems))


def check_note_clashes(effect_mapping: dict[EffectType, dict[str, Effect]]) -> None:
    """After notes are assigned: refuse two effects on the same note and channel."""
    seen: dict[tuple[int, int], str] = {}
    problems = []
    for effect_type, effects in effect_mapping.items():
        for effect in effects.values():
            key = (effect.channel, effect.note)
            label = f"{effect_type} {effect.id!r}"
            if key in seen:
                problems.append(f"{seen[key]} and {label} share note {effect.note} on channel {effect.channel}")
            else:
                seen[key] = label
    if problems:
        raise ShowCheckError("; ".join(problems))
