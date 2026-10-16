"""Sentinel-2 L2A search on the CDSE OData catalogue, for optical runs.

Kept apart from ``copernicus.py`` on purpose: that module hard-locks the SAR
pipeline's product contract (IW GRDH, VV+VH), and that lock must stay a
constant rather than become a parameter. This module locks its own contract,
L2A (surface reflectance with a scene classification layer), the same way:
in the filter, again on every parsed result, and again in the table CHECK.

It reuses the SAR client's token cache, HTTP seam and parsing helpers, so one
credential, one timeout policy and one test transport cover both.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import re
from typing import Any

import httpx

from app.core.config import settings
from app.services.acquisitions import copernicus
from app.services.acquisitions.copernicus import (
    BoundingBox,
    CopernicusError,
    CopernicusProductNotFoundError,
    CopernicusUnavailableError,
    _attribute_map,
    _parse_timestamp,
    _safe_product_id,
)

COLLECTION = "SENTINEL-2"
PRODUCT_TYPE = "S2MSI2A"
PRODUCT_NAME_PATTERN = re.compile(
    r"^S2[A-D]_MSIL2A_\d{8}T\d{6}_N\d{4}_R\d{3}_T\d{2}[A-Z]{3}_\d{8}T\d{6}(\.SAFE)?$"
)


@dataclass(frozen=True, slots=True)
class S2Product:
    product_id: str
    name: str
    sensing_start: datetime | None
    cloud_cover: float | None
    tile_id: str | None
    platform: str | None
    relative_orbit: int | None
    processing_baseline: str | None
    online: bool
    size_bytes: int | None
    footprint: dict[str, Any] | None

    def covers(self, bbox: BoundingBox) -> bool | None:
        """Whether the footprint contains all four corners of the box.

        None when there is no usable footprint. Pure Python on purpose: the API
        venv has no geometry library, and four point-in-polygon tests do not
        justify one.
        """
        rings = _outer_rings(self.footprint)
        if not rings:
            return None
        corners = (
            (bbox.west, bbox.south),
            (bbox.east, bbox.south),
            (bbox.east, bbox.north),
            (bbox.west, bbox.north),
        )
        return all(any(_point_in_ring(x, y, ring) for ring in rings) for x, y in corners)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["sensing_start"] = self.sensing_start.isoformat() if self.sensing_start else None
        return data


def _outer_rings(footprint: dict[str, Any] | None) -> list[list[tuple[float, float]]]:
    if not isinstance(footprint, dict):
        return []
    kind, coords = footprint.get("type"), footprint.get("coordinates")
    try:
        if kind == "Polygon":
            polygons = [coords]
        elif kind == "MultiPolygon":
            polygons = list(coords)
        else:
            return []
        return [[(float(p[0]), float(p[1])) for p in polygon[0]] for polygon in polygons if polygon]
    except (TypeError, ValueError, IndexError):
        return []


def _point_in_ring(x: float, y: float, ring: list[tuple[float, float]]) -> bool:
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            x_cross = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < x_cross:
                inside = not inside
    return inside


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def product_from_odata(entry: dict[str, Any]) -> S2Product | None:
    product_id, name = entry.get("Id"), entry.get("Name")
    if not isinstance(product_id, str) or not isinstance(name, str):
        return None
    attributes = _attribute_map(entry)
    size = entry.get("ContentLength")
    size = _int(size)
    if size is not None and size <= 0:
        size = None
    content = entry.get("ContentDate") if isinstance(entry.get("ContentDate"), dict) else {}
    serial = attributes.get("platformSerialIdentifier")
    platform = f"S2{str(serial).strip().upper()}" if serial not in (None, "") else name[:3]
    baseline = attributes.get("processingBaseline")
    if baseline in (None, ""):
        match = re.search(r"_N(\d{2})(\d{2})_", name)
        baseline = f"{match.group(1)}.{match.group(2)}" if match else None
    tile = attributes.get("tileId")
    if tile in (None, ""):
        match = re.search(r"_T(\d{2}[A-Z]{3})_", name)
        tile = match.group(1) if match else None
    footprint = entry.get("GeoFootprint")
    return S2Product(
        product_id=product_id,
        name=name,
        sensing_start=_parse_timestamp(content.get("Start")),
        cloud_cover=_float(attributes.get("cloudCover")),
        tile_id=str(tile) if tile else None,
        platform=platform,
        relative_orbit=_int(attributes.get("relativeOrbitNumber")),
        processing_baseline=str(baseline) if baseline not in (None, "") else None,
        online=entry.get("Online") is not False,
        size_bytes=size,
        footprint=footprint if isinstance(footprint, dict) else None,
    )


def _usable(product: S2Product | None) -> bool:
    """The contract, re-checked on every parsed result (defence in depth)."""
    return product is not None and bool(PRODUCT_NAME_PATTERN.match(product.name))


def build_filter(bbox: BoundingBox, start: datetime, end: datetime, max_cloud: float) -> str:
    """Hard-locked to L2A. Only validated floats and dates are interpolated."""
    start_text = start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_text = end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.999Z")
    cloud = min(100.0, max(0.0, float(max_cloud)))
    return (
        f"Collection/Name eq '{COLLECTION}'"
        f" and OData.CSC.Intersects(area=geography'SRID=4326;{bbox.as_wkt_polygon()}')"
        f" and ContentDate/Start ge {start_text}"
        f" and ContentDate/Start le {end_text}"
        " and Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'productType'"
        f" and att/OData.CSC.StringAttribute/Value eq '{PRODUCT_TYPE}')"
        " and Attributes/OData.CSC.DoubleAttribute/any(att:att/Name eq 'cloudCover'"
        f" and att/OData.CSC.DoubleAttribute/Value le {cloud:.2f})"
    )


def search(*, bbox: BoundingBox, start: datetime, end: datetime, max_cloud: float, limit: int) -> list[S2Product]:
    url = f"{settings.COPERNICUS_CATALOGUE_URL}/Products"
    params = {
        "$filter": build_filter(bbox, start, end, max_cloud),
        "$expand": "Attributes",
        "$orderby": "ContentDate/Start desc",
        "$top": str(int(limit)),
    }
    try:
        # Through the module attribute, so the SAR tests' transport seam
        # (monkeypatching copernicus._http_client) covers this module too.
        with copernicus._http_client(settings.COPERNICUS_SEARCH_TIMEOUT_SECONDS) as client:
            response = client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise CopernicusUnavailableError("The Copernicus catalogue is unreachable.") from exc
    if response.status_code == 429 or response.status_code >= 500:
        raise CopernicusUnavailableError(f"The Copernicus catalogue returned {response.status_code}.")
    if response.status_code != 200:
        raise CopernicusError(f"The Copernicus catalogue rejected the search with status {response.status_code}.")
    try:
        body = response.json()
    except ValueError as exc:
        raise CopernicusError("The Copernicus catalogue response was not JSON.") from exc
    raw = body.get("value") if isinstance(body, dict) else None
    if not isinstance(raw, list):
        raise CopernicusError("The Copernicus catalogue response contained no result list.")
    results = [p for p in (product_from_odata(e) for e in raw if isinstance(e, dict)) if _usable(p)]
    # Same acquisition, two processing baselines: keep the newest baseline only.
    newest: dict[tuple, S2Product] = {}
    for p in results:
        key = (p.platform, p.sensing_start, p.tile_id)
        kept = newest.get(key)
        if kept is None or (p.processing_baseline or "") > (kept.processing_baseline or ""):
            newest[key] = p
    return sorted(newest.values(), key=lambda p: p.sensing_start or datetime.min.replace(tzinfo=timezone.utc), reverse=True)


def get(product_id: str) -> S2Product:
    """Re-fetch one product so accept-time never trusts browser-sent values."""
    url = f"{settings.COPERNICUS_CATALOGUE_URL}/Products({_safe_product_id(product_id)})"
    try:
        with copernicus._http_client(settings.COPERNICUS_SEARCH_TIMEOUT_SECONDS) as client:
            response = client.get(url, params={"$expand": "Attributes"})
    except httpx.HTTPError as exc:
        raise CopernicusUnavailableError("The Copernicus catalogue is unreachable.") from exc
    if response.status_code == 404:
        raise CopernicusProductNotFoundError("That Copernicus product no longer exists.")
    if response.status_code == 429 or response.status_code >= 500:
        raise CopernicusUnavailableError(f"The Copernicus catalogue returned {response.status_code}.")
    if response.status_code != 200:
        raise CopernicusError(f"The Copernicus catalogue rejected the lookup with status {response.status_code}.")
    try:
        body = response.json()
    except ValueError as exc:
        raise CopernicusError("The Copernicus catalogue response was not JSON.") from exc
    entry = None
    if isinstance(body, dict) and isinstance(body.get("Id"), str):
        entry = body
    elif isinstance(body, dict) and isinstance(body.get("value"), list):
        entry = next((e for e in body["value"] if isinstance(e, dict) and isinstance(e.get("Id"), str)), None)
    product = product_from_odata(entry) if entry else None
    if product is None:
        raise CopernicusProductNotFoundError("That Copernicus product no longer exists.")
    if not _usable(product):
        raise CopernicusError("That product is not a Sentinel-2 L2A product.")
    return product
