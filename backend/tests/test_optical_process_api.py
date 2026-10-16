"""The Sentinel-2 Process API request, and the checks on what comes back. Offline.

Responses are real GeoTIFFs built in memory, served through the same
``copernicus._http_client`` seam as the catalogue tests.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re

import httpx
import numpy as np
import pytest
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import Affine

from app.core.config import settings
from app.services.acquisitions import copernicus
from app.services.optical import process_api
from app.services.optical.grid import OpticalGrid

# Width and height differ so a transposed array cannot pass.
GRID = OpticalGrid(epsg=32644, x0=735120.0, y_top=1962000.0, width=128, height=136)
SENSING = datetime(2026, 4, 12, 5, 6, 49, 24000, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _configured_provider(monkeypatch):
    monkeypatch.setattr(settings, "COPERNICUS_ENABLED", True)
    monkeypatch.setattr(settings, "COPERNICUS_CLIENT_ID", "test-client")
    monkeypatch.setattr(settings, "COPERNICUS_CLIENT_SECRET", "test-secret")
    copernicus.reset_token_cache()
    yield
    copernicus.reset_token_cache()


def _tiff(*, grid: OpticalGrid = GRID, count: int = 13, epsg: int | None = None, x0: float | None = None,
          cloud_cols: int = 0) -> bytes:
    data = np.full((count, grid.height, grid.width), 900, np.uint16)
    data[0] = 1200
    if count == 13:
        data[12] = 4  # SCL vegetation: clear
        data[12, :, :cloud_cols] = 9  # SCL cloud, high probability
    transform = Affine(*grid.transform) if x0 is None else Affine(10.0, 0.0, x0, 0.0, -10.0, grid.y_top)
    with MemoryFile() as memory:
        with memory.open(driver="GTiff", width=grid.width, height=grid.height, count=count, dtype="uint16",
                         crs=f"EPSG:{epsg or grid.epsg}", transform=transform) as dataset:
            dataset.write(data)
        return memory.read()


def _install_raw(monkeypatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(recording)

    def factory(read_seconds: float, *, follow_redirects: bool = False) -> httpx.Client:
        return httpx.Client(transport=transport, follow_redirects=follow_redirects)

    monkeypatch.setattr(copernicus, "_http_client", factory)
    return seen


def _install(monkeypatch, process_handler) -> list[httpx.Request]:
    """Mint tokens normally and route every Process API call to ``process_handler``."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "token-abc", "expires_in": 600})
        return process_handler(request)

    return _install_raw(monkeypatch, handler)


def _process_calls(seen: list[httpx.Request]) -> list[httpx.Request]:
    return [request for request in seen if not request.url.path.endswith("/token")]


# --------------------------------------------------------------------------
# The request
# --------------------------------------------------------------------------


def test_request_is_pinned_to_one_pass_on_the_exact_grid():
    body = process_api.request_body(GRID, SENSING)
    source = body["input"]["data"][0]

    assert source["type"] == "sentinel-2-l2a"
    assert source["dataFilter"]["timeRange"] == {"from": "2026-04-12T04:36:49Z", "to": "2026-04-12T05:36:49Z"}
    assert source["processing"] == {"upsampling": "NEAREST", "downsampling": "NEAREST", "harmonizeValues": True}
    assert body["input"]["bounds"]["bbox"] == list(GRID.bounds)
    assert body["input"]["bounds"]["properties"]["crs"].endswith("/EPSG/0/32644")
    assert (body["output"]["width"], body["output"]["height"]) == (128, 136)


def test_a_naive_sensing_time_is_read_as_utc():
    assert process_api.request_body(GRID, SENSING.replace(tzinfo=None)) == process_api.request_body(GRID, SENSING)


def test_the_evalscript_returns_the_model_band_order_then_scl():
    script = process_api.EVALSCRIPT
    requested = re.search(r"bands: \[([^\]]+)\]", script).group(1)
    returned = re.search(r"return \[(s\.[^\]]+)\]", script).group(1)

    assert [b.strip().strip('"') for b in requested.split(",")] == process_api.S2_BANDS + ["SCL", "dataMask"]
    assert [b.strip()[2:] for b in returned.split(",")] == process_api.S2_BANDS + ["SCL"]
    assert re.search(r"return \[(0,){12}0\]", script), "no-data pixels must return 13 zeros"
    assert 'units: "DN"' in script and "bands: 13" in script and 'sampleType: "UINT16"' in script


# --------------------------------------------------------------------------
# The response
# --------------------------------------------------------------------------


@pytest.mark.parametrize("grid", [GRID, OpticalGrid(epsg=32644, x0=735120.0, y_top=1962000.0, width=264, height=256)])
def test_fetch_writes_a_checked_compressed_geotiff(monkeypatch, tmp_path, grid):
    seen = _install(
        monkeypatch,
        lambda request: httpx.Response(
            200, content=_tiff(grid=grid, cloud_cols=grid.width // 4),
            headers={"content-type": "image/tiff", "x-processingunits-spent": "0.2708"},
        ),
    )
    destination = tmp_path / "s2_t0.tif"

    got = process_api.fetch_date(grid, SENSING, destination)

    assert got["processing_units"] == pytest.approx(0.2708)
    assert got["valid_fraction"] == pytest.approx(0.75)
    assert got["nodata_fraction"] == 0.0
    assert (got["width"], got["height"]) == (grid.width, grid.height)
    assert not destination.with_suffix(".raw.tif").exists()
    with rasterio.open(destination) as dataset:
        assert dataset.count == 13 and dataset.crs.to_epsg() == 32644
        assert tuple(dataset.transform)[:6] == grid.transform
        assert list(dataset.descriptions) == process_api.S2_BANDS + ["SCL"]
        assert dataset.tags()["BAND_ORDER"].split(",") == process_api.S2_BANDS + ["SCL"]
        assert dataset.profile["compress"].lower() == "deflate"
        # 256 px tiles only where one fits; the smallest grid is 128 px.
        if grid.width >= 256 and grid.height >= 256:
            assert dataset.block_shapes[0] == (256, 256)
        else:
            assert dataset.block_shapes[0][1] == grid.width
    calls = _process_calls(seen)
    assert len(calls) == 1
    assert calls[0].headers["authorization"] == "Bearer token-abc"
    assert json.loads(calls[0].content)["output"]["width"] == grid.width


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"count": 12}, "12 bands"),
        ({"grid": OpticalGrid(epsg=32644, x0=735120.0, y_top=1962000.0, width=64, height=64)}, "64x64 px"),
        ({"epsg": 32643}, "different grid"),
        ({"x0": 735180.0}, "different grid"),
    ],
)
def test_fetch_refuses_anything_off_the_requested_grid(monkeypatch, tmp_path, payload, message):
    _install(monkeypatch, lambda request: httpx.Response(200, content=_tiff(**payload)))
    destination = tmp_path / "s2_t0.tif"

    with pytest.raises(copernicus.CopernicusError, match=message):
        process_api.fetch_date(GRID, SENSING, destination)

    assert not destination.exists()
    assert not destination.with_suffix(".raw.tif").exists()


def test_fetch_refreshes_the_token_once_then_gives_up(monkeypatch, tmp_path):
    minted = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            minted["count"] += 1
            return httpx.Response(200, json={"access_token": f"token-{minted['count']}", "expires_in": 600})
        return httpx.Response(401, text="unauthorized")

    seen = _install_raw(monkeypatch, handler)

    with pytest.raises(copernicus.CopernicusAuthError):
        process_api.fetch_date(GRID, SENSING, tmp_path / "s2_t0.tif")

    assert minted["count"] == 2
    assert len(_process_calls(seen)) == 2


def test_fetch_retries_throttling_then_reports_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "COPERNICUS_SUBSET_MAX_ATTEMPTS", 3)
    sleeps: list[float] = []
    monkeypatch.setattr(process_api.time, "sleep", sleeps.append)
    seen = _install(monkeypatch, lambda request: httpx.Response(429, text="slow down"))

    with pytest.raises(copernicus.CopernicusUnavailableError):
        process_api.fetch_date(GRID, SENSING, tmp_path / "s2_t0.tif")

    assert len(_process_calls(seen)) == 3
    assert len(sleeps) == 2 and sleeps[1] == 2 * sleeps[0]


def test_fetch_recovers_after_one_server_error(monkeypatch, tmp_path):
    monkeypatch.setattr(process_api.time, "sleep", lambda seconds: None)
    responses = [httpx.Response(503, text="busy"), httpx.Response(200, content=_tiff())]
    _install(monkeypatch, lambda request: responses.pop(0))

    got = process_api.fetch_date(GRID, SENSING, tmp_path / "s2_t0.tif")

    assert got["valid_fraction"] == 1.0
    # No x-processingunits-spent header: unknown, never guessed.
    assert got["processing_units"] is None


def test_fetch_does_not_retry_a_rejected_request(monkeypatch, tmp_path):
    seen = _install(monkeypatch, lambda request: httpx.Response(400, json={"error": {"message": "bad bbox"}}))

    with pytest.raises(copernicus.CopernicusError) as info:
        process_api.fetch_date(GRID, SENSING, tmp_path / "s2_t0.tif")

    assert not isinstance(info.value, copernicus.CopernicusUnavailableError)
    assert "bad bbox" in str(info.value)
    assert len(_process_calls(seen)) == 1
