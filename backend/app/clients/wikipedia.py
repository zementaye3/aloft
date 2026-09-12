"""
Thin wrapper around three Wikipedia action-API endpoints:
- geosearch: candidate POIs near a point (title + coordinates only)
- get_summary: a short plain-text intro for a known article title
- get_images: real photos for a known article title -- the editorially
  curated lead image plus the largest/cleanest others, never AI-generated,
  never a generic fallback

Sampling multiple points along a corridor and merging geosearch results is
poi_service.py's job, not this file's. Turning a summary into an actual
narrated story is story_service.py's job, not this file's either -- this
stays a thin, honest wrapper around what Wikipedia's API returns.

API docs: https://www.mediawiki.org/wiki/API:Geosearch
          https://www.mediawiki.org/wiki/API:Extracts
          https://www.mediawiki.org/wiki/API:Pageimages
          https://www.mediawiki.org/wiki/API:Imageinfo
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from pydantic import BaseModel

from app.core.config import get_settings

logger = logging.getLogger("aloft.clients.wikipedia")

WIKIPEDIA_API_URL = "https://en.wikipedia.org/w/api.php"

MAX_RADIUS_M = 10_000
MAX_LIMIT = 500

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 2.0

# Simple in-memory cache for geosearch results (rounded coordinates)
# Cache size limited to prevent memory issues
_GEOSEARCH_CACHE_SIZE = 100
_geosearch_cache: dict[tuple, list[RawPoi]] = {}

_wiki_semaphore = asyncio.Semaphore(5)


def _cache_key(lat: float, lng: float, radius_m: int, limit: int) -> tuple:
    """Create a cache key by rounding coordinates to ~1km precision."""
    return (round(lat, 3), round(lng, 3), radius_m, limit)


class RawPoi(BaseModel):
    title: str
    page_id: int
    lat: float
    lng: float
    distance_m: float


class RawImage(BaseModel):
    """A real photo for a POI, straight from Wikimedia, unprocessed."""

    url: str
    width: int
    height: int
    # True for Wikipedia's own editorially-curated lead/infobox image --
    # picked by human editors as the single best representative photo,
    # which is a stronger "best image" signal than raw view counts even
    # if Commons exposed those cheaply at the file level (it doesn't).
    is_lead_image: bool


# Below this, an image is almost certainly a thumbnail-sized icon, not a
# real photo worth showing.
_MIN_IMAGE_DIMENSION_PX = 400

_ALLOWED_IMAGE_MIME_TYPES = {"image/jpeg", "image/png"}

# Every Wikipedia article embeds standard wiki-interface images alongside
# any real photos. Filtered by filename substring since Wikipedia doesn't
# tag them as "interface chrome" in any structured way the API exposes.
_EXCLUDED_FILENAME_SUBSTRINGS = (
    "commons-logo",
    "wiktionary",
    "wikidata-logo",
    "wikinews",
    "wikiquote",
    "edit-icon",
    "icon_",
    "_icon",
    "question_book",
    "ambox",
    "padlock",
    "disambig",
    "wiki_letter",
    "p_vip",
    "merge-arrow",
    "octagon-",
    "red_pencil",
    "loudspeaker",
    "speakerlink",
    "crystal_clear",
    "symbol_",
    "text_document",
    "folder_",
    "ablogo",
    "ok-icon",
)


class WikipediaClientError(Exception):
    pass


async def geosearch(
    client: httpx.AsyncClient,
    lat: float,
    lng: float,
    radius_m: int = MAX_RADIUS_M,
    limit: int = 50,
) -> list[RawPoi]:
    if not (0 < radius_m <= MAX_RADIUS_M):
        raise ValueError(f"radius_m must be between 1 and {MAX_RADIUS_M}, got {radius_m}")
    if not (0 < limit <= MAX_LIMIT):
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}, got {limit}")

    # Check cache first
    key = _cache_key(lat, lng, radius_m, limit)
    if key in _geosearch_cache:
        logger.debug("Cache hit for geosearch near (%s, %s)", lat, lng)
        return _geosearch_cache[key]

    params = {
        "action": "query",
        "list": "geosearch",
        "gscoord": f"{lat}|{lng}",
        "gsradius": radius_m,
        "gslimit": limit,
        "format": "json",
    }
    response = await _request_with_retries(
        client, params, log_context=f"geosearch near ({lat}, {lng})"
    )
    results = _parse_geosearch_response(response, lat, lng)

    # Cache the results
    if len(_geosearch_cache) >= _GEOSEARCH_CACHE_SIZE:
        # Simple eviction: remove oldest entry (first key)
        _geosearch_cache.pop(next(iter(_geosearch_cache)))
    _geosearch_cache[key] = results

    return results


async def get_summary(client: httpx.AsyncClient, title: str) -> str:
    params = {
        "action": "query",
        "prop": "extracts",
        "exintro": 1,
        "explaintext": 1,
        "titles": title,
        "format": "json",
    }
    async with _wiki_semaphore:
        response = await _request_with_retries(
            client, params, log_context=f"get_summary for '{title}'"
        )
    return _parse_summary_response(response, title)


async def get_images(client: httpx.AsyncClient, title: str, max_images: int = 4) -> list[RawImage]:
    """Fetch real photos for a Wikipedia article -- never AI-generated,
    never a generic fallback. Empty list means no real image exists, not an error.

    Combines two signals (Commons doesn't expose per-file view counts cheaply):
    1. pageimages -- Wikipedia's editorially-curated lead image, returned first.
    2. Largest other real photos embedded in the article, after filtering
       wiki-interface icons/logos/diagrams and sub-400px images, ranked by
       resolution descending.

    Raises:
        WikipediaClientError: if a request fails outright.
    """
    async with _wiki_semaphore:
        # These two calls are independent (both only need the article title),
        # so run them concurrently instead of one after the other -- halves
        # the wall-clock time of every image fetch.
        lead_image, gallery_images = await asyncio.gather(
            _get_lead_image(client, title), _get_gallery_images(client, title)
        )

    results: list[RawImage] = []
    seen_urls: set[str] = set()

    if lead_image is not None:
        results.append(lead_image)
        seen_urls.add(lead_image.url)

    gallery_images.sort(key=lambda img: img.width * img.height, reverse=True)
    for image in gallery_images:
        if len(results) >= max_images:
            break
        if image.url in seen_urls:
            continue
        results.append(image)
        seen_urls.add(image.url)

    return results


async def _get_lead_image(client: httpx.AsyncClient, title: str) -> RawImage | None:
    params = {
        "action": "query",
        "prop": "pageimages",
        "piprop": "original",
        "titles": title,
        "format": "json",
    }
    response = await _request_with_retries(
        client, params, log_context=f"get_lead_image for '{title}'"
    )
    pages = response.json().get("query", {}).get("pages", {})
    if not pages:
        return None
    page = next(iter(pages.values()))
    original = page.get("original")
    if original is None:
        return None
    return RawImage(
        url=original["source"],
        width=original["width"],
        height=original["height"],
        is_lead_image=True,
    )


async def _get_gallery_images(client: httpx.AsyncClient, title: str) -> list[RawImage]:
    params = {
        "action": "query",
        "generator": "images",
        "titles": title,
        "prop": "imageinfo",
        "iiprop": "url|size|mime",
        "gimlimit": 50,
        "format": "json",
    }
    response = await _request_with_retries(
        client, params, log_context=f"get_gallery_images for '{title}'"
    )
    pages = response.json().get("query", {}).get("pages", {})

    images: list[RawImage] = []
    for page in pages.values():
        imageinfo = (page.get("imageinfo") or [{}])[0]
        if not imageinfo or not _is_real_photo(imageinfo):
            continue
        images.append(
            RawImage(
                url=imageinfo["url"],
                width=imageinfo["width"],
                height=imageinfo["height"],
                is_lead_image=False,
            )
        )
    return images


def _is_real_photo(imageinfo: dict) -> bool:
    """Filters out wiki-interface chrome, diagrams, and anything too small."""
    mime = imageinfo.get("mime", "")
    width = imageinfo.get("width", 0)
    height = imageinfo.get("height", 0)
    url = imageinfo.get("url", "").lower()

    if mime not in _ALLOWED_IMAGE_MIME_TYPES:
        return False
    if width < _MIN_IMAGE_DIMENSION_PX or height < _MIN_IMAGE_DIMENSION_PX:
        return False
    return not any(pattern in url for pattern in _EXCLUDED_FILENAME_SUBSTRINGS)


async def _request_with_retries(
    client: httpx.AsyncClient, params: dict, *, log_context: str
) -> httpx.Response:
    headers = {"User-Agent": _user_agent()}
    timeout = httpx.Timeout(connect=3.0, read=8.0, write=3.0, pool=3.0)

    last_error: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = await client.get(
                WIKIPEDIA_API_URL, params=params, headers=headers, timeout=timeout
            )
            response.raise_for_status()
        except httpx.TransportError as exc:
            last_error = exc
            logger.warning(
                "Wikipedia %s network error, attempt %d/%d: %s",
                log_context,
                attempt,
                _MAX_ATTEMPTS,
                exc,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in _RETRYABLE_STATUS_CODES:
                raise WikipediaClientError(
                    f"Wikipedia returned non-retryable status "
                    f"{exc.response.status_code} for {log_context}"
                ) from exc
            last_error = exc
            logger.warning(
                "Wikipedia %s got retryable status %d, attempt %d/%d",
                log_context,
                exc.response.status_code,
                attempt,
                _MAX_ATTEMPTS,
            )
        else:
            return response

        if attempt < _MAX_ATTEMPTS:
            await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))

    logger.error("Wikipedia %s failed after %d attempts", log_context, _MAX_ATTEMPTS)
    raise WikipediaClientError(
        f"{log_context} failed after {_MAX_ATTEMPTS} attempts"
    ) from last_error


def _user_agent() -> str:
    return f"AloftFlightNarrationApp/0.1 ({get_settings().app_contact_email})"


def _parse_geosearch_response(response: httpx.Response, lat: float, lng: float) -> list[RawPoi]:
    raw_results = response.json().get("query", {}).get("geosearch", [])
    pois: list[RawPoi] = []
    for raw in raw_results:
        try:
            pois.append(
                RawPoi(
                    title=raw["title"],
                    page_id=raw["pageid"],
                    lat=raw["lat"],
                    lng=raw["lon"],
                    distance_m=raw["dist"],
                )
            )
        except KeyError as exc:
            logger.warning(
                "Skipping malformed geosearch result near (%s, %s): missing key %s",
                lat,
                lng,
                exc,
            )
    return pois


def _parse_summary_response(response: httpx.Response, title: str) -> str:
    pages = response.json().get("query", {}).get("pages", {})
    if not pages:
        logger.warning("Wikipedia get_summary for '%s' returned no pages", title)
        return ""
    page = next(iter(pages.values()))
    if "missing" in page:
        logger.warning("Wikipedia get_summary: '%s' does not exist", title)
        return ""
    return page.get("extract", "")
