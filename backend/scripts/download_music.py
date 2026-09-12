"""
Download all music tracks listed in app/services/music_catalog.py.

Usage:
    python scripts/download_music.py [--output-dir ./music_assets] [--dry-run]

Tracks are fetched from the Mixkit CDN preview URLs. These are 30-second
preview clips -- good enough for testing the mixing pipeline, but the full
tracks (accessible via each track's page_url on mixkit.co) are better for
production use.

Note: Mixkit's full-quality download requires clicking "Download Free Music"
on each track page. The preview stream used here is publicly accessible and
is the best automated option without browser automation. For production, visit
each track's page_url and download the full MP3 manually to music_assets/.

The Mixkit Free License permits use in apps -- see music_catalog.py for details.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

from app.services.music_catalog import TRACKS, MusicTrack

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; AloftMusicDownloader/0.1; https://github.com/your-repo/aloft)"
    ),
    "Referer": "https://mixkit.co/",
}


async def download_track(
    client: httpx.AsyncClient,
    track: MusicTrack,
    output_dir: Path,
    dry_run: bool,
) -> bool:
    dest = output_dir / track.local_filename

    if dest.exists():
        size = dest.stat().st_size
        print(f"  [SKIP] {track.title!r} -- already exists ({size:,} bytes)")
        return True

    if dry_run:
        print(f"  [DRY ] {track.title!r} would fetch: {track.preview_url}")
        return True

    print(f"  [FETCH] {track.title!r} from {track.preview_url} ...")
    try:
        resp = await client.get(
            track.preview_url, headers=_HEADERS, timeout=30.0, follow_redirects=True
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"  [FAIL] {track.title!r}: {exc}")
        return False

    content_type = resp.headers.get("content-type", "")
    if "audio" not in content_type and "octet-stream" not in content_type:
        print(
            f"  [WARN] {track.title!r}: unexpected content-type {content_type!r} "
            f"-- saving anyway ({len(resp.content):,} bytes)"
        )

    dest.write_bytes(resp.content)
    print(f"  [OK  ] {track.title!r} -> {dest} ({len(resp.content):,} bytes)")
    return True


async def main(output_dir: Path, dry_run: bool) -> int:
    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Downloading {len(TRACKS)} track(s) to {output_dir} ...")
    if dry_run:
        print("  (dry-run mode -- no files written)")
    print()

    all_ok = True
    async with httpx.AsyncClient() as client:
        for track in TRACKS:
            ok = await download_track(client, track, output_dir, dry_run)
            if not ok:
                all_ok = False

    print()
    if all_ok:
        print("Done. Verify files with: python scripts/validate_music.py")
    else:
        print("Some downloads FAILED. See output above.")

    return 0 if all_ok else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download Aloft background music tracks.")
    parser.add_argument(
        "--output-dir",
        default="./music_assets",
        type=Path,
        help="Directory to save MP3 files (default: ./music_assets)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be downloaded without fetching.",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.output_dir, args.dry_run)))
