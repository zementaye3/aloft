"""
Licensed background music track catalog for Aloft.

All tracks are sourced from various free music sources under permissive licenses:
- CC0 (Public Domain) - No attribution required, commercial use permitted
- Free use licenses - Commercial use permitted, no attribution required

DOWNLOADING TRACKS
==================
Tracks are not bundled with this repo. Download them once and store locally
(default path: ./music_assets/).

Current tracks are from:
- OpenGameArt.org (CC0/Public Domain)
- dansbits.com (Free use license)

ADDING / UPDATING TRACKS
=========================
1. Find atmospheric, cinematic, ambient tracks suitable for flight narration
2. Ensure tracks have permissive licenses (CC0, free commercial use)
3. Add an entry here following the existing format
4. Download the file to ./music_assets/ with the specified filename
5. Run scripts/validate_music.py to confirm all files are valid audio

TRACK SELECTION CRITERIA
=========================
Chosen for suitability as a background narration bed:
  - Slow tempo (avoids competing with speech rhythm)
  - Minimal or no vocals
  - Atmospheric / cinematic mood (appropriate for looking at landscape below)
  - Duration ≥ 90s (long enough to be looped for typical POI stories)
  - Not culturally region-specific where possible (global playlist)
  - Permissive licensing for commercial use
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MusicTrack:
    track_id: str  # Unique identifier for this catalog
    title: str
    artist: str
    duration_seconds: int  # Approximate -- Mixkit rounds to seconds
    tags: list[str]
    page_url: str  # Mixkit page to download from
    preview_url: str  # CDN stream URL (30s preview only, not full track)
    local_filename: str  # Expected filename in ./music_assets/


# ---------------------------------------------------------------------------
# Curated track list -- CC0 and free-use music for Aloft
# ---------------------------------------------------------------------------
TRACKS: list[MusicTrack] = [
    MusicTrack(
        track_id="first-light-particles",
        title="First Light Particles",
        artist="Yoiyami",
        duration_seconds=180,
        tags=["atmospheric", "piano", "cinematic", "peaceful", "space"],
        page_url="https://opengameart.org/content/first-light-particles-%E2%80%93-cc0-atmospheric-pianoambient-track",
        preview_url="https://opengameart.org/sites/default/files/first_light_particles_0.wav",
        local_filename="first_light_particles.wav",
    ),
    MusicTrack(
        track_id="steller-dreams",
        title="Steller Dreams",
        artist="Synth-thetic",
        duration_seconds=240,
        tags=["atmospheric", "synth", "cosmic", "space", "meditative"],
        page_url="https://opengameart.org/content/steller-dreams",
        preview_url="https://opengameart.org/sites/default/files/steller_dreams.flac",
        local_filename="steller_dreams.flac",
    ),
    MusicTrack(
        track_id="yoiyami-core-theme",
        title="Yoiyami Core Theme",
        artist="Yoiyami",
        duration_seconds=200,
        tags=["atmospheric", "piano", "emotional", "cinematic", "serene"],
        page_url="https://opengameart.org/content/yoiyami-core-theme-%E2%80%93-deep-blue-ambient-piano",
        preview_url="https://opengameart.org/sites/default/files/yoiyami_core_theme_0.wav",
        local_filename="yoiyami_core_theme.wav",
    ),
    MusicTrack(
        track_id="dusk",
        title="Dusk",
        artist="dansbits",
        duration_seconds=180,
        tags=["atmospheric", "synth", "relaxed", "chillout", "background"],
        page_url="https://dansbits.com/dusk/",
        preview_url="https://dansbits.com/wp-content/uploads/2026/01/dansbits-dusk.mp3",
        local_filename="dusk.mp3",
    ),
]

# Alias the full list for convenience
ALL_TRACK_IDS = [t.track_id for t in TRACKS]


def get_track(track_id: str) -> MusicTrack | None:
    """Look up a track by its catalog track_id. Returns None if not found."""
    for track in TRACKS:
        if track.track_id == track_id:
            return track
    return None


def tracks_by_tag(*tags: str) -> list[MusicTrack]:
    """Return tracks that have ALL the given tags."""
    tag_set = set(tags)
    return [t for t in TRACKS if tag_set.issubset(set(t.tags))]
