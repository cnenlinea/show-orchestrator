"""Where a MadMapper project keeps its triggers (cues or timelines), across versions.

- MadMapper 5 (`MM5`): `cueBanksV4.cueBanks[]`, each with a `cues` grid in
  which `{}` is an empty slot. A MIDI trigger targets
  `/cues/<bank>/cues/by_name/<cue>`.
- Early MadMapper 6 (`MM6`), which replaced cues with timelines:
  `timelineBanks.timelineBanks[]`, each with a `timelines` grid of
  `{scene, timeline, uid}` entries (`{}` empty), its own conductor and its own
  `attributeRegistryMap`. A trigger targets
  `/timelines/<bank>/by_name/<timeline>/play_from_beginning`.
- Current MadMapper 6 (`MM6_SHARED`): the `{scene, timeline, uid}` entries,
  conductor included, sit in one list, `timelineBanks.timelines`; a bank's
  grid holds their uids (-1 empty) and the `attributeRegistryMap` is shared by
  all banks. A trigger targets `/show/<bank>/by_name/<timeline>/play_from_beginning`.
  A bank flagged `is_conductor` (e.g. `<bank>_Conductor`) holds only the
  conductor timeline; it is not a trigger bank and is skipped here.
"""
from show_orchestrator.madfile import (
    T_DOUBLE, T_FLOAT, T_INT, T_MAP, T_ULONGLONG, Variant, VariantMap, get, set_value,
)

MM5, MM6, MM6_SHARED = "mm5", "mm6", "mm6-shared"
TRIGGER_URL_PREFIXES = ("/cues/", "/timelines/", "/show/")


def _value(var: Variant | None):
    return None if var is None else var[1]


def trigger_url(layout: str, bank: str, name: str) -> str:
    """The address a MIDI mapping uses to fire the trigger `name` of `bank`."""
    if layout == MM5:
        return f"/cues/{bank}/cues/by_name/{name}"
    root = "show" if layout == MM6_SHARED else "timelines"
    return f"/{root}/{bank}/by_name/{name}/play_from_beginning"


def timeline_transition(duration: float) -> Variant:
    """A MadMapper 6 timeline's transition (fade) settings."""
    return (T_MAP, [
        ("d", (T_DOUBLE, duration, 0)),
        ("tr", (T_MAP, [("tp", (T_INT, 1, 0))], 0)),
    ], 0)


class TriggerBanks:
    """The trigger banks of a loaded project's top-level map."""

    def __init__(self, root: VariantMap) -> None:
        timeline_banks = get(root, "timelineBanks")
        cue_banks = get(root, "cueBanksV4")
        if timeline_banks is not None:
            self.container = timeline_banks[1]
            shared = get(self.container, "timelines")
            self.layout = MM6_SHARED if shared is not None else MM6
            self.shared = shared[1] if shared is not None else []
            all_banks = [b[1] for b in get(self.container, "timelineBanks")[1]]
            # A conductor bank only holds MadMapper's conductor timeline, which is no trigger.
            self.banks = [b for b in all_banks if not _value(get(b, "is_conductor"))]
            self.conductor_banks = [b for b in all_banks if _value(get(b, "is_conductor"))]
            self.active = _value(get(self.container, "activeTimelineBank"))
        elif cue_banks is not None:
            self.container = cue_banks[1]
            self.layout = MM5
            self.shared = []
            self.banks = [b[1] for b in get(self.container, "cueBanks")[1]]
            self.conductor_banks = []
            self.active = _value(get(self.container, "activeCueBank"))
        else:
            self.container, self.layout, self.shared, self.active = [], None, [], None
            self.banks, self.conductor_banks = [], []

    @property
    def is_mm6(self) -> bool:
        return self.layout in (MM6, MM6_SHARED)

    @property
    def kind(self) -> str:
        """What MadMapper calls a trigger in this version."""
        return "timeline" if self.is_mm6 else "cue"

    # -- banks and slots ---------------------------------------------------------

    @staticmethod
    def bank_name(bank: VariantMap) -> str:
        return _value(get(bank, "bank_name"))

    def bank(self, name: str | None = None) -> VariantMap | None:
        """The bank called `name`, or the active (else first) bank when None."""
        wanted = name or self.active
        for bank in self.banks:
            if self.bank_name(bank) == wanted:
                return bank
        if name is None and self.banks:
            return self.banks[0]
        return None

    def slots(self, bank: VariantMap) -> list[Variant]:
        """The bank's grid, row by row."""
        return get(bank, "timelines" if self.is_mm6 else "cues")[1]

    def _shared_entry(self, uid: int) -> Variant | None:
        for entry in self.shared:
            if _value(get(entry[1], "uid")) == uid:
                return entry
        return None

    def entry(self, slot: Variant) -> Variant | None:
        """The cue, or `{scene, timeline, uid}` entry, in a slot; None when empty."""
        if self.layout == MM6_SHARED:
            return None if slot[1] < 0 else self._shared_entry(slot[1])
        return slot if slot[1] else None

    def is_free(self, slot: Variant) -> bool:
        if self.layout == MM6_SHARED:
            return slot[1] < 0  # a uid with no timeline is left alone, not reused
        return not slot[1]

    def name(self, entry: Variant) -> str | None:
        if self.is_mm6:
            timeline = get(entry[1], "timeline")
            return _value(get(timeline[1], "name")) if timeline else None
        return _value(get(entry[1], "name"))

    def triggers(self, bank: VariantMap) -> list[Variant]:
        """The bank's cues or timeline entries, in grid order."""
        return [e for e in (self.entry(slot) for slot in self.slots(bank)) if e is not None]

    def put(self, bank: VariantMap, index: int, entry: Variant) -> None:
        """Place a new cue or timeline entry in slot `index` of `bank`."""
        slots = self.slots(bank)
        if self.layout == MM6_SHARED:
            self.shared.append(entry)
            slots[index] = (slots[index][0], _value(get(entry[1], "uid")), 0)
        else:
            slots[index] = entry

    # -- timelines -----------------------------------------------------------

    def registry(self, bank: VariantMap) -> VariantMap:
        """The map from timeline track ids to the parameters they drive."""
        owner = self.container if self.layout == MM6_SHARED else bank
        registry = get(owner, "attributeRegistryMap")
        if registry is None:
            registry = (T_MAP, [], 0)
            set_value(owner, "attributeRegistryMap", registry)
        return registry[1]

    def uids(self) -> set[int]:
        """Every timeline uid in use, conductors included."""
        if self.layout == MM6_SHARED:
            entries = list(self.shared)
        else:
            entries = []
            for bank in self.banks + self.conductor_banks:
                conductor = get(bank, "conductor")
                entries += self.slots(bank) + ([conductor] if conductor else [])
        uids = set()
        for entry in entries:
            uid = get(entry[1], "uid") if entry[1] else None
            if uid is not None:
                uids.add(uid[1])
        return uids

    # -- fades ---------------------------------------------------------------

    def fade(self, entry: Variant) -> float | None:
        """A trigger's transition time in seconds."""
        if self.is_mm6:
            transition = get(get(entry[1], "timeline")[1], "transition_settings")
            return _value(get(transition[1], "d")) if transition else None
        transition = get(entry[1], "transition_settings")
        return _value(get(transition[1], "duration")) if transition else None

    def set_fade(self, entry: Variant, seconds: float) -> None:
        if self.is_mm6:
            timeline = get(entry[1], "timeline")[1]
            transition = get(timeline, "transition_settings")
            if transition is None:
                set_value(timeline, "transition_settings", timeline_transition(seconds))
            else:
                set_value(transition[1], "d", (T_DOUBLE, float(seconds), 0))
            return
        transition = get(entry[1], "transition_settings")
        if transition is None:
            set_value(entry[1], "transition_settings", (T_MAP, [  # MadMapper 5 key order
                ("type", (T_ULONGLONG, 0, 0)),
                ("params", (T_MAP, [], 0)),
                ("duration", (T_FLOAT, float(seconds), 0)),
            ], 0))
        else:
            set_value(transition[1], "duration", (T_FLOAT, float(seconds), 0))
