from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.clients.groq import GROQ_API_URL
from app.clients.wikipedia import WIKIPEDIA_API_URL
from app.services.story_service import (
    InsufficientFactsError,
    UnsupportedLanguageError,
    generate_story,
)

CATHEDRAL_SUMMARY = {
    "query": {
        "pages": {
            "1": {
                "title": "Holy Trinity Cathedral, Addis Ababa",
                "extract": "Holy Trinity Cathedral is the second largest church in "
                "Ethiopia, built to commemorate liberation from Italian occupation.",
            }
        }
    }
}

EMPTY_SUMMARY = {"query": {"pages": {"-1": {"title": "Nowhere", "missing": ""}}}}

GENERATED_TEXT = {
    "choices": [
        {
            "message": {
                "content": "Below, a cathedral rises -- built not for kings, but for freedom."
            }
        }
    ]
}


@pytest.fixture(autouse=True)
def no_real_sleep():
    with (
        patch("app.clients.wikipedia.asyncio.sleep", new=AsyncMock()),
        patch("app.clients.groq.asyncio.sleep", new=AsyncMock()),
    ):
        yield


@pytest.fixture(autouse=True)
def fake_groq_key(monkeypatch):
    fake_settings = type(
        "S", (), {"groq_api_key": SecretStr("test-key"), "groq_model": "test-model"}
    )()
    monkeypatch.setattr("app.clients.groq.get_settings", lambda: fake_settings)
    monkeypatch.setattr("app.services.story_service.get_settings", lambda: fake_settings)


@pytest.mark.asyncio
async def test_generate_story_returns_story_with_correct_fields():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(WIKIPEDIA_API_URL).mock(
                return_value=httpx.Response(200, json=CATHEDRAL_SUMMARY)
            )
            respx.post(GROQ_API_URL).mock(return_value=httpx.Response(200, json=GENERATED_TEXT))

            story = await generate_story(
                client,
                poi_source_id="wikipedia:1001",
                poi_name="Holy Trinity Cathedral, Addis Ababa",
            )

    assert story.poi_source_id == "wikipedia:1001"
    assert story.language == "en"
    assert "cathedral rises" in story.text_content
    assert story.model_version == "test-model"


@pytest.mark.asyncio
async def test_generate_story_refuses_when_summary_is_empty():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(WIKIPEDIA_API_URL).mock(return_value=httpx.Response(200, json=EMPTY_SUMMARY))
            groq_route = respx.post(GROQ_API_URL).mock(
                return_value=httpx.Response(200, json=GENERATED_TEXT)
            )

            with pytest.raises(InsufficientFactsError):
                await generate_story(client, poi_source_id="wikipedia:9999", poi_name="Nowhere")

    assert groq_route.call_count == 0  # LLM is never called when there are no facts


@pytest.mark.asyncio
async def test_generate_story_sends_facts_and_language_in_prompt():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(WIKIPEDIA_API_URL).mock(
                return_value=httpx.Response(200, json=CATHEDRAL_SUMMARY)
            )
            groq_route = respx.post(GROQ_API_URL).mock(
                return_value=httpx.Response(200, json=GENERATED_TEXT)
            )

            await generate_story(
                client,
                poi_source_id="wikipedia:1001",
                poi_name="Holy Trinity Cathedral, Addis Ababa",
                language="am",
            )

    sent_body = groq_route.calls[0].request.content.decode()
    assert "second largest church" in sent_body
    assert "Amharic" in sent_body


@pytest.mark.asyncio
async def test_generate_story_rejects_unsupported_language():
    async with httpx.AsyncClient() as client:
        with pytest.raises(UnsupportedLanguageError, match="Unsupported language 'xx'"):
            await generate_story(
                client, poi_source_id="wikipedia:1001", poi_name="Anywhere", language="xx"
            )
