"""
Tests for destination tour service.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.destination_tour_service import (
    _generate_highlight_narration,
    _generate_highlights_list,
    prepare_destination_tour,
)


@pytest.mark.asyncio
async def test_prepare_destination_tour_success():
    """Test successful preparation of destination tour narrations."""
    client = AsyncMock()
    db = MagicMock()

    # Mock the highlights list generation
    highlights = [
        "The Great Wall of China",
        "Shanghai's skyline",
        "Chinese cuisine",
        "The Yangtze River",
    ]

    # Mock each highlight narration generation
    narrations = [
        "The Great Wall stretches over 21,000 kilometers across China's northern border.",
        "Shanghai transformed from rice paddies to a global financial icon in just 30 years.",
        "Chinese cuisine features eight distinct regional traditions, each with unique flavors.",
        "The Yangtze River is the world's third-longest river, flowing through the heart of China.",
    ]

    with (
        patch(
            "app.services.destination_tour_service._generate_highlights_list",
            new_callable=AsyncMock,
        ) as mock_highlights,
        patch(
            "app.services.destination_tour_service._generate_highlight_narration",
            new_callable=AsyncMock,
        ) as mock_narration,
    ):
        mock_highlights.return_value = highlights
        mock_narration.side_effect = narrations

        result = await prepare_destination_tour(
            client,
            db,
            arrival_iata="PVG",
            arrival_country="China",
            arrival_city="Shanghai",
            language="en",
        )

        assert len(result) == 4
        assert result == narrations
        mock_highlights.assert_called_once_with(client, "China", "Shanghai", "en")
        assert mock_narration.call_count == 4


@pytest.mark.asyncio
async def test_prepare_destination_tour_partial_failure():
    """Test that partial failures in narration generation don't break the whole process."""
    client = AsyncMock()
    db = MagicMock()

    highlights = [
        "The Great Wall of China",
        "Shanghai's skyline",
        "Chinese cuisine",
    ]

    # Mock one narration to fail
    def side_effect(client, topic, country, language):
        if topic == "Shanghai's skyline":
            raise Exception("API error")
        return f"Narration about {topic}"

    with (
        patch(
            "app.services.destination_tour_service._generate_highlights_list",
            new_callable=AsyncMock,
        ) as mock_highlights,
        patch(
            "app.services.destination_tour_service._generate_highlight_narration",
            new_callable=AsyncMock,
        ) as mock_narration,
    ):
        mock_highlights.return_value = highlights
        mock_narration.side_effect = side_effect

        result = await prepare_destination_tour(
            client,
            db,
            arrival_iata="PVG",
            arrival_country="China",
            arrival_city="Shanghai",
            language="en",
        )

        # Should return 2 narrations (one failed)
        assert len(result) == 2
        assert "Narration about The Great Wall of China" in result
        assert "Narration about Chinese cuisine" in result


@pytest.mark.asyncio
async def test_generate_highlights_list_json_response():
    """Test parsing JSON response from Groq."""
    client = AsyncMock()

    mock_response = MagicMock()
    mock_response.text = '["The Great Wall", "Shanghai", "Chinese Food", "Yangtze River"]'

    with patch(
        "app.services.destination_tour_service.chat_completion", new_callable=AsyncMock
    ) as mock_completion:
        mock_completion.return_value = mock_response.text

        result = await _generate_highlights_list(client, "China", "Shanghai", "en")

        assert len(result) == 4
        assert "The Great Wall" in result
        assert "Shanghai" in result


@pytest.mark.asyncio
async def test_generate_highlights_list_markdown_response():
    """Test parsing JSON response wrapped in markdown code blocks."""
    client = AsyncMock()

    mock_response = MagicMock()
    mock_response.text = '```json\n["The Great Wall", "Shanghai", "Chinese Food"]\n```'

    with patch(
        "app.services.destination_tour_service.chat_completion", new_callable=AsyncMock
    ) as mock_completion:
        mock_completion.return_value = mock_response.text

        result = await _generate_highlights_list(client, "China", "Shanghai", "en")

        assert len(result) == 3
        assert "The Great Wall" in result


@pytest.mark.asyncio
async def test_generate_highlights_list_fallback():
    """Test fallback to newline splitting when JSON parsing fails."""
    client = AsyncMock()

    mock_response = MagicMock()
    mock_response.text = "The Great Wall\n- Shanghai\nChinese Food\nYangtze River"

    with patch(
        "app.services.destination_tour_service.chat_completion", new_callable=AsyncMock
    ) as mock_completion:
        mock_completion.return_value = mock_response.text

        result = await _generate_highlights_list(client, "China", "Shanghai", "en")

        # Should parse from newlines
        assert len(result) >= 2
        assert any("Great Wall" in item for item in result)


@pytest.mark.asyncio
async def test_generate_highlight_narration_with_wikipedia():
    """Test narration generation when Wikipedia summary is available."""
    client = AsyncMock()

    mock_summary = (
        "The Great Wall of China is a series of fortifications made of stone, brick, "
        "earth and other materials, generally built along the northern borders of China "
        "to protect against raids from various nomadic groups of the Eurasian Steppe."
    )
    mock_narration = (
        "The Great Wall of China stretches over 21,000 kilometers across northern China."
    )

    with (
        patch(
            "app.services.destination_tour_service.get_summary", new_callable=AsyncMock
        ) as mock_get_summary,
        patch(
            "app.services.destination_tour_service.chat_completion", new_callable=AsyncMock
        ) as mock_completion,
    ):
        mock_get_summary.return_value = mock_summary
        mock_completion.return_value = mock_narration

        result = await _generate_highlight_narration(client, "The Great Wall", "China", "en")

        assert result == mock_narration
        mock_get_summary.assert_called_once_with(client, "The Great Wall")
        # Check that the prompt includes the Wikipedia summary
        call_args = mock_completion.call_args
        assert "Facts:" in str(call_args)


@pytest.mark.asyncio
async def test_generate_highlight_narration_without_wikipedia():
    """Test narration generation when Wikipedia summary fails."""
    client = AsyncMock()

    mock_narration = "The Great Wall is one of the most famous landmarks in China."

    with (
        patch(
            "app.services.destination_tour_service.get_summary", new_callable=AsyncMock
        ) as mock_get_summary,
        patch(
            "app.services.destination_tour_service.chat_completion", new_callable=AsyncMock
        ) as mock_completion,
    ):
        mock_get_summary.side_effect = Exception("Wikipedia error")
        mock_completion.return_value = mock_narration

        result = await _generate_highlight_narration(client, "The Great Wall", "China", "en")

        assert result == mock_narration
        # Check that the prompt doesn't include Wikipedia facts
        call_args = mock_completion.call_args
        assert "Facts:" not in str(call_args)


@pytest.mark.asyncio
async def test_generate_highlight_narration_short_summary():
    """Test that short Wikipedia summaries are ignored."""
    client = AsyncMock()

    mock_summary = "Too short"
    mock_narration = "The Great Wall is a famous landmark."

    with (
        patch(
            "app.services.destination_tour_service.get_summary", new_callable=AsyncMock
        ) as mock_get_summary,
        patch(
            "app.services.destination_tour_service.chat_completion", new_callable=AsyncMock
        ) as mock_completion,
    ):
        mock_get_summary.return_value = mock_summary
        mock_completion.return_value = mock_narration

        result = await _generate_highlight_narration(client, "The Great Wall", "China", "en")

        assert result == mock_narration
        # Short summary should be ignored
        call_args = mock_completion.call_args
        assert "Facts:" not in str(call_args)


@pytest.mark.asyncio
async def test_prepare_destination_tour_respects_limit():
    """Test that the number of narrations respects the settings limit."""
    client = AsyncMock()
    db = MagicMock()

    # Generate more highlights than the default limit
    highlights = [f"Highlight {i}" for i in range(30)]

    with (
        patch(
            "app.services.destination_tour_service._generate_highlights_list",
            new_callable=AsyncMock,
        ) as mock_highlights,
        patch(
            "app.services.destination_tour_service._generate_highlight_narration",
            new_callable=AsyncMock,
        ) as mock_narration,
        patch("app.services.destination_tour_service.get_settings") as mock_settings,
    ):
        mock_highlights.return_value = highlights
        mock_narration.return_value = "Test narration"

        # Mock settings to return a limit of 20
        settings_mock = MagicMock()
        settings_mock.destination_highlights_count = 20
        mock_settings.return_value = settings_mock

        result = await prepare_destination_tour(
            client,
            db,
            arrival_iata="PVG",
            arrival_country="China",
            arrival_city="Shanghai",
            language="en",
        )

        # Should only generate 20 narrations even though 30 highlights were returned
        assert len(result) == 20
        assert mock_narration.call_count == 20
