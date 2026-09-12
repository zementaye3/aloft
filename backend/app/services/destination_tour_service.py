"""
Generates a curated 'destination preview' for the arrival country --
a set of 20-25 famous places, cultural facts, and highlights about
where the flight is headed.

This fires when:
1. The plane is over ocean/remote area (no nearby POIs)
2. The upcoming POI teaser cooldown hasn't triggered yet
3. The destination tour hasn't been fully played yet

Think of it as a travel documentary playing during the empty stretch
of a long-haul flight: "While you cross the Atlantic, here's what
awaits you in the United States..."
"""

from __future__ import annotations

import json
import logging

import httpx
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.clients.groq import chat_completion
from app.clients.wikipedia import get_summary
from app.core.config import get_settings
from app.services.story_service import _LANGUAGE_NAMES

logger = logging.getLogger("aloft.services.destination_tour")


async def prepare_destination_tour(
    client: httpx.AsyncClient,
    db: AsyncIOMotorDatabase,
    arrival_iata: str,
    arrival_country: str,
    arrival_city: str,
    language: str = "en",
) -> list[str]:
    """
    Generate a list of 20 short narrations about the destination.
    Called once when a session starts, stored on the session.

    Returns a list of pre-generated narration texts, ready to play
    one by one during ocean crossings.
    """
    settings = get_settings()
    highlights_count = settings.destination_highlights_count

    # Get the destination highlights from Groq
    highlights = await _generate_highlights_list(client, arrival_country, arrival_city, language)

    # For each highlight, generate a short narration
    narrations = []
    for highlight in highlights[:highlights_count]:
        try:
            narration = await _generate_highlight_narration(
                client, highlight, arrival_country, language
            )
            narrations.append(narration)
        except Exception as exc:
            logger.warning("Failed to generate highlight for '%s': %s", highlight, exc)
            continue

    logger.info("Prepared %d destination highlights for %s", len(narrations), arrival_city)
    return narrations


async def _generate_highlights_list(
    client: httpx.AsyncClient,
    country: str,
    city: str,
    language: str,
) -> list[str]:
    """Ask Groq for 20 famous/interesting things about the destination."""
    response = await chat_completion(
        client,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a travel curator. Return ONLY a JSON array of strings. "
                    "No explanation, no preamble, no markdown. Just the JSON array."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Give me 20 of the most fascinating facts, places, cultural highlights, "
                    f"historical moments, or natural wonders about {country} "
                    f"(destination city: {city}). "
                    f"Mix of: famous landmarks, food culture, history, nature, "
                    f"surprising facts, world records, cultural traditions. "
                    f"Each item should be a short topic title like "
                    f"'The Great Wall of China' or 'Ethiopian Coffee Ceremony'. "
                    f"Return as JSON array of 20 strings."
                ),
            },
        ],
        temperature=0.7,
        max_tokens=400,
    )

    try:
        clean = response.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
        highlights = json.loads(clean.strip())
        return [str(h) for h in highlights if h]
    except (json.JSONDecodeError, ValueError):
        # Fallback: split by newlines if JSON parsing fails
        lines = [line.strip("- •").strip() for line in response.split("\n")]
        return [line for line in lines if line and len(line) > 3][:20]


async def _generate_highlight_narration(
    client: httpx.AsyncClient,
    topic: str,
    country: str,
    language: str,
) -> str:
    """Generate a 2-3 sentence narration about one destination highlight."""
    language_name = _LANGUAGE_NAMES.get(language, language)

    # Try to get real Wikipedia facts first
    summary = ""
    import contextlib

    with contextlib.suppress(Exception):
        summary = await get_summary(client, topic)

    if summary and len(summary) > 100:
        user_content = f"Topic: {topic}\n\nFacts: {summary[:800]}"
    else:
        user_content = f"Topic: {topic} (in {country})"

    return await chat_completion(
        client,
        messages=[
            {
                "role": "system",
                "content": (
                    f"You are narrating a destination preview for passengers flying to {country}. "
                    f"Write 2 sentences about this topic that make the passenger excited to arrive. "
                    f"Warm, vivid, documentary tone. No more than 50 words total. "
                    f"Write entirely in {language_name}."
                ),
            },
            {"role": "user", "content": user_content},
        ],
        temperature=0.8,
        max_tokens=100,
    )
