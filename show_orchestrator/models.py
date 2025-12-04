import re
from enum import StrEnum

from pydantic import BaseModel, field_validator


class EffectType(StrEnum):
    LIGHTS = "lights"
    PROJECTION = "projection"
    HOMEASSISTANT = "homeassistant"


class MidiEvent(BaseModel):
    timestamp: float
    message: str
    channel: int
    note: int


class Event(BaseModel):
    timestamp: float | str
    effect_id: str
    duration: float | str | None = None

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, value: float | str) -> float | str:
        if isinstance(value, str) and not re.match(r"^\d{1,2}:\d{2}(\.\d+)?$", value):
            raise ValueError("Timestamp must be in the format 'MM:SS' or 'MM:SS.sss'")
        if isinstance(value, (int, float)) and value < 0:
            raise ValueError("Timestamp must be non-negative")
        return value    
    
    @field_validator("duration")
    @classmethod
    def validate_duration(cls, value: float | str | None) -> float | str | None:
        if isinstance(value, str) and not re.match(r"^\d{1,2}:\d{2}(\.\d+)?$", value):
            try:
                return float(value)
            except:
                raise ValueError("Duration must be a valid number")
        if isinstance(value, (int, float)) and value < 0:
            raise ValueError("Duration must be non-negative")
        return value
    
    @property
    def timestamp_seconds(self) -> float:
        if isinstance(self.timestamp, str):
            minutes, seconds = map(float, self.timestamp.split(':'))
            return minutes * 60 + seconds
        return float(self.timestamp)
    
    @property
    def duration_seconds(self) -> float | None:
        if self.duration is None:
            return None
        if isinstance(self.duration, str):
            minutes, seconds = map(float, self.duration.split(':'))
            return minutes * 60 + seconds
        return float(self.duration)


class ExtraAudioTrack(BaseModel):
    name: str
    file_path: str
    duration: float | str
    timestamp: float | str

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, value: float | str) -> float | str:
        if isinstance(value, str) and not re.match(r"^\d{1,2}:\d{2}(\.\d+)?$", value):
            raise ValueError("Timestamp must be in the format 'MM:SS' or 'MM:SS.sss'")
        if isinstance(value, (int, float)) and value < 0:
            raise ValueError("Timestamp must be non-negative")
        return value    
    
    @field_validator("duration")
    @classmethod
    def validate_duration(cls, value: float | str | None) -> float | str | None:
        if isinstance(value, str) and not re.match(r"^\d{1,2}:\d{2}(\.\d+)?$", value):
            try:
                return float(value)
            except:
                raise ValueError("Duration must be a valid number")
        if isinstance(value, (int, float)) and value < 0:
            raise ValueError("Duration must be non-negative")
        return value
    
    @property
    def timestamp_seconds(self) -> float:
        if isinstance(self.timestamp, str):
            minutes, seconds = map(float, self.timestamp.split(':'))
            return minutes * 60 + seconds
        return float(self.timestamp)
    
    @property
    def duration_seconds(self) -> float:
        if isinstance(self.duration, str):
            minutes, seconds = map(float, self.duration.split(':'))
            return minutes * 60 + seconds
        return float(self.duration)


class AudioTrack(BaseModel):
    name: str
    events: dict[EffectType, list[Event]]
    extra_tracks: list[ExtraAudioTrack] | None = None
    duration: float | str
    file_path: str | None = None

    @field_validator("duration")
    @classmethod
    def validate_duration(cls, value: float | str) -> float | str:
        if isinstance(value, str) and not re.match(r"^\d{1,2}:\d{2}(\.\d+)?$", value):
            try:
                return float(value)
            except:
                raise ValueError("Duration must be a valid number")
        if isinstance(value, (int, float)) and value < 0:
            raise ValueError("Duration must be non-negative")
        return value
    
    @property
    def duration_seconds(self) -> float:
        if isinstance(self.duration, str):
            minutes, seconds = map(float, self.duration.split(':'))
            return minutes * 60 + seconds
        return float(self.duration)


class Effect(BaseModel):
    id: str
    name: str
    note: int | None = None
    channel: int | None = None


class Show(BaseModel):
    audio_tracks: list[AudioTrack]
    effects: dict[EffectType, list[Effect]]