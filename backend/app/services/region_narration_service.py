"""
Generates contextual narration for when the plane is over an ocean,
remote wilderness, or any area with no nearby Wikipedia POIs.

Unlike POI narrations (cached forever per poi_source_id), region
narrations are generated on demand but rate-limited per session --
you don't want "you're over the Atlantic" repeating every 10 seconds.
The session tracks the last time a region narration fired and suppresses
repeats within a cooldown window.
"""

from __future__ import annotations

import logging

import httpx

from app.clients.geocoding_client import RegionInfo, reverse_geocode
from app.clients.groq import chat_completion
from app.services.story_service import _LANGUAGE_NAMES

logger = logging.getLogger("aloft.services.region_narration")

# One region narration per session per this many minutes maximum.
# Long enough that it doesn't feel repetitive, short enough that a
# 7-hour transatlantic flight gets a few interesting moments.
REGION_NARRATION_COOLDOWN_MINUTES = 45

OCEAN_FACTS = {
    "North Atlantic Ocean": (
        "The North Atlantic is one of the world's busiest air corridors -- "
        "more than 600 flights cross it every single day. Below you, the "
        "ocean reaches depths of over 8,000 metres, hiding the Mid-Atlantic "
        "Ridge, a mountain range longer than the Andes."
    ),
    "South Atlantic Ocean": (
        "The South Atlantic is one of the least-trafficked oceans on Earth. "
        "Its isolation made it a graveyard for ships during the age of sail "
        "and a strategic battleground during the Second World War."
    ),
    "North Pacific Ocean": (
        "The Pacific Ocean covers more area than all of Earth's landmasses "
        "combined. The route you're flying was impossible before 1976, when "
        "the first nonstop transpacific commercial flights began."
    ),
    "Indian Ocean": (
        "The Indian Ocean is the world's warmest ocean and home to some of "
        "the most complex weather systems on Earth. Ancient Arab and Indian "
        "traders used its predictable monsoon winds to navigate for millennia."
    ),
    "Arctic Ocean": (
        "Polar routes like this one shave hours off long-haul flights by "
        "flying over the top of the Earth rather than across it. You're "
        "currently above one of the most sparsely populated regions on the planet."
    ),
}


async def generate_region_narration(
    client: httpx.AsyncClient,
    lat: float,
    lng: float,
    language: str = "en",
) -> str:
    """Generate a short contextual narration for the current region.

    Returns the narration text directly (not a Story object -- region
    narrations aren't persisted, they're generated fresh per session
    within the cooldown window).
    """
    region = await reverse_geocode(client, lat, lng)
    return await _generate_text(client, region, language)


async def _generate_text(
    client: httpx.AsyncClient,
    region: RegionInfo,
    language: str,
) -> str:
    language_name = _LANGUAGE_NAMES.get(language, language)

    # Check curated facts first -- faster and cheaper than Groq
    for ocean_name, fact_text in OCEAN_FACTS.items():
        if ocean_name.lower() in region.description.lower():
            if language == "en":
                return fact_text
            # For other languages, translate via Groq
            translation = await chat_completion(
                client,
                messages=[
                    {
                        "role": "system",
                        "content": f"Translate the following to {language_name}. Return only the translation, no preamble.",
                    },
                    {"role": "user", "content": fact_text},
                ],
                temperature=0.3,
                max_tokens=200,
            )
            return translation

    # No curated fact -- generate one with Groq
    if region.is_ocean:
        subject = f"the {region.description}"
        context = "You're narrating for passengers on a flight over this body of water."
    else:
        subject = region.description
        context = "You're narrating for passengers on a flight currently passing over this region."

    messages = [
        {
            "role": "system",
            "content": (
                f"{context} Write 2 sentences that are genuinely interesting -- "
                f"a surprising fact, a historical moment, or what makes this "
                f"part of the world unusual. Warm and documentary in tone. "
                f"Write entirely in {language_name}."
            ),
        },
        {"role": "user", "content": f"Current location: {subject}"},
    ]

    return await chat_completion(client, messages, temperature=0.8, max_tokens=120)
