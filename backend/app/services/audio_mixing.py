from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

_MUSIC_VOLUME_REDUCTION_DB = 18.0
_FADE_MS = 1500
_MUSIC_VOLUME_RATIO = 0.1  # -18dB ≈ 0.125, using 0.1 for safety


class AudioMixingError(Exception):
    pass


def mix_narration_with_music(narration_bytes: bytes, music_file_path: str) -> bytes:
    """Mix narration over a background music bed.

    Music is lowered 18dB, faded in/out, and looped or trimmed to match
    narration duration. Output format matches narration input format.

    Raises AudioMixingError if either input can't be decoded.
    """
    return _mix_with_numpy(narration_bytes, music_file_path)


def _mix_with_numpy(narration_bytes: bytes, music_file_path: str) -> bytes:
    """Mix audio using numpy + soundfile."""
    try:
        # Detect input format
        fmt = _detect_format(narration_bytes)

        # Load narration audio
        with tempfile.NamedTemporaryFile(suffix=f".{fmt}", delete=False) as temp_narration:
            temp_narration.write(narration_bytes)
            temp_narration_path = temp_narration.name

        try:
            narration_audio, sr = sf.read(temp_narration_path, always_2d=True)
        finally:
            Path(temp_narration_path).unlink(missing_ok=True)

        # Load music audio
        try:
            music_audio, music_sr = sf.read(music_file_path, always_2d=True)
        except Exception as exc:
            raise AudioMixingError(f"Could not read music file '{music_file_path}': {exc}") from exc

        # Resample music to match narration if needed (simple linear interpolation)
        if music_sr != sr:
            num_samples = int(len(music_audio) * sr / music_sr)
            indices = np.linspace(0, len(music_audio) - 1, num_samples)
            music_audio = music_audio[np.clip(indices.astype(int), 0, len(music_audio) - 1)]

        # Reduce music volume and fit to narration duration
        music_audio = _fit_music_to_narration(music_audio, len(narration_audio))

        # Apply fade in/out to music
        music_audio = _apply_fade(music_audio, _FADE_MS, sr)

        # Mix narration with music (narration takes priority)
        mixed_audio = narration_audio + music_audio

        # Normalize to prevent clipping
        max_val = np.max(np.abs(mixed_audio))
        if max_val > 0.95:
            mixed_audio = mixed_audio / max_val * 0.95

        # Export to bytes
        with tempfile.NamedTemporaryFile(suffix=f".{fmt}", delete=False) as temp_output:
            temp_output_path = temp_output.name

        try:
            sf.write(temp_output_path, mixed_audio, sr)
            with open(temp_output_path, "rb") as f:
                return f.read()
        finally:
            Path(temp_output_path).unlink(missing_ok=True)

    except Exception as exc:
        raise AudioMixingError(f"Audio mixing failed: {exc}") from exc


def _fit_music_to_narration(music: np.ndarray, narration_length: int) -> np.ndarray:
    """Fit music to narration duration by looping or trimming."""
    if len(music) == 0:
        return np.zeros((narration_length, music.shape[1]))

    if len(music) >= narration_length:
        return music[:narration_length] * _MUSIC_VOLUME_RATIO

    # Loop music to match narration length
    looped = music.copy()
    while len(looped) < narration_length:
        looped = np.concatenate([looped, music])

    return looped[:narration_length] * _MUSIC_VOLUME_RATIO


def _apply_fade(audio: np.ndarray, fade_ms: int, sample_rate: int) -> np.ndarray:
    """Apply fade in/out to audio."""
    fade_samples = int(fade_ms * sample_rate / 1000)

    if len(audio) < 2 * fade_samples:
        # Audio too short for full fade, just apply half fade
        fade_samples = len(audio) // 2

    if fade_samples <= 0:
        return audio

    # Fade in
    fade_in_curve = np.linspace(0, 1, fade_samples)
    audio[:fade_samples] *= fade_in_curve[:, np.newaxis]

    # Fade out
    fade_out_curve = np.linspace(1, 0, fade_samples)
    audio[-fade_samples:] *= fade_out_curve[:, np.newaxis]

    return audio


def _detect_format(data: bytes) -> str:
    """Detect audio format from magic bytes.

    Returns the file extension to use for temporary files during mixing.
    soundfile can handle WAV, FLAC, OGG, and most other formats natively.
    MP3 is the default fallback since ElevenLabs outputs MP3.
    """
    if data[:4] == b"RIFF":
        return "wav"
    if data[:4] == b"fLaC":
        return "flac"
    if data[:4] == b"OggS":
        return "ogg"
    return "mp3"
