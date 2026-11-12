"""Find where a song's audio becomes audible, to time fades to its silences.

Decoding uses miniaudio (MP3, WAV, FLAC, Ogg Vorbis; it reads the content, so
an MP3 named `.mpeg` works too) and numpy; both are imported on first use, so
the rest of show-orchestrator runs without them.
"""
from functools import lru_cache
from pathlib import Path

DEFAULT_SILENCE_DB = -40.0  # audio quieter than this, in dBFS, counts as silence
WINDOW_SECONDS = 0.02  # loudness is measured over windows this long
SAMPLE_RATE = 44100


class AudioError(ValueError):
    pass


@lru_cache(maxsize=16)
def loudness(path: Path):
    """RMS loudness in dBFS of each WINDOW_SECONDS window of a file, mixed to mono."""
    try:
        import miniaudio
        import numpy as np
    except ImportError as error:
        raise AudioError(
            f"analysing audio needs {error.name}; run: pip install -r requirements.txt"
        ) from error
    try:
        sound = miniaudio.decode(
            Path(path).read_bytes(), output_format=miniaudio.SampleFormat.FLOAT32,
            nchannels=1, sample_rate=SAMPLE_RATE,
        )
    except miniaudio.DecodeError as error:
        raise AudioError(f"can't decode {path} (MP3, WAV, FLAC and Ogg Vorbis are supported)") from error
    except OSError as error:
        raise AudioError(f"can't read {path}: {error}") from error
    samples = np.asarray(sound.samples, dtype=np.float32)
    window = int(WINDOW_SECONDS * SAMPLE_RATE)
    frames = samples[: len(samples) // window * window].reshape(-1, window)
    return 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-12)


def audible_from(path: Path, after: float = 0.0, threshold_db: float = DEFAULT_SILENCE_DB) -> float | None:
    """Seconds into the file at which, from `after` on, the audio first rises above
    `threshold_db`; None if it never does."""
    levels = loudness(Path(path))
    start = max(0, int(after / WINDOW_SECONDS))
    for i in range(start, len(levels)):
        if levels[i] > threshold_db:
            return max(after, i * WINDOW_SECONDS)
    return None


def silence_after(path: Path, at: float = 0.0, threshold_db: float = DEFAULT_SILENCE_DB) -> float:
    """How long the audio stays silent from `at` seconds into the file (0 if it isn't)."""
    audible = audible_from(path, at, threshold_db)
    if audible is None:
        return max(0.0, len(loudness(Path(path))) * WINDOW_SECONDS - at)
    return audible - at
