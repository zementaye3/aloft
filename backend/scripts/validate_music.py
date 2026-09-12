"""
Validate downloaded music tracks for the Aloft mixing pipeline.

Usage:
    python scripts/validate_music.py [--music-dir ./music_assets]

Checks:
  1. File exists and is non-empty.
  2. soundfile can decode the file without error.
  3. Duration is at least 10s (catches truncated downloads).
  4. Audio has at least one channel (catches corrupt/silent-only files).

Exits non-zero if any track fails.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

from app.services.music_catalog import TRACKS, MusicTrack

_MIN_DURATION_S = 10  # 10 seconds minimum


def validate_track(track: MusicTrack, music_dir: Path) -> bool:
    path = music_dir / track.local_filename

    # 1. File existence
    if not path.exists():
        print(
            f"  [FAIL] {track.title!r}: file not found at {path}\n"
            f"         Run: python scripts/download_music.py"
        )
        return False

    size = path.stat().st_size
    if size == 0:
        print(f"  [FAIL] {track.title!r}: file is empty ({path})")
        return False

    # 2. Decode with soundfile
    try:
        import soundfile as sf

        audio, sample_rate = sf.read(str(path))
    except Exception as exc:
        print(f"  [FAIL] {track.title!r}: soundfile could not decode: {exc}")
        return False

    # 3. Duration check
    duration_s = len(audio) / sample_rate
    if duration_s < _MIN_DURATION_S:
        print(
            f"  [FAIL] {track.title!r}: too short ({duration_s:.1f}s < "
            f"{_MIN_DURATION_S}s). File may be truncated."
        )
        return False

    # 4. Channel check
    num_channels = 1 if audio.ndim == 1 else audio.shape[1]

    if num_channels < 1:
        print(f"  [FAIL] {track.title!r}: audio has no channels")
        return False

    print(
        f"  [OK  ] {track.title!r} -- {duration_s:.1f}s, "
        f"{num_channels}ch, {sample_rate}Hz, {size:,} bytes"
    )
    return True


def main(music_dir: Path) -> int:
    try:
        import soundfile as sf  # noqa: F401
    except ImportError:
        print("ERROR: soundfile is not installed. Run: pip install soundfile")
        return 1

    print(f"Validating {len(TRACKS)} track(s) in {music_dir} ...")
    print()

    all_ok = True
    for track in TRACKS:
        if not validate_track(track, music_dir):
            all_ok = False

    print()
    if all_ok:
        print("All tracks valid.")
        return 0
    else:
        print("One or more tracks FAILED validation. See output above.")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Validate Aloft background music files.")
    parser.add_argument(
        "--music-dir",
        default="./music_assets",
        type=Path,
        help="Directory containing downloaded MP3 files (default: ./music_assets)",
    )
    args = parser.parse_args()
    sys.exit(main(args.music_dir))
