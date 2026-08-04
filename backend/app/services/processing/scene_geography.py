"""Turn the SAFE manifest footprint into facts a reader can act on.

A Sentinel-1 GRD measurement band is distributed in radar geometry and carries no
CRS, so the raster alone cannot say where on Earth a scene sits.  The manifest
footprint is the only georeference these products ship with, and it was already
being parsed -- once, for the land-cover domain check -- and then discarded.
Chat never told the user where the scene was or how much ground it covered, which
are the two questions a person asks first.

Ground extent is measured between the footprint corners rather than derived from
pixel count times an assumed 10 m spacing, so it stays correct for any product
type.  On S1D_IW_GRDH_1SDV_20260630T003057 the two agree (255 x 170 km against
25523 x 16749 px), which is the check that the corner ordering is being read the
way the manifest writes it.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


_EARTH_RADIUS_KM = 6371.0088


@dataclass(frozen=True)
class Footprint:
    """A scene's ground footprint, as four corners on the WGS84 sphere."""

    corners: tuple[tuple[float, float], ...]
    centroid_latitude: float
    centroid_longitude: float
    min_latitude: float
    max_latitude: float
    min_longitude: float
    max_longitude: float
    width_km: float
    height_km: float


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def bbox_extent_km(
    west: float, south: float, east: float, north: float
) -> tuple[float, float]:
    """Return the (width, height) of a search rectangle in kilometres.

    Reuses the haversine above so the server's AOI area limit and the figure
    the map shows a user are derived the same way and cannot disagree. Width is
    measured across the middle latitude: at the top of a tall box the parallel
    is shorter, and using a corner would over- or under-state the area by that
    ratio.
    """
    middle_latitude = (south + north) / 2.0
    width = _haversine_km((middle_latitude, west), (middle_latitude, east))
    height = _haversine_km((south, west), (north, west))
    return width, height


def parse_footprint(bounding_box: Any) -> Footprint | None:
    """Read the manifest footprint, which lists space-separated 'lat,lon' corners.

    Returns ``None`` rather than raising for anything unparseable: the three bare
    GeoTIFF scenes have no footprint at all, and a scene must still describe
    itself without one.
    """
    if not isinstance(bounding_box, str) or not bounding_box.strip():
        return None
    corners: list[tuple[float, float]] = []
    for corner in bounding_box.split():
        parts = corner.split(",")
        if len(parts) != 2:
            return None
        try:
            latitude, longitude = float(parts[0]), float(parts[1])
        except ValueError:
            return None
        if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
            return None
        corners.append((latitude, longitude))
    if len(corners) < 3:
        return None

    latitudes = [corner[0] for corner in corners]
    longitudes = [corner[1] for corner in corners]

    # Opposite sides of the quadrilateral are averaged so a slightly skewed
    # footprint -- every real one is skewed, the orbit is not axis-aligned --
    # reports one extent per axis instead of four edge lengths.
    sides = [_haversine_km(corners[i], corners[(i + 1) % len(corners)]) for i in range(len(corners))]
    if len(corners) == 4:
        first, second = (sides[0] + sides[2]) / 2, (sides[1] + sides[3]) / 2
    else:
        first, second = max(sides), min(sides)

    return Footprint(
        corners=tuple(corners),
        centroid_latitude=sum(latitudes) / len(latitudes),
        centroid_longitude=sum(longitudes) / len(longitudes),
        min_latitude=min(latitudes),
        max_latitude=max(latitudes),
        min_longitude=min(longitudes),
        max_longitude=max(longitudes),
        width_km=max(first, second),
        height_km=min(first, second),
    )


def format_coordinate(latitude: float, longitude: float) -> str:
    """Render a point the way a chart does, not the way a database does."""
    return (
        f"{abs(latitude):.2f}°{'N' if latitude >= 0 else 'S'}, "
        f"{abs(longitude):.2f}°{'E' if longitude >= 0 else 'W'}"
    )


def footprint_payload(footprint: Footprint | None) -> dict[str, Any] | None:
    """The compact form carried on scene context and into the model prompt."""
    if footprint is None:
        return None
    return {
        "centroid": {
            "latitude": round(footprint.centroid_latitude, 4),
            "longitude": round(footprint.centroid_longitude, 4),
        },
        "latitude_range": [round(footprint.min_latitude, 4), round(footprint.max_latitude, 4)],
        "longitude_range": [round(footprint.min_longitude, 4), round(footprint.max_longitude, 4)],
        "ground_extent_km": [round(footprint.width_km), round(footprint.height_km)],
    }
