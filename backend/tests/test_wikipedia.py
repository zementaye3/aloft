from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from app.clients.wikipedia import (
    WIKIPEDIA_API_URL,
    WikipediaClientError,
    geosearch,
    get_images,
    get_summary,
)

MOCK_GEOSEARCH_RESPONSE = {
    "query": {
        "geosearch": [
            {
                "pageid": 1001,
                "title": "Holy Trinity Cathedral, Addis Ababa",
                "lat": 9.0177,
                "lon": 38.7669,
                "dist": 450.2,
            },
            {
                "pageid": 1002,
                "title": "National Museum of Ethiopia",
                "lat": 9.0339,
                "lon": 38.7611,
                "dist": 1820.7,
            },
        ]
    }
}


@pytest.fixture(autouse=True)
def no_real_sleep():
    """Every test in this file goes through retry logic that calls
    asyncio.sleep on failure -- patch it so tests run instantly instead of
    actually waiting out the backoff.

    Also clear the module-level geosearch cache between tests so retry and
    error-handling cases don't accidentally hit a cached response from an
    earlier test (the cache key is derived purely from coordinates).
    """
    from app.clients import wikipedia as wikipedia_client

    wikipedia_client._geosearch_cache.clear()
    with patch("app.clients.wikipedia.asyncio.sleep", new=AsyncMock()):
        yield
    wikipedia_client._geosearch_cache.clear()


@pytest.mark.asyncio
async def test_geosearch_parses_results_correctly():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(WIKIPEDIA_API_URL).mock(
                return_value=httpx.Response(200, json=MOCK_GEOSEARCH_RESPONSE)
            )
            results = await geosearch(client, lat=9.02, lng=38.76)

    assert len(results) == 2
    assert results[0].title == "Holy Trinity Cathedral, Addis Ababa"
    assert results[0].page_id == 1001
    assert results[0].distance_m == 450.2


@pytest.mark.asyncio
async def test_geosearch_returns_empty_list_when_nothing_nearby():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(WIKIPEDIA_API_URL).mock(
                return_value=httpx.Response(200, json={"query": {"geosearch": []}})
            )
            results = await geosearch(client, lat=0.0, lng=0.0)

    assert results == []


@pytest.mark.asyncio
async def test_geosearch_skips_malformed_result_but_keeps_the_rest():
    bad_response = {
        "query": {
            "geosearch": [
                {"pageid": 1, "title": "Missing Coordinates"},  # no lat/lon/dist
                MOCK_GEOSEARCH_RESPONSE["query"]["geosearch"][0],
            ]
        }
    }
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(WIKIPEDIA_API_URL).mock(return_value=httpx.Response(200, json=bad_response))
            results = await geosearch(client, lat=9.02, lng=38.76)

    assert len(results) == 1
    assert results[0].title == "Holy Trinity Cathedral, Addis Ababa"


@pytest.mark.asyncio
async def test_geosearch_sends_correct_request_params_and_user_agent():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.get(WIKIPEDIA_API_URL).mock(
                return_value=httpx.Response(200, json={"query": {"geosearch": []}})
            )
            await geosearch(client, lat=9.02, lng=38.76, radius_m=5000, limit=10)

    sent_request = route.calls[0].request
    assert sent_request.url.params["gscoord"] == "9.02|38.76"
    assert sent_request.url.params["gsradius"] == "5000"
    assert sent_request.url.params["gslimit"] == "10"
    assert "AloftFlightNarrationApp" in sent_request.headers["User-Agent"]


@pytest.mark.asyncio
async def test_geosearch_rejects_radius_over_wikipedia_max():
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError):
            await geosearch(client, lat=9.02, lng=38.76, radius_m=20_000)


@pytest.mark.asyncio
async def test_geosearch_rejects_zero_or_negative_radius():
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError):
            await geosearch(client, lat=9.02, lng=38.76, radius_m=0)


@pytest.mark.asyncio
async def test_geosearch_retries_on_503_then_succeeds():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.get(WIKIPEDIA_API_URL).mock(
                side_effect=[
                    httpx.Response(503),
                    httpx.Response(503),
                    httpx.Response(200, json=MOCK_GEOSEARCH_RESPONSE),
                ]
            )
            results = await geosearch(client, lat=9.02, lng=38.76)

    assert route.call_count == 3
    assert len(results) == 2


@pytest.mark.asyncio
async def test_geosearch_raises_after_exhausting_all_retries():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.get(WIKIPEDIA_API_URL).mock(return_value=httpx.Response(503))
            with pytest.raises(WikipediaClientError):
                await geosearch(client, lat=9.02, lng=38.76)

    assert route.call_count == 3  # tried the full _MAX_ATTEMPTS, no more


@pytest.mark.asyncio
async def test_geosearch_does_not_retry_non_retryable_errors():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.get(WIKIPEDIA_API_URL).mock(return_value=httpx.Response(404))
            with pytest.raises(WikipediaClientError):
                await geosearch(client, lat=9.02, lng=38.76)

    assert route.call_count == 1  # a 404 won't change on retry -- fail fast


MOCK_SUMMARY_RESPONSE = {
    "query": {
        "pages": {
            "12345": {
                "pageid": 12345,
                "title": "Holy Trinity Cathedral, Addis Ababa",
                "extract": "Holy Trinity Cathedral is the second largest church in Ethiopia, "
                "built to commemorate Ethiopia's liberation from Italian occupation.",
            }
        }
    }
}

MOCK_MISSING_PAGE_RESPONSE = {
    "query": {"pages": {"-1": {"ns": 0, "title": "Some Nonexistent Title", "missing": ""}}}
}


@pytest.mark.asyncio
async def test_get_summary_returns_extract_text():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(WIKIPEDIA_API_URL).mock(
                return_value=httpx.Response(200, json=MOCK_SUMMARY_RESPONSE)
            )
            summary = await get_summary(client, "Holy Trinity Cathedral, Addis Ababa")

    assert "second largest church in Ethiopia" in summary


@pytest.mark.asyncio
async def test_get_summary_returns_empty_string_for_missing_page():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(WIKIPEDIA_API_URL).mock(
                return_value=httpx.Response(200, json=MOCK_MISSING_PAGE_RESPONSE)
            )
            summary = await get_summary(client, "Some Nonexistent Title")

    assert summary == ""


@pytest.mark.asyncio
async def test_get_summary_sends_correct_params():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.get(WIKIPEDIA_API_URL).mock(
                return_value=httpx.Response(200, json=MOCK_SUMMARY_RESPONSE)
            )
            await get_summary(client, "Holy Trinity Cathedral, Addis Ababa")

    sent = route.calls[0].request
    assert sent.url.params["prop"] == "extracts"
    assert sent.url.params["titles"] == "Holy Trinity Cathedral, Addis Ababa"
    assert sent.url.params["exintro"] == "1"
    assert sent.url.params["explaintext"] == "1"


@pytest.mark.asyncio
async def test_get_summary_retries_on_503_then_succeeds():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.get(WIKIPEDIA_API_URL).mock(
                side_effect=[
                    httpx.Response(503),
                    httpx.Response(200, json=MOCK_SUMMARY_RESPONSE),
                ]
            )
            summary = await get_summary(client, "Holy Trinity Cathedral, Addis Ababa")

    assert route.call_count == 2
    assert "second largest church" in summary


@pytest.mark.asyncio
async def test_get_summary_raises_after_exhausting_retries():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.get(WIKIPEDIA_API_URL).mock(return_value=httpx.Response(503))
            with pytest.raises(WikipediaClientError):
                await get_summary(client, "Anything")

    assert route.call_count == 3


# ---------------------------------------------------------------------------
# get_images tests
# ---------------------------------------------------------------------------

LEAD_IMAGE_RESPONSE = {
    "query": {
        "pages": {
            "12345": {
                "pageid": 12345,
                "title": "Holy Trinity Cathedral, Addis Ababa",
                "original": {
                    "source": "https://upload.wikimedia.org/commons/cathedral_lead.jpg",
                    "width": 1200,
                    "height": 800,
                },
            }
        }
    }
}

NO_LEAD_IMAGE_RESPONSE = {"query": {"pages": {"12345": {"pageid": 12345, "title": "Some Article"}}}}

GALLERY_RESPONSE = {
    "query": {
        "pages": {
            "1": {
                "title": "File:Commons-logo.svg",
                "imageinfo": [
                    {
                        "url": "https://upload.wikimedia.org/commons/Commons-logo.svg",
                        "width": 1024,
                        "height": 1024,
                        "mime": "image/svg+xml",
                    }
                ],
            },
            "2": {
                "title": "File:Edit-icon.png",
                "imageinfo": [
                    {
                        "url": "https://upload.wikimedia.org/commons/Edit-icon.png",
                        "width": 20,
                        "height": 20,
                        "mime": "image/png",
                    }
                ],
            },
            "3": {
                "title": "File:Cathedral_exterior.jpg",
                "imageinfo": [
                    {
                        "url": "https://upload.wikimedia.org/commons/Cathedral_exterior.jpg",
                        "width": 1600,
                        "height": 1200,
                        "mime": "image/jpeg",
                    }
                ],
            },
            "4": {
                "title": "File:Cathedral_interior.jpg",
                "imageinfo": [
                    {
                        "url": "https://upload.wikimedia.org/commons/Cathedral_interior.jpg",
                        "width": 800,
                        "height": 600,
                        "mime": "image/jpeg",
                    }
                ],
            },
            "5": {
                "title": "File:Tiny_thumbnail.jpg",
                "imageinfo": [
                    {
                        "url": "https://upload.wikimedia.org/commons/Tiny_thumbnail.jpg",
                        "width": 100,
                        "height": 100,
                        "mime": "image/jpeg",
                    }
                ],
            },
            "6": {
                "title": "File:Diagram_of_layout.svg",
                "imageinfo": [
                    {
                        "url": "https://upload.wikimedia.org/commons/Diagram_of_layout.svg",
                        "width": 2000,
                        "height": 2000,
                        "mime": "image/svg+xml",
                    }
                ],
            },
        }
    }
}

ONLY_ICONS_GALLERY_RESPONSE = {
    "query": {
        "pages": {
            "1": {
                "title": "File:Commons-logo.svg",
                "imageinfo": [
                    {
                        "url": "https://upload.wikimedia.org/commons/Commons-logo.svg",
                        "width": 1024,
                        "height": 1024,
                        "mime": "image/svg+xml",
                    }
                ],
            }
        }
    }
}


def _images_handler(lead_response: dict, gallery_response: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        if "piprop" in request.url.params:
            return httpx.Response(200, json=lead_response)
        if "generator" in request.url.params:
            return httpx.Response(200, json=gallery_response)
        return httpx.Response(404)

    return handler


@pytest.mark.asyncio
async def test_get_images_returns_lead_image_first():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(WIKIPEDIA_API_URL).mock(
                side_effect=_images_handler(LEAD_IMAGE_RESPONSE, GALLERY_RESPONSE)
            )
            images = await get_images(client, "Holy Trinity Cathedral, Addis Ababa")

    assert images[0].is_lead_image is True
    assert images[0].url == "https://upload.wikimedia.org/commons/cathedral_lead.jpg"


@pytest.mark.asyncio
async def test_get_images_filters_out_icons_logos_and_diagrams():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(WIKIPEDIA_API_URL).mock(
                side_effect=_images_handler(NO_LEAD_IMAGE_RESPONSE, GALLERY_RESPONSE)
            )
            images = await get_images(client, "Some Article")

    urls = {img.url for img in images}
    assert not any("Commons-logo" in url for url in urls)
    assert not any("Diagram_of_layout" in url for url in urls)


@pytest.mark.asyncio
async def test_get_images_filters_out_too_small_images():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(WIKIPEDIA_API_URL).mock(
                side_effect=_images_handler(NO_LEAD_IMAGE_RESPONSE, GALLERY_RESPONSE)
            )
            images = await get_images(client, "Some Article")

    assert not any("Tiny_thumbnail" in img.url for img in images)


@pytest.mark.asyncio
async def test_get_images_sorts_remaining_by_resolution_descending():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(WIKIPEDIA_API_URL).mock(
                side_effect=_images_handler(NO_LEAD_IMAGE_RESPONSE, GALLERY_RESPONSE)
            )
            images = await get_images(client, "Some Article")

    assert "Cathedral_exterior" in images[0].url
    assert "Cathedral_interior" in images[1].url


@pytest.mark.asyncio
async def test_get_images_respects_max_images_limit():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(WIKIPEDIA_API_URL).mock(
                side_effect=_images_handler(LEAD_IMAGE_RESPONSE, GALLERY_RESPONSE)
            )
            images = await get_images(client, "Holy Trinity Cathedral, Addis Ababa", max_images=2)

    assert len(images) == 2


@pytest.mark.asyncio
async def test_get_images_returns_empty_list_when_nothing_real_exists():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(WIKIPEDIA_API_URL).mock(
                side_effect=_images_handler(NO_LEAD_IMAGE_RESPONSE, ONLY_ICONS_GALLERY_RESPONSE)
            )
            images = await get_images(client, "Some Article")

    assert images == []


@pytest.mark.asyncio
async def test_get_images_handles_missing_lead_image_gracefully():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(WIKIPEDIA_API_URL).mock(
                side_effect=_images_handler(NO_LEAD_IMAGE_RESPONSE, GALLERY_RESPONSE)
            )
            images = await get_images(client, "Some Article")

    assert not any(img.is_lead_image for img in images)
    assert len(images) == 2
