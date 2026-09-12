"""
Create silent audio placeholder files for all music tracks.

This is useful for testing when the actual music files are not available.
The silent files are valid audio format and will work with the audio mixing pipeline.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.music_catalog import TRACKS

OUTPUT_DIR = Path(__file__).parent.parent / "music_assets"


def create_silent_audio(filepath: Path, duration_seconds: int = 10) -> bool:
    """Create a valid silent audio file using soundfile."""
    try:
        import numpy as np
        import soundfile as sf

        # Create silent audio (stereo)
        sample_rate = 44100
        num_samples = int(sample_rate * duration_seconds)
        silence = np.zeros((num_samples, 2), dtype=np.float32)

        # Write as WAV (more universally supported than MP3 for creation)
        wav_path = filepath.with_suffix(".wav")
        sf.write(wav_path, silence, sample_rate)

        # If the original request was for MP3, we can convert it
        # But for testing, WAV is fine and more reliable
        if filepath.suffix == ".mp3":
            # For now, just use WAV and rename to .mp3
            # This workaround works for testing purposes when pydub is unavailable
            import shutil

            shutil.move(wav_path, filepath)
            print(
                f"  [OK] Created {filepath.name} ({duration_seconds}s silence as WAV with .mp3 extension)"
            )
        else:
            print(f"  [OK] Created {filepath.name} ({duration_seconds}s silence)")

        return True
    except ImportError:
        print(f"  [FAIL] numpy or soundfile not available for {filepath.name}")
        return False
    except Exception as e:
        print(f"  [FAIL] Could not create {filepath.name}: {e}")
        return False


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Creating {len(TRACKS)} placeholder audio file(s) in {OUTPUT_DIR} ...")
    print()

    success_count = 0
    for track in TRACKS:
        dest = OUTPUT_DIR / track.local_filename

        # Remove existing file if it exists
        if dest.exists():
            dest.unlink()

        # Create 10 seconds of silence for each track
        if create_silent_audio(dest, duration_seconds=10):
            success_count += 1

    print()
    print(f"Created {success_count}/{len(TRACKS)} placeholder files.")

    if success_count == len(TRACKS):
        print("All placeholder files created successfully!")
        return 0
    else:
        print("Some files failed to create.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
