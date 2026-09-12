from __future__ import annotations

from pyproj import Geod, Transformer
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import transform

_GEOD = Geod(ellps="WGS84")
_DEFAULT_NUM_POINTS = 100


class DegenerateRouteError(ValueError):
    pass


def build_corridor(
    departure: tuple[float, float],
    arrival: tuple[float, float],
    width_km: float = 100.0,
    num_points: int = _DEFAULT_NUM_POINTS,
) -> Polygon:
    if width_km <= 0:
        raise ValueError(f"width_km must be positive, got {width_km}")

    dep_lat, dep_lng = departure
    arr_lat, arr_lng = arrival

    if _is_same_point(dep_lat, dep_lng, arr_lat, arr_lng):
        raise DegenerateRouteError(
            f"departure {departure} and arrival {arrival} are effectively "
            "the same point -- can't build a corridor from a route with no length."
        )

    path_points = _great_circle_points(dep_lat, dep_lng, arr_lat, arr_lng, num_points)
    line_wgs84 = LineString([(lng, lat) for lat, lng in path_points])

    midpoint = path_points[len(path_points) // 2]
    to_planar, to_wgs84 = _planar_transformers(center_lat=midpoint[0], center_lng=midpoint[1])

    line_planar = transform(to_planar, line_wgs84)
    corridor_planar = line_planar.buffer((width_km * 1000) / 2, quad_segs=16)
    return transform(to_wgs84, corridor_planar)


def sample_points_by_spacing(
    departure: tuple[float, float],
    arrival: tuple[float, float],
    spacing_km: float,
) -> list[tuple[float, float]]:
    if spacing_km <= 0:
        raise ValueError(f"spacing_km must be positive, got {spacing_km}")

    dep_lat, dep_lng = departure
    arr_lat, arr_lng = arrival

    _, _, total_distance_m = _GEOD.inv(dep_lng, dep_lat, arr_lng, arr_lat)
    num_points = max(2, round(total_distance_m / (spacing_km * 1000)) + 1)
    return _great_circle_points(dep_lat, dep_lng, arr_lat, arr_lng, num_points)


def corridor_to_geojson(polygon: Polygon) -> dict:
    return {"type": "Polygon", "coordinates": [list(polygon.exterior.coords)]}


def point_in_corridor(polygon: Polygon, lat: float, lng: float) -> bool:
    return polygon.contains(Point(lng, lat))


def distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Real geodesic distance between two points, in kilometers.

    Shared here because it's the same WGS84 ellipsoid math _is_same_point
    already uses internally -- one correct implementation, not a second
    one that might quietly drift from it.
    """
    _, _, distance_m = _GEOD.inv(lng1, lat1, lng2, lat2)
    return distance_m / 1000


def _is_same_point(lat1: float, lng1: float, lat2: float, lng2: float) -> bool:
    _, _, distance_m = _GEOD.inv(lng1, lat1, lng2, lat2)
    return distance_m < 1.0


def _great_circle_points(
    lat1: float, lng1: float, lat2: float, lng2: float, num_points: int
) -> list[tuple[float, float]]:
    intermediate = _GEOD.npts(lng1, lat1, lng2, lat2, num_points - 2)
    return [(lat1, lng1)] + [(lat, lng) for lng, lat in intermediate] + [(lat2, lng2)]


def _planar_transformers(center_lat: float, center_lng: float):
    aeqd_crs = f"+proj=aeqd +lat_0={center_lat} +lon_0={center_lng} +units=m +ellps=WGS84"
    to_planar = Transformer.from_crs("EPSG:4326", aeqd_crs, always_xy=True).transform
    to_wgs84 = Transformer.from_crs(aeqd_crs, "EPSG:4326", always_xy=True).transform
    return to_planar, to_wgs84
