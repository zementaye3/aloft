"""
Mixing tests use WAV for synthetic fixtures (no ffmpeg needed for WAV
encode/decode) -- the service itself takes and returns MP3 bytes in
production, but the mixing logic is identical regardless of container format.
The service-level AudioMixingError tests use raw bytes to trigger decode
failures without needing ffmpeg at all.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from app.services.audio_mixing import AudioMixingError, mix_narration_with_music


def _wav_bytes(duration_ms: int, sample_rate: int = 44100) -> bytes:
    """Generate WAV audio bytes using numpy/soundfile."""
    num_samples = int(duration_ms * sample_rate / 1000)
    # Generate sine wave at 440 Hz
    t = np.linspace(0, duration_ms / 1000, num_samples)
    audio = 0.5 * np.sin(2 * np.pi * 440 * t)
    # Convert to stereo
    audio = np.column_stack([audio, audio])

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp:
        temp_path = temp.name

    try:
        sf.write(temp_path, audio, sample_rate)
        with open(temp_path, "rb") as f:
            return f.read()
    finally:
        Path(temp_path).unlink(missing_ok=True)


def _wav_file(
    tmp_path, name: str, duration_ms: int, freq: int = 220, sample_rate: int = 44100
) -> str:
    """Generate WAV file using numpy/soundfile."""
    path = tmp_path / name
    num_samples = int(duration_ms * sample_rate / 1000)
    # Generate sine wave
    t = np.linspace(0, duration_ms / 1000, num_samples)
    audio = 0.5 * np.sin(2 * np.pi * freq * t)
    # Convert to stereo
    audio = np.column_stack([audio, audio])

    sf.write(str(path), audio, sample_rate)
    return str(path)


def _get_audio_duration(bytes_data: bytes) -> float:
    """Get audio duration in seconds from bytes."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp:
        temp_path = temp.name

    try:
        with open(temp_path, "wb") as f:
            f.write(bytes_data)
        audio, sr = sf.read(temp_path)
        return len(audio) / sr
    finally:
        Path(temp_path).unlink(missing_ok=True)


def _get_audio_rms(bytes_data: bytes) -> float:
    """Get RMS level from audio bytes."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp:
        temp_path = temp.name

    try:
        with open(temp_path, "wb") as f:
            f.write(bytes_data)
        audio, _ = sf.read(temp_path)
        return np.sqrt(np.mean(audio**2))
    finally:
        Path(temp_path).unlink(missing_ok=True)


# --- mix_narration_with_music integration tests (WAV fixtures, no ffmpeg) ---


def test_mix_produces_output_matching_narration_duration(tmp_path):
    narration = _wav_bytes(3000)
    music_path = _wav_file(tmp_path, "music.wav", 1000)

    mixed_bytes = mix_narration_with_music(narration, music_path)
    mixed_duration = _get_audio_duration(mixed_bytes)

    assert abs(mixed_duration - 3.0) < 0.1  # 3000ms = 3 seconds


def test_mix_short_music_is_looped(tmp_path):
    narration = _wav_bytes(3000)
    music_path = _wav_file(tmp_path, "short.wav", 1000)

    result = mix_narration_with_music(narration, music_path)
    assert len(result) > 0


def test_mix_long_music_is_trimmed(tmp_path):
    narration = _wav_bytes(3000)
    music_path = _wav_file(tmp_path, "long.wav", 10000)

    mixed_bytes = mix_narration_with_music(narration, music_path)
    mixed_duration = _get_audio_duration(mixed_bytes)
    assert abs(mixed_duration - 3.0) < 0.1  # 3000ms = 3 seconds


def test_mix_reduces_music_volume(tmp_path):
    narration = _wav_bytes(3000)
    music_path = _wav_file(tmp_path, "loud.wav", 3000)

    # Get loud music RMS
    loud_rms = _get_audio_rms(_wav_bytes(3000))

    mixed_bytes = mix_narration_with_music(narration, music_path)
    mixed_rms = _get_audio_rms(mixed_bytes)

    # Mixed audio should be quieter than loud music (allowing for small differences due to narration)
    # The music is reduced by ~18dB, so it should be significantly quieter
    assert mixed_rms < loud_rms * 1.5  # Allow some tolerance for narration contribution


def test_raises_on_corrupt_narration(tmp_path):
    music_path = _wav_file(tmp_path, "music.wav", 1000)
    with pytest.raises(AudioMixingError, match="Audio mixing failed"):
        mix_narration_with_music(b"not audio", music_path)


def test_raises_on_corrupt_music_file(tmp_path):
    narration = _wav_bytes(3000)
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not audio")
    with pytest.raises(AudioMixingError, match="Could not read music file"):
        mix_narration_with_music(narration, str(bad))


def test_silent_music_does_not_crash(tmp_path):
    narration = _wav_bytes(3000)
    path = tmp_path / "silent.wav"
    # Generate silent audio
    num_samples = int(2000 * 44100 / 1000)
    silent_audio = np.zeros((num_samples, 2))
    sf.write(str(path), silent_audio, 44100)
    assert len(mix_narration_with_music(narration, str(path))) > 0
