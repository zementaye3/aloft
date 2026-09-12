"""
Verify that all ElevenLabs voice IDs used by the app are still valid and
support eleven_multilingual_v2.

Run from the backend directory:
    python scripts/verify_tts_voices.py

Requires ELEVENLABS_API_KEY in .env (or env). Exits non-zero if any voice
fails -- useful as a pre-deploy sanity check.

What it checks:
  - Each voice ID resolves to a real voice (GET /v1/voices/{voice_id})
  - The voice lists eleven_multilingual_v2 in its high_quality_base_model_ids
  - A short synthesis call succeeds (optional, pass --synth to enable)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Allow running from the backend directory without installing the package.
sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

# Load .env before importing app modules
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass  # python-dotenv not installed -- rely on env being set externally

from app.core.config import get_settings
from app.services.audio_service import _LANGUAGE_VOICE_DEFAULTS

_BASE_URL = "https://api.elevenlabs.io"
_MODEL = "eleven_multilingual_v2"
_TEST_TEXT = "Hello."  # minimal synthesis test -- short to save quota


async def check_voice(
    client: httpx.AsyncClient,
    voice_id: str,
    label: str,
    api_key: str,
    do_synth: bool,
) -> bool:
    """Return True if voice is valid and supports the multilingual model."""
    headers = {"xi-api-key": api_key}
    ok = True

    # 1. Voice metadata
    try:
        resp = await client.get(
            f"{_BASE_URL}/v1/voices/{voice_id}",
            headers=headers,
            timeout=10.0,
        )
    except httpx.RequestError as exc:
        print(f"  [{label}] NETWORK ERROR: {exc}")
        return False

    if resp.status_code == 404:
        print(
            f"  [{label}] FAIL  voice_id={voice_id!r} -- 404 NOT FOUND (voice deleted or ID wrong)"
        )
        return False
    if resp.status_code == 401:
        print(f"  [{label}] FAIL  voice_id={voice_id!r} -- 401 UNAUTHORIZED (bad API key)")
        return False
    if resp.status_code != 200:
        print(f"  [{label}] FAIL  voice_id={voice_id!r} -- HTTP {resp.status_code}")
        return False

    data = resp.json()
    name = data.get("name", "?")
    model_ids: list[str] = data.get("high_quality_base_model_ids", [])

    if _MODEL not in model_ids:
        print(
            f"  [{label}] WARN  voice_id={voice_id!r} name={name!r} -- "
            f"eleven_multilingual_v2 NOT in high_quality_base_model_ids: {model_ids}"
        )
        ok = False
    else:
        print(f"  [{label}] OK    voice_id={voice_id!r} name={name!r} -- supports {_MODEL}")

    # 2. Optional synthesis check
    if do_synth and ok:
        synth_resp = await client.post(
            f"{_BASE_URL}/v1/text-to-speech/{voice_id}",
            headers={**headers, "Content-Type": "application/json", "Accept": "audio/mpeg"},
            json={"text": _TEST_TEXT, "model_id": _MODEL, "output_format": "mp3_44100_128"},
            timeout=30.0,
        )
        if synth_resp.status_code == 200 and len(synth_resp.content) > 0:
            print(f"  [{label}] OK    synthesis succeeded ({len(synth_resp.content)} bytes)")
        else:
            print(
                f"  [{label}] FAIL  synthesis: HTTP {synth_resp.status_code}: "
                f"{synth_resp.text[:200]}"
            )
            ok = False

    return ok


async def main(do_synth: bool) -> int:
    settings = get_settings()
    api_key = settings.elevenlabs_api_key
    if api_key is None:
        print("ERROR: ELEVENLABS_API_KEY is not set. Add it to .env or set the env var.")
        return 1
    raw_key = api_key.get_secret_value()

    # Build the full set of voice IDs to check:
    # 1. Global default
    # 2. All per-language defaults
    # 3. Any ELEVENLABS_VOICE_ID_XX env var overrides
    voices_to_check: dict[str, str] = {
        "global default (Rachel)": settings.elevenlabs_voice_id,
    }
    for lang, vid in _LANGUAGE_VOICE_DEFAULTS.items():
        voices_to_check[f"lang={lang} default"] = vid

    # Check for per-language env overrides
    for lang in ["ar", "fr", "en", "de", "es", "zh", "hi", "am"]:
        env_key = f"ELEVENLABS_VOICE_ID_{lang.upper()}"
        override = os.environ.get(env_key)
        if override:
            voices_to_check[f"lang={lang} env override ({env_key})"] = override

    print(f"Checking {len(voices_to_check)} voice(s) against {_BASE_URL} ...")
    if do_synth:
        print("  (synthesis test enabled -- will consume a few characters of quota)")
    print()

    all_ok = True
    async with httpx.AsyncClient() as client:
        for label, vid in voices_to_check.items():
            result = await check_voice(client, vid, label, raw_key, do_synth)
            if not result:
                all_ok = False

    print()
    if all_ok:
        print("All voices OK.")
        return 0
    else:
        print("One or more voices FAILED. See output above.")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verify ElevenLabs voice IDs for the aloft app.")
    parser.add_argument(
        "--synth",
        action="store_true",
        help="Also run a short synthesis call for each voice (uses quota).",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main(do_synth=args.synth)))
