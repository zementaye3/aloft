from __future__ import annotations

import hashlib

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.models.route_bundle import RouteBundle

# Human-readable prefix uses 2dp (~1.1km resolution) for legibility.
# A 6-character hex hash of the full-precision coordinates is appended
# so two departure/arrival pairs that round to the same 2dp prefix still
# get distinct keys (e.g. two airports within ~1km of each other).
_COORD_PRECISION = 2


def make_route_key(departure: tuple[float, float], arrival: tuple[float, float]) -> str:
    dep_lat, dep_lng = departure
    arr_lat, arr_lng = arrival
    # Round to _COORD_PRECISION first so tiny floating-point differences in the
    # same coordinate don't produce different keys.
    dep_lat_r = round(dep_lat, _COORD_PRECISION)
    dep_lng_r = round(dep_lng, _COORD_PRECISION)
    arr_lat_r = round(arr_lat, _COORD_PRECISION)
    arr_lng_r = round(arr_lng, _COORD_PRECISION)

    prefix = (
        f"{dep_lat_r:.{_COORD_PRECISION}f},{dep_lng_r:.{_COORD_PRECISION}f}"
        f"__{arr_lat_r:.{_COORD_PRECISION}f},{arr_lng_r:.{_COORD_PRECISION}f}"
    )
    # Hash the rounded (not raw) coords so two airports that round to the same
    # prefix still share a key (float noise absorbed), while two genuinely
    # different airports within ~1km of each other get distinct hashes.
    full = f"{dep_lat_r},{dep_lng_r}__{arr_lat_r},{arr_lng_r}"
    suffix = hashlib.sha256(full.encode()).hexdigest()[:6]
    return f"{prefix}-{suffix}"


async def save_route_bundle(
    db: AsyncIOMotorDatabase,
    departure: tuple[float, float],
    arrival: tuple[float, float],
    poi_source_ids: list[str],
) -> RouteBundle:
    bundle = RouteBundle(
        route_key=make_route_key(departure, arrival),
        departure=departure,
        arrival=arrival,
        poi_source_ids=poi_source_ids,
    )
    await db.route_bundles.update_one(
        {"route_key": bundle.route_key},
        {"$set": bundle.to_mongo_dict()},
        upsert=True,
    )
    return bundle


async def get_route_bundle(db: AsyncIOMotorDatabase, route_key: str) -> RouteBundle | None:
    doc = await db.route_bundles.find_one({"route_key": route_key})
    if doc is None:
        return None
    doc.pop("_id", None)
    return RouteBundle(**doc)
