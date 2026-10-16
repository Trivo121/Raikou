"""Offline tests for the Sentinel-2 L2A catalogue client. No network, no quota.

Requests go through the same ``copernicus._http_client`` seam as the SAR
client's tests. ``cdse_s2_search.json`` is hand-built: one pass in two
processing baselines, a second pass whose footprint only half covers the box,
and a Sentinel-1 product that must never come through.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.core.config import settings
from app.services.acquisitions import copernicus, sentinel2

FIXTURES = Path(__file__).parent / "fixtures"
BBOX = copernicus.BoundingBox(west=83.25, south=17.60, east=83.34, north=17.77)
START = datetime(2026, 4, 1, tzinfo=timezone.utc)
END = datetime(2026, 5, 20, tzinfo=timezone.utc)
NEWEST_BASELINE_ID = "6f1c3d2a-0b4e-4c55-9a1d-2b3c4d5e6f70"
SECOND_PASS_ID = "6f1c3d2a-0b4e-4c55-9a1d-2b3c4d5e6f72"


def _fixture() -> dict:
    return json.loads((FIXTURES / "cdse_s2_search.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _configured_provider(monkeypatch):
    monkeypatch.setattr(settings, "COPERNICUS_ENABLED", True)
    monkeypatch.setattr(settings, "COPERNICUS_CLIENT_ID", "test-client")
    monkeypatch.setattr(settings, "COPERNICUS_CLIENT_SECRET", "test-secret")
    copernicus.reset_token_cache()
    yield
    copernicus.reset_token_cache()


def _install(monkeypatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(recording)

    def factory(read_seconds: float, *, follow_redirects: bool = False) -> httpx.Client:
        return httpx.Client(transport=transport, follow_redirects=follow_redirects)

    monkeypatch.setattr(copernicus, "_http_client", factory)
    return seen


def _search(monkeypatch, body: dict | None = None, **overrides):
    payload = _fixture() if body is None else body
    seen = _install(monkeypatch, lambda request: httpx.Response(200, json=payload))
    arguments = {"bbox": BBOX, "start": START, "end": END, "max_cloud": 20, "limit": 30, **overrides}
    return sentinel2.search(**arguments), seen


def _filter(request: httpx.Request) -> str:
    return parse_qs(urlsplit(str(request.url)).query)["$filter"][0]


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------


def test_search_filter_hard_locks_the_l2a_contract(monkeypatch):
    _, seen = _search(monkeypatch)

    odata_filter = _filter(seen[0])
    assert "Collection/Name eq 'SENTINEL-2'" in odata_filter
    assert "att/OData.CSC.StringAttribute/Value eq 'S2MSI2A'" in odata_filter
    assert "att/OData.CSC.DoubleAttribute/Value le 20.00" in odata_filter
    assert "SENTINEL-1" not in odata_filter
    assert "S2MSI1C" not in odata_filter


@pytest.mark.parametrize(("asked", "sent"), [(150, "le 100.00"), (-5, "le 0.00"), (12.5, "le 12.50")])
def test_search_clamps_the_cloud_bound(monkeypatch, asked, sent):
    _, seen = _search(monkeypatch, body={"value": []}, max_cloud=asked)

    assert sent in _filter(seen[0])


def test_search_drops_other_products_and_keeps_the_newest_baseline(monkeypatch):
    results, _ = _search(monkeypatch)

    # Newest pass first; the N0500 copy of the same pass and the S1 entry are gone.
    assert [product.product_id for product in results] == [NEWEST_BASELINE_ID, SECOND_PASS_ID]
    assert results[0].processing_baseline == "05.11"
    assert all("_MSIL2A_" in product.name for product in results)


def test_search_reads_tile_platform_and_baseline_from_attributes_or_name(monkeypatch):
    (first, second), _ = _search(monkeypatch)

    assert (first.tile_id, first.platform, first.relative_orbit) == ("44QQE", "S2B", 19)
    assert first.cloud_cover == pytest.approx(3.21)
    assert first.sensing_start == datetime(2026, 4, 12, 5, 6, 49, 24000, tzinfo=timezone.utc)
    assert first.size_bytes == 1093742311
    # The second entry has no tileId, processingBaseline or orbit attribute.
    assert (second.tile_id, second.platform, second.processing_baseline) == ("44QQE", "S2C", "05.11")
    assert second.relative_orbit is None


def test_footprint_coverage_needs_all_four_corners(monkeypatch):
    (first, second), _ = _search(monkeypatch)

    assert first.covers(BBOX) is True
    assert second.covers(BBOX) is False
    assert dataclasses.replace(first, footprint=None).covers(BBOX) is None
    multipolygon = {"type": "MultiPolygon", "coordinates": [first.footprint["coordinates"]]}
    assert dataclasses.replace(first, footprint=multipolygon).covers(BBOX) is True


def test_search_maps_throttling_to_a_retryable_error(monkeypatch):
    _install(monkeypatch, lambda request: httpx.Response(429, text="slow down"))

    with pytest.raises(copernicus.CopernicusUnavailableError):
        sentinel2.search(bbox=BBOX, start=START, end=END, max_cloud=20, limit=30)


def test_search_maps_a_rejection_to_a_permanent_error(monkeypatch):
    _install(monkeypatch, lambda request: httpx.Response(400, text="bad filter"))

    with pytest.raises(copernicus.CopernicusError) as info:
        sentinel2.search(bbox=BBOX, start=START, end=END, max_cloud=20, limit=30)

    assert not isinstance(info.value, copernicus.CopernicusUnavailableError)


# --------------------------------------------------------------------------
# Single product lookup (accept time never trusts browser-sent values)
# --------------------------------------------------------------------------


def test_get_rejects_a_non_uuid_id_before_any_request(monkeypatch):
    seen = _install(monkeypatch, lambda request: httpx.Response(200, json={}))

    with pytest.raises(copernicus.CopernicusProductNotFoundError):
        sentinel2.get("../../etc/passwd")

    assert seen == []


def test_get_reads_the_entity_shape(monkeypatch):
    entry = _fixture()["value"][0]
    seen = _install(monkeypatch, lambda request: httpx.Response(200, json=entry))

    product = sentinel2.get(NEWEST_BASELINE_ID)

    assert product.product_id == NEWEST_BASELINE_ID
    assert product.tile_id == "44QQE"
    assert NEWEST_BASELINE_ID in str(seen[0].url)


def test_get_refuses_a_product_outside_the_contract(monkeypatch):
    sentinel1 = _fixture()["value"][3]
    _install(monkeypatch, lambda request: httpx.Response(200, json=sentinel1))

    with pytest.raises(copernicus.CopernicusError, match="not a Sentinel-2 L2A"):
        sentinel2.get(sentinel1["Id"])


def test_get_maps_404(monkeypatch):
    _install(monkeypatch, lambda request: httpx.Response(404, text="not found"))

    with pytest.raises(copernicus.CopernicusProductNotFoundError):
        sentinel2.get(NEWEST_BASELINE_ID)
