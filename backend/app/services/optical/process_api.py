"""Sentinel-2 L2A for one date of an optical run, rendered by the Process API.

Exactly the request the Phase 0 model was checked against in its parity test:
* 12 bands in OlmoEarth's order plus the scene classification layer (SCL);
* DN units with harmonizeValues, so the baseline-04.00 offset is removed and
  2021 training data and today's imagery share one scale (DN = 10000 x reflectance);
* nearest-neighbour onto the run's exact UTM 10 m grid;
* pinned to one pass: the time window is +/-30 minutes around the chosen
  product's sensing start, so Sentinel Hub cannot mosaic other dates in.

The response is re-written as a DEFLATE-compressed GeoTIFF with band
descriptions before it is stored.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import time
from typing import Any

import httpx

from app.core.config import settings
from app.services.acquisitions import copernicus
from app.services.acquisitions.copernicus import (
    CopernicusAuthError,
    CopernicusError,
    CopernicusUnavailableError,
)
from app.services.optical.grid import OpticalGrid

logger = logging.getLogger(__name__)

# Modality.SENTINEL2_L2A.band_order in olmoearth-pretrain-minimal 0.1.2.
S2_BANDS = ["B02", "B03", "B04", "B08", "B05", "B06", "B07", "B8A", "B11", "B12", "B01", "B09"]
# 0 nodata, 1 saturated, 3 cloud shadow, 8/9 cloud, 10 cirrus, 11 snow.
SCL_INVALID = (0, 1, 3, 8, 9, 10, 11)
PIN_MINUTES = 30

EVALSCRIPT = """//VERSION=3
function setup() {
  return {
    input: [{
      bands: ["B02","B03","B04","B08","B05","B06","B07","B8A","B11","B12","B01","B09","SCL","dataMask"],
      units: "DN"
    }],
    output: { bands: 13, sampleType: "UINT16" }
  };
}
function evaluatePixel(s) {
  if (s.dataMask === 0) { return [0,0,0,0,0,0,0,0,0,0,0,0,0]; }
  return [s.B02,s.B03,s.B04,s.B08,s.B05,s.B06,s.B07,s.B8A,s.B11,s.B12,s.B01,s.B09,s.SCL];
}"""


def request_body(grid: OpticalGrid, sensing_start: datetime) -> dict[str, Any]:
    if sensing_start.tzinfo is None:
        sensing_start = sensing_start.replace(tzinfo=timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    low = (sensing_start - timedelta(minutes=PIN_MINUTES)).astimezone(timezone.utc).strftime(fmt)
    high = (sensing_start + timedelta(minutes=PIN_MINUTES)).astimezone(timezone.utc).strftime(fmt)
    left, bottom, right, top = grid.bounds
    return {
        "input": {
            "bounds": {
                "bbox": [left, bottom, right, top],
                "properties": {"crs": f"http://www.opengis.net/def/crs/EPSG/0/{grid.epsg}"},
            },
            "data": [
                {
                    "type": "sentinel-2-l2a",
                    "dataFilter": {"timeRange": {"from": low, "to": high}, "mosaickingOrder": "leastCC"},
                    "processing": {"upsampling": "NEAREST", "downsampling": "NEAREST", "harmonizeValues": True},
                }
            ],
        },
        "output": {
            "width": grid.width,
            "height": grid.height,
            "responses": [{"identifier": "default", "format": {"type": "image/tiff"}}],
        },
        "evalscript": EVALSCRIPT,
    }


def _detail(response: httpx.Response) -> str:
    if "json" in (response.headers.get("content-type") or "") or response.status_code >= 400:
        try:
            return response.text[:300].replace("\n", " ").strip()
        except Exception:
            return "(unreadable body)"
    return ""


def fetch_date(grid: OpticalGrid, sensing_start: datetime, destination: Path) -> dict[str, Any]:
    """Render one date onto the grid and write it to ``destination``.

    Returns size, processing units spent and the clear (SCL-valid) fraction.
    """
    body = request_body(grid, sensing_start)
    token = copernicus.access_token()
    attempts = max(1, settings.COPERNICUS_SUBSET_MAX_ATTEMPTS)
    payload: bytes | None = None
    response: httpx.Response | None = None
    last_detail = ""
    with copernicus._http_client(settings.COPERNICUS_SUBSET_TIMEOUT_SECONDS) as client:
        for attempt in range(1, attempts + 1):
            response = client.post(settings.COPERNICUS_PROCESS_URL, headers={"Authorization": f"Bearer {token}"}, json=body)
            if response.status_code in (401, 403):
                token = copernicus.access_token(force_refresh=True)
                response = client.post(settings.COPERNICUS_PROCESS_URL, headers={"Authorization": f"Bearer {token}"}, json=body)
            if response.status_code == 200:
                payload = response.content
                break
            if response.status_code in (401, 403):
                raise CopernicusAuthError(f"Sentinel Hub rejected the configured credentials. {_detail(response)}".strip())
            last_detail = _detail(response)
            if not (response.status_code == 429 or response.status_code >= 500):
                raise CopernicusError(f"Sentinel Hub rejected the Sentinel-2 request ({response.status_code}). {last_detail}".strip())
            if attempt < attempts:
                delay = settings.COPERNICUS_SUBSET_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
                logger.warning("Sentinel Hub returned %s for a Sentinel-2 request; retrying in %ss (%s/%s). %s",
                               response.status_code, delay, attempt, attempts, last_detail)
                time.sleep(delay)
    if payload is None:
        raise CopernicusUnavailableError(
            f"Sentinel Hub returned {response.status_code if response else 'no response'} for the "
            f"Sentinel-2 request after {attempts} attempts. {last_detail}".strip()
        )

    pu = response.headers.get("x-processingunits-spent")
    try:
        processing_units = round(float(pu), 4) if pu is not None else None
    except ValueError:
        processing_units = None

    raw = destination.with_suffix(".raw.tif")
    destination.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(payload)
    try:
        stats = _rewrite_checked(raw, destination, grid)
    finally:
        raw.unlink(missing_ok=True)
    return {"path": destination, "bytes": destination.stat().st_size, "processing_units": processing_units, **stats}


def _rewrite_checked(raw: Path, destination: Path, grid: OpticalGrid) -> dict[str, Any]:
    """Refuse anything not on the requested grid, then store it compressed."""
    import numpy as np
    import rasterio

    with rasterio.open(raw) as src:
        if src.count != 13:
            raise CopernicusError(f"Sentinel Hub returned {src.count} bands; expected 12 + SCL.")
        if (src.width, src.height) != (grid.width, grid.height):
            raise CopernicusError(f"Sentinel Hub returned {src.width}x{src.height} px; expected {grid.width}x{grid.height}.")
        epsg = src.crs.to_epsg() if src.crs else None
        tr = tuple(src.transform)[:6]
        if epsg != grid.epsg or max(abs(a - b) for a, b in zip(tr, grid.transform)) > 1e-3:
            raise CopernicusError("Sentinel Hub returned a different grid than requested.")
        data = src.read()
        profile = src.profile
    scl = data[12]
    valid = (~np.isin(scl, SCL_INVALID)) & (data[0] > 0)
    profile.update(driver="GTiff", compress="deflate", predictor=2, dtype="uint16", nodata=None)
    # Drop whatever block layout the response came with; tile only when a
    # 256 px tile fits, since the smallest grid is 128 px.
    profile.pop("blockxsize", None)
    profile.pop("blockysize", None)
    profile["tiled"] = grid.width >= 256 and grid.height >= 256
    if profile["tiled"]:
        profile.update(blockxsize=256, blockysize=256)
    with rasterio.open(destination, "w", **profile) as dst:
        dst.write(data)
        for i, name in enumerate(S2_BANDS + ["SCL"], start=1):
            dst.set_band_description(i, name)
        dst.update_tags(UNITS="DN = 10000 x surface reflectance, harmonized", BAND_ORDER=",".join(S2_BANDS + ["SCL"]))
    return {
        "width": grid.width,
        "height": grid.height,
        "valid_fraction": round(float(valid.mean()), 4),
        "nodata_fraction": round(float((scl == 0).mean()), 4),
    }
