from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.dependencies import (
    audio_synthesis_rate_limit,
    get_current_user,
    get_database,
    get_http_client,
    mixed_audio_rate_limit,
    require_permission,
)
from app.models.role import Permission
from app.models.user import User
from app.services.audio_mixing import AudioMixingError, mix_narration_with_music
from app.services.audio_repository import get_audio, read_audio_bytes, save_audio
from app.services.audio_service import get_voice_id_for_language, synthesize_story_audio
from app.services.music_asset_repository import MusicAssetError, resolve_music_file
from app.services.music_catalog import ALL_TRACK_IDS, get_track
from app.services.story_repository import get_story

router = APIRouter(prefix="/v1/pois", tags=["audio"])


@router.post(
    "/{source_id}/audio",
    summary="Synthesise narration audio for a POI",
    response_description="MP3 audio bytes",
    responses={
        200: {"content": {"audio/mpeg": {}}, "description": "Narration as MP3 audio"},
        404: {
            "description": "No story found for this POI — generate it first via POST /pois/{source_id}/story"
        },
    },
    dependencies=[
        Depends(audio_synthesis_rate_limit()),
        Depends(require_permission(Permission.CREATE_AUDIO)),
    ],
)
async def create_audio(
    source_id: str,
    language: str = "en",
    voice_id: str | None = None,
    _: User = Depends(get_current_user),
    client: httpx.AsyncClient = Depends(get_http_client),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> Response:
    """Synthesise MP3 narration audio for a POI story using ElevenLabs.

    - The story must exist first — run `POST /pois/{source_id}/story` before this.
    - Audio is cached in R2 (production) or local disk (dev); subsequent calls
      with the same source_id + language + voice_id return the cached audio
      immediately (no ElevenLabs call).
    - `voice_id` overrides the per-language default. Leave blank to use the
      language-appropriate voice (Arabic → Anas, French → Charlotte, others → Rachel).
    - Returns raw MP3 bytes with `Content-Type: audio/mpeg`.
    """
    resolved_voice = voice_id or get_voice_id_for_language(language)

    existing = await get_audio(db, source_id, language, resolved_voice)
    if existing is not None:
        try:
            audio_bytes = await read_audio_bytes(existing)
            return Response(content=audio_bytes, media_type="audio/mpeg")
        except (FileNotFoundError, RuntimeError):
            # Cache record exists but the file is gone (e.g. disk wiped on Render
            # restart before R2 was configured). Fall through to re-synthesise.
            pass

    story = await get_story(db, source_id, language)
    if story is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No story found for source_id '{source_id}' in language '{language}'. "
                f"Generate it first via POST /pois/{source_id}/story."
            ),
        )

    audio_bytes = await synthesize_story_audio(
        story.text_content, language=language, voice_id=resolved_voice, http_client=client
    )
    asset = await save_audio(db, source_id, language, resolved_voice, audio_bytes)
    return Response(content=await read_audio_bytes(asset), media_type="audio/mpeg")


@router.post(
    "/{source_id}/audio/mixed",
    summary="Synthesise narration audio mixed with background music",
    response_description="MP3 audio bytes with background music bed",
    responses={
        200: {"content": {"audio/mpeg": {}}, "description": "Narration mixed with music as MP3"},
        404: {"description": "No story or audio found for this POI, or music track not found"},
        422: {"description": "Audio mixing failed (corrupt audio or music file)"},
    },
    dependencies=[
        Depends(mixed_audio_rate_limit()),
        Depends(require_permission(Permission.CREATE_AUDIO)),
    ],
)
async def create_mixed_audio(
    source_id: str,
    language: str = "en",
    voice_id: str | None = None,
    track_id: str = "mixkit-feedback-dreams",
    _: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> Response:
    """Synthesise narration audio and mix it over a background music track.

    The narration must already exist (run `POST /pois/{source_id}/audio` first).
    Background music is drawn from the built-in Mixkit catalog (all Mixkit Free
    License). Use `track_id` to select a track; defaults to "mixkit-feedback-dreams".

    Music tracks are fetched from R2 (production) or read from local disk (dev)
    automatically. If R2 isn't configured and the file isn't available locally,
    a 404 is returned — configure R2 and run scripts/upload_music_assets_to_r2.py
    to fix this.

    Returns raw MP3 bytes with the narration mixed over a low-volume music bed.
    The mix is NOT cached — call this on-demand for preview; cache the result
    yourself if needed.
    """
    resolved_voice = voice_id or get_voice_id_for_language(language)

    # Fetch existing narration audio (must exist already)
    audio_asset = await get_audio(db, source_id, language, resolved_voice)
    if audio_asset is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No audio found for source_id '{source_id}' in language '{language}' "
                f"with voice '{resolved_voice}'. Generate it first via POST /pois/{source_id}/audio."
            ),
        )

    try:
        narration_bytes = await read_audio_bytes(audio_asset)
    except (FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Audio file for '{source_id}' is missing: {exc}",
        ) from exc

    # Resolve music track
    track = get_track(track_id)
    if track is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Unknown track_id '{track_id}'. Available tracks: "
                f"{', '.join(ALL_TRACK_IDS[:5])}{'...' if len(ALL_TRACK_IDS) > 5 else ''} "
                f"(see music_catalog.py for the full list)."
            ),
        )

    try:
        music_path = await resolve_music_file(track)
    except MusicAssetError as exc:
        raise HTTPException(
            status_code=404,
            detail=str(exc),
        ) from exc

    try:
        mixed_bytes = mix_narration_with_music(narration_bytes, music_path)
    except AudioMixingError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Audio mixing failed: {exc}",
        ) from exc

    return Response(content=mixed_bytes, media_type="audio/mpeg")
