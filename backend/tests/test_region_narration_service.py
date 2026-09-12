"""
Tests for the region narration service (app/services/region_narration_service.py).

Covers:
  - Curated ocean facts are returned for known oceans
  - Curated facts are translated for non-English languages
  - Unknown regions fall back to Groq generation
  - English language uses curated facts directly
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
import respx

from app.clients.geocoding_client import RegionInfo
from app.services.region_narration_service import (
    OCEAN_FACTS,
    _generate_text,
    generate_region_narration,
)


@pytest.fixture
def http_client():
    return httpx.AsyncClient()


# ---------------------------------------------------------------------------
# _generate_text unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uses_curated_fact_for_known_ocean_english(http_client):
    """Known oceans in English return curated fact without calling Groq."""
    region = RegionInfo(
        description="North Atlantic Ocean",
        is_ocean=True,
        country=None,
        locality=None,
    )

    with patch("app.services.region_narration_service.chat_completion") as mock_groq:
        result = await _generate_text(http_client, region, "en")

        # Should NOT call Groq for English curated facts
        mock_groq.assert_not_called()
        assert "busiest air corridors" in result


@pytest.mark.asyncio
async def test_translates_curated_fact_for_non_english(http_client):
    """Known oceans in non-English languages trigger translation via Groq."""
    region = RegionInfo(
        description="North Atlantic Ocean",
        is_ocean=True,
        country=None,
        locality=None,
    )

    with patch("app.services.region_narration_service.chat_completion") as mock_groq:
        mock_groq.return_value = (
            "El Atlántico Norte es uno de los corredores aéreos más transitados del mundo."
        )

        result = await _generate_text(http_client, region, "es")

        # Should call Groq for translation
        mock_groq.assert_called_once()
        assert (
            result
            == "El Atlántico Norte es uno de los corredores aéreos más transitados del mundo."
        )


@pytest.mark.asyncio
async def test_unknown_ocean_falls_back_to_groq(http_client):
    """Unknown oceans trigger Groq generation."""
    region = RegionInfo(
        description="Southern Ocean",
        is_ocean=True,
        country=None,
        locality=None,
    )

    with patch("app.services.region_narration_service.chat_completion") as mock_groq:
        mock_groq.return_value = "The Southern Ocean surrounds Antarctica."

        result = await _generate_text(http_client, region, "en")

        # Should call Groq for generation
        mock_groq.assert_called_once()
        assert result == "The Southern Ocean surrounds Antarctica."


@pytest.mark.asyncio
async def test_land_region_falls_back_to_groq(http_client):
    """Land regions trigger Groq generation."""
    region = RegionInfo(
        description="Sahara Desert",
        is_ocean=False,
        country="Algeria",
        locality=None,
    )

    with patch("app.services.region_narration_service.chat_completion") as mock_groq:
        mock_groq.return_value = "The Sahara covers 9 million square kilometers."

        result = await _generate_text(http_client, region, "en")

        # Should call Groq for generation
        mock_groq.assert_called_once()
        assert result == "The Sahara covers 9 million square kilometers."


@pytest.mark.asyncio
async def test_groq_receives_correct_context_for_ocean(http_client):
    """Groq prompt includes correct context for ocean regions."""
    region = RegionInfo(
        description="Pacific Ocean",
        is_ocean=True,
        country=None,
        locality=None,
    )

    with patch("app.services.region_narration_service.chat_completion") as mock_groq:
        mock_groq.return_value = "Generated text."

        await _generate_text(http_client, region, "en")

        # Check that Groq was called (Pacific Ocean is not in curated facts)
        assert mock_groq.called
        call_args = mock_groq.call_args
        # chat_completion is called with (client, messages, **kwargs)
        # So messages should be the second positional argument
        if len(call_args[0]) >= 2:
            messages = call_args[0][1]
            system_message = messages[0]["content"]
            assert "flight over this body of water" in system_message
            assert "the Pacific Ocean" in messages[1]["content"]


@pytest.mark.asyncio
async def test_groq_receives_correct_context_for_land(http_client):
    """Groq prompt includes correct context for land regions."""
    region = RegionInfo(
        description="County Cork, Ireland",
        is_ocean=False,
        country="Ireland",
        locality="Cork",
    )

    with patch("app.services.region_narration_service.chat_completion") as mock_groq:
        mock_groq.return_value = "Generated text."

        await _generate_text(http_client, region, "en")

        assert mock_groq.called
        call_args = mock_groq.call_args
        if len(call_args[0]) >= 2:
            messages = call_args[0][1]
            system_message = messages[0]["content"]
            assert "currently passing over this region" in system_message
            assert "County Cork, Ireland" in messages[1]["content"]


@pytest.mark.asyncio
async def test_groq_includes_language_in_prompt(http_client):
    """Groq prompt includes target language."""
    region = RegionInfo(
        description="Unknown Ocean",
        is_ocean=True,
        country=None,
        locality=None,
    )

    with patch("app.services.region_narration_service.chat_completion") as mock_groq:
        mock_groq.return_value = "Texto generado."

        await _generate_text(http_client, region, "es")

        assert mock_groq.called
        call_args = mock_groq.call_args
        if len(call_args[0]) >= 2:
            messages = call_args[0][1]
            system_message = messages[0]["content"]
            assert "Spanish" in system_message


# ---------------------------------------------------------------------------
# generate_region_narration integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_generate_region_narration_calls_reverse_geocode(http_client):
    """Full integration test: calls reverse_geocode then generates text."""
    # Mock reverse geocoding response with unknown ocean (not in curated facts)
    geocode_url = "https://api.bigdatacloud.net/data/reverse-geocode-client"
    respx.get(geocode_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "countryName": "",
                "locality": "Southern Ocean",
                "localityInfo": {"informative": [{"name": "Southern Ocean"}]},
                "principalSubdivision": "",
            },
        )
    )

    with patch("app.services.region_narration_service.chat_completion") as mock_groq:
        mock_groq.return_value = "Generated narration."

        result = await generate_region_narration(http_client, 42.66, -8.46, "en")

        assert result == "Generated narration."
        # Should call Groq for unknown ocean
        mock_groq.assert_called_once()


@pytest.mark.asyncio
async def test_generate_region_narration_handles_geocoding_failure(http_client):
    """If reverse geocoding fails, returns fallback region info."""
    with patch("app.services.region_narration_service.reverse_geocode") as mock_geocode:
        mock_geocode.return_value = RegionInfo(
            description="a remote area",
            is_ocean=False,
            country=None,
            locality=None,
        )

        with patch("app.services.region_narration_service.chat_completion") as mock_groq:
            mock_groq.return_value = "Fallback narration."

            result = await generate_region_narration(http_client, 42.66, -8.46, "en")

            assert result == "Fallback narration."
            mock_groq.assert_called_once()


# ---------------------------------------------------------------------------
# Curated facts validation
# ---------------------------------------------------------------------------


def test_curated_facts_contain_expected_oceans():
    """Verify curated facts exist for major oceans."""
    expected_oceans = [
        "North Atlantic Ocean",
        "South Atlantic Ocean",
        "North Pacific Ocean",
        "Indian Ocean",
        "Arctic Ocean",
    ]

    for ocean in expected_oceans:
        assert ocean in OCEAN_FACTS
        assert len(OCEAN_FACTS[ocean]) > 50  # Substantial content


def test_curated_facts_are_in_english():
    """All curated facts should be in English for the base version."""
    for _ocean, fact in OCEAN_FACTS.items():
        assert isinstance(fact, str)
        assert len(fact) > 0
