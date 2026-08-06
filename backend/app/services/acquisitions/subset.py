"""Fetch just the area of interest, instead of the frame that contains it.

Sentinel-1 GRD is distributed as whole ~250x170 km frames, so the catalogue
download hands back 1.7 GB to analyse a city. Sentinel Hub's Process API will
instead render an arbitrary box out of the same acquisition: ~44 MB for a
25 km square, in seconds, already orthorectified and carrying a CRS the GRD
never had.

Three things about this path are deliberate and load-bearing:

Pixel size is pinned at 10 m and never traded away to fit a bigger box. It is
tempting to stretch a large area into the same 2500 px, but reBEN land cover
is trained on 120 px at 10 m, and at 40 m that window covers 4.8 km instead of
1.2 km -- the classifier would be scoring a scale it never saw and would return
confident nonsense rather than a weaker result. The scattering windows share
that geometry. So the Process API's 2500 px per-request ceiling becomes a real
25 km cap on the area, which is a product limit rather than a bug.

The request is pinned to one acquisition by narrowing the time range to the
instant the chosen product was taken. Without that, Sentinel Hub mosaics
whatever passes overhead in the window and the result would not be the scene
the user picked off the catalogue.

The response is written as two single-band files rather than one two-band file.
``_build_vrt_local`` exposes only band 1 of a single source unless it has three
or more bands, so a two-band GeoTIFF would silently lose VH and every dual-pol
consumer downstream would see one band and return None.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging
import math
import time
from pathlib import Path
from typing import Any

import httpx

from app.core.config import settings
from app.services.acquisitions.copernicus import (
    BoundingBox,
    CopernicusAuthError,
    CopernicusError,
    CopernicusProduct,
    CopernicusUnavailableError,
    _http_client,
    access_token,
)


logger = logging.getLogger(__name__)

# Two bands, float32, sigma0 in linear power. Linear rather than dB on purpose:
# the block-mean pass averages power, because averaging decibels biases the
# result low, so dB here would be converted straight back at every consumer.
_EVALSCRIPT = """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["VV", "VH"] }],
    output: { bands: 2, sampleType: "FLOAT32" }
  };
}
function evaluatePixel(sample) {
  return [sample.VV, sample.VH];
}"""


class SubsetTooLargeError(CopernicusError):
    """The requested area needs more pixels than one request may return."""


@dataclass(frozen=True, slots=True)
class SubsetPlan:
    """What a subset request will cost and cover, before it is made."""

    width_px: int
    height_px: int
    metres_per_pixel: float
    width_km: float
    height_km: float

    @property
    def megapixels(self) -> float:
        return (self.width_px * self.height_px) / 1e6

    def as_dict(self) -> dict[str, Any]:
        return {
            "width_px": self.width_px,
            "height_px": self.height_px,
            "metres_per_pixel": self.metres_per_pixel,
            "width_km": round(self.width_km, 2),
            "height_km": round(self.height_km, 2),
        }


def _span_km(bbox: BoundingBox) -> tuple[float, float]:
    """Ground span of the box. Equirectangular is well within a percent here."""
    radius_km = 6371.0
    rad = math.pi / 180.0
    mid_lat = ((bbox.north + bbox.south) / 2.0) * rad
    width = abs((bbox.east - bbox.west) * rad * radius_km * math.cos(mid_lat))
    height = abs((bbox.north - bbox.south) * rad * radius_km)
    return width, height


def plan_subset(bbox: BoundingBox) -> SubsetPlan:
    """Size a subset at native resolution, or refuse the area outright.

    Refusing is the honest option: silently coarsening the pixels to make a
    large area fit would break the land-cover and scattering window geometry
    without anything visible going wrong.
    """
    metres = float(settings.COPERNICUS_SUBSET_METRES_PER_PIXEL)
    limit = int(settings.COPERNICUS_SUBSET_MAX_PIXELS)
    width_km, height_km = _span_km(bbox)
    width_px = max(1, math.ceil((width_km * 1000.0) / metres))
    height_px = max(1, math.ceil((height_km * 1000.0) / metres))
    if width_px > limit or height_px > limit:
        max_km = (limit * metres) / 1000.0
        raise SubsetTooLargeError(
            f"This area is {width_km:.0f} x {height_km:.0f} km. A subset keeps the "
            f"native {metres:.0f} m resolution, which caps it at {max_km:.0f} km a side. "
            "Draw a smaller area, or fetch the whole frame instead."
        )
    return SubsetPlan(
        width_px=width_px,
        height_px=height_px,
        metres_per_pixel=metres,
        width_km=width_km,
        height_km=height_km,
    )


def _request_body(product: CopernicusProduct, bbox: BoundingBox, plan: SubsetPlan) -> dict[str, Any]:
    if product.sensing_start is None:
        raise CopernicusError("The selected product has no acquisition time to pin the subset to.")
    # A 30 s window either side comfortably contains one slice (they are ~25 s)
    # and excludes the neighbouring pass, so exactly one acquisition contributes.
    low = (product.sensing_start - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    high = (product.sensing_start + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "input": {
            "bounds": {
                "bbox": [bbox.west, bbox.south, bbox.east, bbox.north],
                "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"},
            },
            "data": [{
                "type": "sentinel-1-grd",
                "dataFilter": {
                    "timeRange": {"from": low, "to": high},
                    "acquisitionMode": "IW",
                    "polarization": "DV",
                    "resolution": "HIGH",
                },
                # Ellipsoid sigma0 rather than terrain-flattened gamma0: the
                # full-frame path applies no terrain correction either, so this
                # keeps the two routes radiometrically comparable.
                "processing": {"backCoeff": "SIGMA0_ELLIPSOID", "orthorectify": True},
            }],
        },
        "output": {
            "width": plan.width_px,
            "height": plan.height_px,
            "responses": [{"identifier": "default", "format": {"type": "image/tiff"}}],
        },
        "evalscript": _EVALSCRIPT,
    }


def fetch_subset(
    product: CopernicusProduct,
    bbox: BoundingBox,
    destination_dir: Path,
    *,
    plan: SubsetPlan | None = None,
) -> dict[str, Any]:
    """Render the area of interest and split it into one file per polarisation.

    Returns the written paths plus what it cost, for the acquisition record.
    """
    plan = plan or plan_subset(bbox)
    body = _request_body(product, bbox, plan)
    token = access_token()

    def _detail(response: httpx.Response) -> str:
        """A snippet of what the provider actually said.

        Worth the care: a 200 body is a TIFF, so decoding it as text produces
        noise. Reporting only the status code cost a live diagnosis once --
        the failure was transient and the reason was in the body we discarded.
        """
        if "json" in (response.headers.get("content-type") or "") or response.status_code >= 400:
            try:
                return response.text[:300].replace("\n", " ").strip()
            except Exception:
                return "(unreadable body)"
        return ""

    # A brief upstream wobble should not cost the task its retry budget. The
    # durable ladder backs off 5/10/20/40/80s and gives up after five, which a
    # 500 lasting a minute can exhaust before the provider recovers -- observed
    # doing exactly that. Riding out the blip here keeps those five attempts
    # for failures that are actually ours.
    attempts = max(1, settings.COPERNICUS_SUBSET_MAX_ATTEMPTS)
    last_detail = ""
    payload: bytes | None = None
    response: httpx.Response | None = None

    with _http_client(settings.COPERNICUS_SUBSET_TIMEOUT_SECONDS) as client:
        for attempt in range(1, attempts + 1):
            response = client.post(
                settings.COPERNICUS_PROCESS_URL,
                headers={"Authorization": f"Bearer {token}"},
                json=body,
            )
            if response.status_code in (401, 403):
                # The cached token may have aged out mid-flight on a long job.
                token = access_token(force_refresh=True)
                response = client.post(
                    settings.COPERNICUS_PROCESS_URL,
                    headers={"Authorization": f"Bearer {token}"},
                    json=body,
                )
            if response.status_code == 200:
                payload = response.content
                break
            if response.status_code in (401, 403):
                raise CopernicusAuthError(
                    f"Sentinel Hub rejected the configured credentials. {_detail(response)}".strip()
                )
            last_detail = _detail(response)
            retryable = response.status_code == 429 or response.status_code >= 500
            if not retryable:
                raise CopernicusError(
                    f"Sentinel Hub rejected the subset request ({response.status_code}). {last_detail}".strip()
                )
            if attempt < attempts:
                delay = settings.COPERNICUS_SUBSET_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "Sentinel Hub returned %s for the subset request; retrying in %ss (%s/%s). %s",
                    response.status_code, delay, attempt, attempts, last_detail,
                )
                time.sleep(delay)

    if payload is None:
        raise CopernicusUnavailableError(
            f"Sentinel Hub returned {response.status_code if response else 'no response'} for the "
            f"subset request after {attempts} attempts. {last_detail}".strip()
        )

    processing_units = _float_or_none(response.headers.get("x-processingunits-spent"))
    destination_dir.mkdir(parents=True, exist_ok=True)
    combined = destination_dir / "subset-2band.tif"
    combined.write_bytes(payload)

    try:
        written = _split_polarisations(combined, destination_dir, product)
    finally:
        combined.unlink(missing_ok=True)

    return {
        "rasters": written["paths"],
        "width_px": written["width"],
        "height_px": written["height"],
        "crs": written["crs"],
        "metres_per_pixel": plan.metres_per_pixel,
        "bytes": sum(path.stat().st_size for path in written["paths"]),
        "processing_units": processing_units,
        "plan": plan.as_dict(),
    }


def _float_or_none(value: Any) -> float | None:
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def _split_polarisations(
    combined: Path, destination_dir: Path, product: CopernicusProduct
) -> dict[str, Any]:
    """Write band 1 and band 2 as separate single-band GeoTIFFs.

    The names carry ``vv`` and ``vh`` because the worker orders bands by
    exactly that substring before building the VRT.
    """
    import rasterio

    stem = product.name.removesuffix(".SAFE")
    paths: list[Path] = []
    with rasterio.open(combined) as source:
        if source.count < 2:
            raise CopernicusError(
                f"Sentinel Hub returned {source.count} band(s); the analysis needs VV and VH."
            )
        profile = source.profile
        profile.update(count=1, driver="GTiff", compress="deflate", predictor=3)
        crs = str(source.crs) if source.crs else None
        width, height = source.width, source.height
        for index, polarisation in enumerate(("vv", "vh"), start=1):
            target = destination_dir / f"{stem}-{polarisation}.tif"
            with rasterio.open(target, "w", **profile) as sink:
                sink.write(source.read(index), 1)
                sink.update_tags(POLARISATION=polarisation.upper())
            paths.append(target)
    return {"paths": paths, "width": width, "height": height, "crs": crs}
