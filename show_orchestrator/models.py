import re
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, BaseModel

MMSS_PATTERN = re.compile(r"^\d{1,2}:\d{2}(\.\d+)?$")


def to_seconds(value: float | str) -> float:
    """Convert a 'MM:SS(.sss)' string or a plain number of seconds to seconds."""
    if isinstance(value, str):
        minutes, seconds = map(float, value.split(":"))
        return minutes * 60 + seconds
    return float(value)


def validate_timestamp_value(value: float | str) -> float | str:
    if isinstance(value, str) and not MMSS_PATTERN.match(value):
        raise ValueError("Timestamp must be in the format 'MM:SS' or 'MM:SS.sss'")
    if isinstance(value, (int, float)) and value < 0:
        raise ValueError("Timestamp must be non-negative")
    return value


def validate_duration_value(value: float | str) -> float | str:
    if isinstance(value, str) and not MMSS_PATTERN.match(value):
        try:
            value = float(value)
        except ValueError:
            raise ValueError("Duration must be a valid number")
    if isinstance(value, (int, float)) and value < 0:
        raise ValueError("Duration must be non-negative")
    return value


Timestamp = Annotated[float | str, AfterValidator(validate_timestamp_value)]
Duration = Annotated[float | str, AfterValidator(validate_duration_value)]


class EffectType(StrEnum):
    LIGHTS = "lights"
    PROJECTION = "projection"
    HOMEASSISTANT = "homeassistant"


class MidiEvent(BaseModel):
    timestamp: float
    message: str
    channel: int
    note: int
    label: str | None = None


class Event(BaseModel):
    timestamp: Timestamp
    effect_id: str
    duration: Duration | None = None

    @property
    def timestamp_seconds(self) -> float:
        return to_seconds(self.timestamp)

    @property
    def duration_seconds(self) -> float | None:
        if self.duration is None:
            return None
        return to_seconds(self.duration)


class ExtraAudioTrack(BaseModel):
    name: str
    file_path: str
    duration: Duration
    timestamp: Timestamp

    @property
    def timestamp_seconds(self) -> float:
        return to_seconds(self.timestamp)

    @property
    def duration_seconds(self) -> float:
        return to_seconds(self.duration)


class AudioTrack(BaseModel):
    name: str
    events: dict[EffectType, list[Event]]
    extra_tracks: list[ExtraAudioTrack] | None = None
    duration: Duration
    file_path: str | None = None

    @property
    def duration_seconds(self) -> float:
        return to_seconds(self.duration)


class Effect(BaseModel):
    id: str
    name: str
    note: int | None = None
    channel: int | None = None


class Show(BaseModel):
    audio_tracks: list[AudioTrack]
    effects: dict[EffectType, list[Effect]]
