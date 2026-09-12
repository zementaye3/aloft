from __future__ import annotations

import httpx

from app.clients.groq import chat_completion
from app.clients.wikipedia import get_summary
from app.core.config import get_settings
from app.models.story import Story

_MIN_SUMMARY_LENGTH = 40

_LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "am": "Amharic",
    "ar": "Arabic",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "zh": "Chinese",
    "hi": "Hindi",
}

_STYLE_INSTRUCTION = (
    "You are narrating for an app where passengers hear a short, vivid story "
    "about the place they are currently flying over. Write like a documentary "
    "narrator revealing something extraordinary -- warm, a little dramatic, "
    "with a thoughtful beat before the key detail. Never just list facts as "
    "a summary. Write 2-3 sentences, no more. Do not address the listener "
    "directly with phrases like 'you are flying over' -- narrate the place "
    "itself, not the act of flying over it."
)


class InsufficientFactsError(Exception):
    """Raised when there isn't enough real factual material to generate an honest story."""


class UnsupportedLanguageError(ValueError):
    """Raised when the requested language code is not supported."""


def supported_languages() -> list[str]:
    return list(_LANGUAGE_NAMES.keys())


async def generate_story(
    client: httpx.AsyncClient,
    poi_source_id: str,
    poi_name: str,
    language: str = "en",
) -> Story:
    if language not in _LANGUAGE_NAMES:
        raise UnsupportedLanguageError(
            f"Unsupported language '{language}'. Supported: {', '.join(_LANGUAGE_NAMES)}"
        )

    summary = await get_summary(client, poi_name)
    if len(summary.strip()) < _MIN_SUMMARY_LENGTH:
        raise InsufficientFactsError(
            f"No usable Wikipedia summary for '{poi_name}' ({len(summary.strip())} chars)"
            " -- refusing to generate a story from the name alone."
        )

    messages = _build_prompt(poi_name, summary, language)
    text = await chat_completion(client, messages, temperature=0.8, max_tokens=300)

    return Story(
        poi_source_id=poi_source_id,
        language=language,
        text_content=text.strip(),
        model_version=get_settings().groq_model,
    )


def _build_prompt(poi_name: str, summary: str, language: str) -> list[dict[str, str]]:
    language_name = _LANGUAGE_NAMES[language]
    return [
        {"role": "system", "content": f"{_STYLE_INSTRUCTION} Write entirely in {language_name}."},
        {"role": "user", "content": f"Place: {poi_name}\n\nFacts to draw from:\n{summary}"},
    ]


async def generate_upcoming_story(
    client: httpx.AsyncClient,
    poi_source_id: str,
    poi_name: str,
    distance_km: float,
    language: str = "en",
) -> Story:
    """Generate a 'coming up ahead' teaser story for a POI.

    Different tone from generate_story() -- shorter, forward-looking,
    builds anticipation rather than describing the place fully.
    A passenger will hear this while still 20-30 minutes away.
    """
    summary = await get_summary(client, poi_name)
    if len(summary.strip()) < _MIN_SUMMARY_LENGTH:
        raise InsufficientFactsError(
            f"No usable summary for '{poi_name}' -- can't generate upcoming teaser."
        )

    minutes_away = round((distance_km / 850) * 60)  # 850 km/h average cruising speed

    messages = _build_upcoming_prompt(poi_name, summary, language, minutes_away)
    text = await chat_completion(client, messages, temperature=0.8, max_tokens=150)

    return Story(
        poi_source_id=f"upcoming:{poi_source_id}",
        language=language,
        text_content=text.strip(),
        style_prompt="upcoming teaser",
        model_version=get_settings().groq_model,
    )


def _build_upcoming_prompt(
    poi_name: str,
    summary: str,
    language: str,
    minutes_away: int,
) -> list[dict[str, str]]:
    language_name = _LANGUAGE_NAMES.get(language, language)
    system_content = (
        f"You are narrating for passengers on a flight. The place you're "
        f"describing is approximately {minutes_away} minutes ahead. Write "
        f"ONE sentence that builds anticipation -- hint at what makes this "
        f"place remarkable without giving everything away. Dramatic, warm, "
        f"forward-looking. No more than 30 words. Write entirely in {language_name}."
    )
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": f"Place coming up: {poi_name}\n\nFacts: {summary}"},
    ]
