"""POST /api/v1/optical/runs/{id}/similar through the real app, with Supabase, storage and Qdrant faked."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
import numpy as np
import pytest

import main
from app.api.deps import CurrentUser, get_current_user
from app.api.routes import optical as optical_routes
from app.core import middleware
from app.services.optical import embeddings as emb
from app.services.optical import similar
from app.services.optical.artifacts import EMBEDDINGS_KEY, WINDOWS_KEY
from app.services.optical.grid import OpticalGrid, _transform

OWNER = str(uuid4())
SCENE_ID = str(uuid4())
OTHER_RUN = str(uuid4())
DELETED_RUN = str(uuid4())
GRID = OpticalGrid(epsg=32644, x0=735120.0, y_top=1962000.0, width=64, height=64)


def _centre_of(row: float, col: float) -> tuple[float, float]:
    lons, lats = _transform(GRID.epsg, 4326, [GRID.x0 + col * GRID.res], [GRID.y_top - row * GRID.res])
    return lons[0], lats[0]


class _Query:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def in_(self, *_a, **_k):
        return self

    def execute(self):
        return SimpleNamespace(data=self.rows)


class _Supabase:
    def __init__(self, tables: dict[str, list[dict]]) -> None:
        self.tables = tables

    def table(self, name: str) -> _Query:
        return _Query(self.tables.get(name, []))


class _Storage:
    def __init__(self, source: Path) -> None:
        self.source = source

    def download_file(self, key, destination):
        Path(destination).write_bytes(self.source.read_bytes())


class _Qdrant:
    def __init__(self, seed: dict | None, hits: list[dict]) -> None:
        self.seed, self.hits = seed, hits

    def retrieve_owned_point(self, collection_name, point_id, *, owner_id):
        return self.seed if owner_id == OWNER else None

    def search_owner_windows(self, collection_name, query_vector, *, owner_id, exclude_scene_id=None, limit=10):
        return [h for h in self.hits if h["payload"]["scene_id"] != exclude_scene_id][:limit]


def _artifacts() -> list[dict]:
    common = {"model_name": "Raikou OlmoEarth India LULC (5 classes)", "model_version": "0.1.0", "grid": GRID.as_dict(), "token_px": 4}
    return [
        {"id": "emb-1", "logical_key": EMBEDDINGS_KEY, "storage_key": "k/emb", "size_bytes": 10, "checksum_sha256": "c1",
         "metadata": {**common, "dims": 8, "energy_captured": 0.97, "basis": "top singular directions"}},
        {"id": "win-1", "logical_key": WINDOWS_KEY, "storage_key": "k/win", "size_bytes": 10, "checksum_sha256": "c2",
         "metadata": {**common, "window_tokens": 16, "window_m": 640.0, "dim": 768, "embedding": "window mean"}},
    ]


def _window_payload(scene_id: str, dominant: str) -> dict:
    return {"scene_id": scene_id, "bbox_lonlat": [83.2, 17.7, 83.21, 17.71], "center_lonlat": [83.205, 17.705],
            "dates": ["2026-04-13"], "dominant_class": dominant, "class_shares": {dominant: 100.0}}


@pytest.fixture
def api(tmp_path, monkeypatch):
    similar.clear_cache()
    reduced = np.zeros((16, 16, 8), np.float16)
    reduced[:, :8, 0] = 1
    reduced[:, 8:, 1] = 1
    source = tmp_path / "emb.npz"
    np.savez(source, reduced=reduced, valid=np.ones((16, 16), bool))

    state = SimpleNamespace(
        scene={"id": SCENE_ID, "owner_id": OWNER, "name": "Land cover 2026-04-13", "status": "ready", "metadata": {"pipeline": "optical"}},
        tables={"scene_artifacts": _artifacts(), "scenes": [{"id": OTHER_RUN, "name": "Land cover Chennai"}]},
        qdrant=_Qdrant(
            {"id": "seed", "vector": [0.1] * 4, "payload": _window_payload(SCENE_ID, "Built-up")},
            [
                {"id": "a", "score": 0.97, "payload": _window_payload(OTHER_RUN, "Built-up")},
                {"id": "b", "score": 0.95, "payload": _window_payload(DELETED_RUN, "Built-up")},
            ],
        ),
    )

    async def resolve_owned_scene(scene_id, current_user, **_):
        return state.scene

    monkeypatch.setattr(optical_routes, "resolve_owned_scene", resolve_owned_scene)
    monkeypatch.setattr(optical_routes, "get_supabase", lambda: _Supabase(state.tables))
    monkeypatch.setattr(optical_routes, "get_object_storage", lambda: _Storage(source))
    monkeypatch.setattr(optical_routes.QdrantStore, "get_instance", staticmethod(lambda: state.qdrant))
    monkeypatch.setattr(middleware.ReleaseHardeningMiddleware, "_allow_request", lambda self, request: True)
    main.app.dependency_overrides[get_current_user] = lambda: CurrentUser(id=OWNER, role="authenticated")
    state.client = TestClient(main.app)
    yield state
    main.app.dependency_overrides.pop(get_current_user, None)
    similar.clear_cache()


def _post(api, **body):
    lon, lat = _centre_of(6.5, 9.5)  # pixel (6, 9) -> token (1, 2): the left half
    return api.client.post(f"/api/v1/optical/runs/{SCENE_ID}/similar", json={"lon": lon, "lat": lat, **body})


def test_a_click_returns_a_heatmap_over_the_run(api):
    response = _post(api)

    assert response.status_code == 200
    body = response.json()
    assert body["scope"] == "run" and body["label"] == emb.LABEL
    assert (body["seed"]["token_row"], body["seed"]["token_col"], body["seed"]["token_m"]) == (1, 2, 40.0)
    assert body["image"]["data_url"].startswith("data:image/png;base64,")
    assert (body["image"]["width"], body["image"]["height"]) == (16, 16)
    at_09 = next(a for a in body["areas"] if a["threshold"] == 0.9)
    assert at_09["share_pct"] == 50.0
    assert body["basis"]["energy_captured"] == 0.97


def test_library_matches_skip_runs_that_no_longer_exist(api):
    response = _post(api, scope="library", limit=5)

    assert response.status_code == 200
    body = response.json()
    assert body["scope"] == "library" and body["seed"]["window_row"] == 0 and body["seed"]["window_col"] == 0
    assert [m["scene_id"] for m in body["matches"]] == [OTHER_RUN]
    assert body["matches"][0]["scene_name"] == "Land cover Chennai" and body["matches"][0]["score"] == 0.97
    assert body["seed_window"]["dominant_class"] == "Built-up"


def test_no_other_runs_gives_an_empty_library_with_a_note(api):
    api.qdrant.hits = []

    body = _post(api, scope="library").json()

    assert body["matches"] == [] and body["notes"]


def test_a_run_from_before_phase_two_asks_for_a_reprocess(api):
    api.tables["scene_artifacts"] = []

    response = _post(api)

    assert response.status_code == 409
    assert "Reprocess" in response.json()["detail"]


def test_a_run_still_processing_is_refused(api):
    api.scene["status"] = "processing"

    assert _post(api).status_code == 409


def test_a_sar_scene_is_not_an_optical_run(api):
    api.scene["metadata"] = {}

    assert _post(api).status_code == 404


def test_a_click_outside_the_run_is_refused(api):
    response = api.client.post(f"/api/v1/optical/runs/{SCENE_ID}/similar", json={"lon": 80.0, "lat": 17.7})

    assert response.status_code == 422
    assert "outside" in response.json()["detail"]


def test_unknown_fields_are_rejected(api):
    response = api.client.post(f"/api/v1/optical/runs/{SCENE_ID}/similar", json={"lon": 83.2, "lat": 17.7, "row": 3})

    assert response.status_code == 422
