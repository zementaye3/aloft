"""
One-time upload of the real music_assets/ files to Cloudflare R2.

Why this exists
================
The 4 files in music_assets/ (~109MB total) were previously committed
directly to git, which is why the repo/clone size ballooned. They've now
been removed from git tracking (see .gitignore) and the Dockerfile no
longer COPYs them in.

scripts/download_music.py fetches only 30-second Mixkit *preview* clips --
by its own docstring, not production-quality. It cannot regenerate the real
files that were previously committed. So: run this script ONCE, locally,
using the real files still sitting in your local music_assets/ directory
(git rm --cached does not delete working-tree files, only untracks them),
to upload them to R2. After that, the app fetches them from R2 on first
use and caches them locally -- see app/services/music_asset_repository.py.

Usage:
    cd backend
    python scripts/upload_music_assets_to_r2.py

Requires R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY,
R2_BUCKET_NAME to be set in your .env (the same R2 credentials already
used for generated narration audio).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import get_settings
from app.services.music_catalog import TRACKS

R2_MUSIC_PREFIX = "static/music"


def main() -> int:
    settings = get_settings()
    if not settings.r2_configured:
        print(
            "R2 is not configured. Set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, "
            "R2_SECRET_ACCESS_KEY, and R2_BUCKET_NAME in your .env before running this."
        )
        return 1

    client = boto3.client(
        "s3",
        endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key.get_secret_value(),
        region_name="auto",
    )

    music_dir = Path(__file__).parent.parent / "music_assets"
    all_ok = True

    for track in TRACKS:
        local_path = music_dir / track.local_filename
        if not local_path.exists():
            print(f"  [SKIP] {track.local_filename} -- not found locally at {local_path}")
            all_ok = False
            continue

        key = f"{R2_MUSIC_PREFIX}/{track.local_filename}"
        size = local_path.stat().st_size
        print(f"  [UPLOAD] {track.local_filename} ({size:,} bytes) -> r2:{key}")
        try:
            client.upload_file(str(local_path), settings.r2_bucket_name, key)
        except (BotoCoreError, ClientError) as exc:
            print(f"  [FAIL] {track.local_filename}: {exc}")
            all_ok = False

    print()
    if all_ok:
        print("Done. All tracks uploaded to R2. The app will fetch them from there at runtime.")
    else:
        print("Some uploads failed or files were missing locally -- see above.")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
