"""The similarity service behind POST /optical/runs/{id}/similar, against fakes."""

from __future__ import annotations

import base64
import inspect
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image
import pydantic
import pytest

from app.core.config import settings
from app.services.optical import embeddings as emb
from app.services.optical import similar
from app.services.optical.grid import OpticalGrid
from app.services.storage.payloads import OpticalWindowPayload
from app.services.storage.qdrant import QdrantStore

# 64 x 64 px = 16 x 16 tokens of 4 px over Visakhapatnam.
GRID = OpticalGrid(epsg=32644, x0=735120.0, y_top=1962000.0, width=64, height=64)
TOKEN_PX = 4


def _reduced() -> tuple[np.ndarray, np.ndarray]:
    """Left half one direction, right half an orthogonal one: similarity exactly 1 or 0."""
    y = np.zeros((16, 16, 8), np.float32)
    y[:, :8, 0] = 1.0
    y[:, 8:, 1] = 1.0
    valid = np.ones((16, 16), bool)
    valid[15, 15] = False
    y[~valid] = 0
    return y.astype(np.float16), valid


def _lonlat_of_pixel(row: float, col: float) -> tuple[float, float]:
    from app.services.optical.grid import _transform

    lons, lats = _transform(GRID.epsg, 4326, [GRID.x0 + col * GRID.res], [GRID.y_top - row * GRID.res])
    return lons[0], lats[0]


@pytest.fixture(autouse=True)
def _fresh_cache():
    similar.clear_cache()
    yield
    similar.clear_cache()


# --------------------------------------------------------------------------
# Grid helpers
# --------------------------------------------------------------------------


def test_a_clicked_point_maps_to_its_pixel_and_token():
    lon, lat = _lonlat_of_pixel(10.5, 37.5)

    assert GRID.pixel_of(lon, lat) == (10, 37)
    assert similar.seed_token(GRID, TOKEN_PX, lon, lat) == (2, 9)


def test_a_point_outside_the_run_is_refused():
    with pytest.raises(similar.SimilarError) as info:
        similar.seed_token(GRID, TOKEN_PX, 80.0, 17.7)

    assert info.value.status == 422


def test_window_boxes_contain_their_pixels():
    (box,) = GRID.bboxes_lonlat([(0, 64, 0, 64)])
    centre = _lonlat_of_pixel(32, 32)

    west, south, east, north = box
    assert west < centre[0] < east and south < centre[1] < north
    assert GRID.bboxes_lonlat([]) == []


# --------------------------------------------------------------------------
# Run heatmap
# --------------------------------------------------------------------------


def test_the_run_heatmap_is_a_png_on_the_token_grid():
    reduced, valid = _reduced()

    result = similar.run_heatmap(reduced, valid, 3, 2, grid=GRID, token_px=TOKEN_PX)

    image = result["image"]
    assert (image["width"], image["height"]) == (16, 16)
    assert image["corners_lonlat"] == GRID.corners_lonlat()
    assert image["value_range"] == [emb.VMIN, emb.VMAX]
    raw = base64.b64decode(image["data_url"].split(",", 1)[1])
    with Image.open(BytesIO(raw)) as png:
        assert png.size == (16, 16)
        assert png.getpixel((15, 15))[3] == 0  # no data stays transparent
    # The seed's half (8 x 16 tokens, 40 m each) looks alike; the other half does not.
    at_09 = next(a for a in result["areas"] if a["threshold"] == 0.9)
    assert at_09["area_sqkm"] == pytest.approx(128 * 0.0016)
    assert at_09["share_pct"] == pytest.approx(round(100 * 128 / 255, 1))


def test_a_seed_without_data_is_refused():
    reduced, valid = _reduced()

    with pytest.raises(similar.SimilarError) as info:
        similar.run_heatmap(reduced, valid, 15, 15, grid=GRID, token_px=TOKEN_PX)

    assert info.value.status == 422


class _Storage:
    def __init__(self, source: Path) -> None:
        self.source = source
        self.downloads = 0

    def download_file(self, key, destination):
        self.downloads += 1
        Path(destination).write_bytes(self.source.read_bytes())


def test_embeddings_are_downloaded_once_per_run_version(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OPTICAL_SIMILAR_CACHE_RUNS", 1)
    reduced, valid = _reduced()
    source = tmp_path / "embeddings.npz"
    np.savez(source, reduced=reduced, valid=valid)
    storage = _Storage(source)
    first = {"id": "a", "storage_key": "k", "checksum_sha256": "x"}

    got, got_valid = similar.load_embeddings(storage, first)
    similar.load_embeddings(storage, first)
    similar.load_embeddings(storage, {"id": "b", "storage_key": "k", "checksum_sha256": "y"})
    similar.load_embeddings(storage, first)  # evicted by "b"

    assert storage.downloads == 3
    assert np.array_equal(got, reduced) and np.array_equal(got_valid, valid)


# --------------------------------------------------------------------------
# Library search (Qdrant), with calls bound against the real store
# --------------------------------------------------------------------------


class _Store:
    def __init__(self, seed: dict | None, hits: list[dict]) -> None:
        self.seed, self.hits, self.calls = seed, hits, []

    def retrieve_owned_point(self, collection_name, point_id, *, owner_id):
        inspect.signature(QdrantStore.retrieve_owned_point).bind(None, collection_name, point_id, owner_id=owner_id)
        self.calls.append(("retrieve", collection_name, point_id, owner_id))
        return self.seed

    def search_owner_windows(self, collection_name, query_vector, *, owner_id, exclude_scene_id=None, limit=10):
        inspect.signature(QdrantStore.search_owner_windows).bind(
            None, collection_name, query_vector, owner_id=owner_id, exclude_scene_id=exclude_scene_id, limit=limit
        )
        self.calls.append(("search", collection_name, owner_id, exclude_scene_id, limit))
        return self.hits


def test_library_search_starts_from_the_seed_window_and_stays_with_its_owner():
    store = _Store({"id": "p", "vector": [0.1, 0.2], "payload": {"scene_id": "s1"}}, [{"id": "q", "score": 0.93, "payload": {"scene_id": "s2"}}])

    seed, hits, window = similar.library_matches(
        store, scene_id="s1", owner_id="u1", model_version="0.1.0", window_tokens=16, row=17, col=40, limit=5
    )

    assert window == (1, 2)
    assert store.calls[0] == ("retrieve", settings.OPTICAL_QDRANT_COLLECTION, emb.window_point_id("s1", "0.1.0", 1, 2), "u1")
    assert store.calls[1] == ("search", settings.OPTICAL_QDRANT_COLLECTION, "u1", "s1", 5)
    assert hits[0]["score"] == 0.93 and seed["id"] == "p"


def test_a_window_that_was_not_indexed_is_refused():
    with pytest.raises(similar.SimilarError) as info:
        similar.library_matches(
            _Store(None, []), scene_id="s1", owner_id="u1", model_version="0.1.0", window_tokens=16, row=0, col=0, limit=5
        )

    assert info.value.status == 422


def test_window_matches_carry_place_and_map_summary():
    payload = {
        "scene_id": "5d0c9a8e-0d55-4b0e-9a57-0c0f6d1b2a10", "bbox_lonlat": [83.2, 17.7, 83.21, 17.71],
        "center_lonlat": [83.205, 17.705], "dates": ["2026-04-13"], "dominant_class": "Built-up",
        "class_shares": {"Built-up": 70.0, "Water bodies & wetlands": 30.0},
    }

    match = similar.window_match(payload, 0.912345, "Land cover 2026-04-13")

    assert match["score"] == 0.9123 and match["scene_name"] == "Land cover 2026-04-13"
    assert match["class_shares"]["Built-up"] == 70.0


# --------------------------------------------------------------------------
# The Qdrant payload contract
# --------------------------------------------------------------------------


def _payload(**overrides) -> dict:
    base = {
        "owner_id": "9d3d658e-47e0-429c-8463-c256c9e49f9b", "project_id": "ca58d2fe-234e-4813-9d77-160bb0d190c6",
        "scene_id": "6079c678-e128-49be-91cf-d53b21ecbec8", "window_row": 0, "window_col": 1, "row_start": 0,
        "row_end": 64, "col_start": 64, "col_end": 128, "window_px": 64, "epsg": 32644,
        "bbox_lonlat": [83.2, 17.7, 83.21, 17.71], "center_lonlat": [83.205, 17.705], "valid_tokens": 256,
        "model_name": "Raikou OlmoEarth India LULC (5 classes)", "model_version": "0.1.0", "embedding": "window mean",
    }
    return {**base, **overrides}


def test_the_optical_payload_requires_the_tenant_and_rejects_strays():
    assert OpticalWindowPayload.model_validate(_payload()).as_qdrant_payload()["scene_id"] == "6079c678-e128-49be-91cf-d53b21ecbec8"
    with pytest.raises(pydantic.ValidationError):
        OpticalWindowPayload.model_validate({k: v for k, v in _payload().items() if k != "owner_id"})
    with pytest.raises(pydantic.ValidationError):
        OpticalWindowPayload.model_validate(_payload(session_id="legacy"))
    with pytest.raises(pydantic.ValidationError):
        OpticalWindowPayload.model_validate(_payload(row_end=0))
