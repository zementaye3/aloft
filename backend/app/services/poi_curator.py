"""
Selects the best 25-30 POIs from a discovered corridor.
Quality over quantity -- a curated set of remarkable places
is better than 400 mediocre ones that burn through API quotas.

Scoring factors:
1. Wikipedia distance as proxy for notability -- closer to sample point = more geographically specific
2. Title length as proxy for notability -- famous places tend to have shorter, cleaner titles
3. Geographic spread -- don't cluster 10 POIs in one city
4. Position along route -- spread evenly across the journey
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.clients.wikipedia import RawPoi
from app.core.config import get_settings
from app.services.corridor import distance_km

logger = logging.getLogger("aloft.services.poi_curator")


@dataclass
class ScoredPoi:
    raw_poi: RawPoi
    score: float
    position_along_route: float  # 0.0 to 1.0


def curate_pois(
    raw_pois: list[RawPoi],
    departure: tuple[float, float],
    arrival: tuple[float, float],
    max_pois: int | None = None,
) -> list[RawPoi]:
    """Select the best POIs from the full discovered list.

    Returns at most max_pois POIs, well-spaced along the route,
    prioritizing notable places over obscure ones.
    """
    settings = get_settings()
    if max_pois is None:
        max_pois = settings.max_curated_pois_per_route

    scored = [_score_poi(poi, departure, arrival) for poi in raw_pois]

    if len(raw_pois) <= max_pois:
        # Small routes: take everything, no greedy selection needed.
        # Still sort by position along route so narration happens in
        # geographic order (departure -> arrival).
        scored.sort(key=lambda s: s.position_along_route)
        return [s.raw_poi for s in scored]

    scored.sort(key=lambda s: s.score, reverse=True)

    # Greedy selection: take highest-scored POIs that aren't
    # too close to an already-selected one
    selected: list[ScoredPoi] = []
    min_spacing_km = settings.poi_min_spacing_km

    for candidate in scored:
        if _too_close_to_existing(candidate, selected, min_spacing_km):
            continue
        selected.append(candidate)
        if len(selected) >= max_pois:
            break

    # Sort final selection by position along route
    # so narration happens in geographic order
    selected.sort(key=lambda s: s.position_along_route)

    logger.info(
        "Curated %d POIs from %d discovered (kept top %d%%)",
        len(selected),
        len(raw_pois),
        round(len(selected) / len(raw_pois) * 100),
    )
    return [s.raw_poi for s in selected]


def _score_poi(
    poi: RawPoi,
    departure: tuple[float, float],
    arrival: tuple[float, float],
) -> ScoredPoi:
    score = 0.0

    # Wikipedia distance as a proxy for article notability --
    # the API returns results sorted by distance, so closer results
    # to our sample points got found first. We invert: a POI found
    # by a sample point very close to it is more "pinned" to that
    # spot = more geographically specific = more interesting.
    # Scale: 0 to 40 points
    proximity_score = max(0, 40 - (poi.distance_m / 250))
    score += proximity_score

    # Title length as a rough notability proxy --
    # famous places tend to have shorter, cleaner titles
    # "London" > "Small Industrial Estate near Coventry"
    # Scale: 0 to 20 points
    title_words = len(poi.title.split())
    if title_words <= 2:
        score += 20
    elif title_words <= 4:
        score += 12
    elif title_words <= 6:
        score += 5

    # Position along route (0 = departure, 1 = arrival)
    dep_lat, dep_lng = departure
    arr_lat, arr_lng = arrival
    total_dist = distance_km(dep_lat, dep_lng, arr_lat, arr_lng)
    poi_dist_from_dep = distance_km(dep_lat, dep_lng, poi.lat, poi.lng)
    position = min(1.0, poi_dist_from_dep / total_dist) if total_dist > 0 else 0.5

    return ScoredPoi(raw_poi=poi, score=score, position_along_route=position)


def _too_close_to_existing(
    candidate: ScoredPoi,
    selected: list[ScoredPoi],
    min_spacing_km: float,
) -> bool:
    for existing in selected:
        dist = distance_km(
            candidate.raw_poi.lat,
            candidate.raw_poi.lng,
            existing.raw_poi.lat,
            existing.raw_poi.lng,
        )
        if dist < min_spacing_km:
            return True
    return False
