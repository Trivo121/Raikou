"""The optical stages end to end against fakes: offline, no database, no torch.

Every repository and storage call is bound against the real WorkerRepository
and S3ObjectStorage signatures, Qdrant calls against QdrantStore, and the model
calls against the release's own ``run_aoi`` and ``predict_tile`` signatures, so
a renamed keyword fails here instead of on the box halfway through a run.
Sentinel Hub is replaced at the HTTP seam and the model at ``lulc_model``;
everything in between (GeoTIFF checks, the overview, the date selection, CSV
parsing, the captured model outputs, embeddings, window vectors, the bundle
zip, the record) is production code.
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta
import importlib
import inspect
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
from typing import Any
from uuid import uuid4
import zipfile

import httpx
import numpy as np
from PIL import Image
import pytest
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import Affine

from app.core.config import settings
from app.services.acquisitions import copernicus
from app.services.optical import lulc_model
from app.services.optical.artifacts import (
    BUNDLE_KEY,
    EMBEDDINGS_KEY,
    OVERVIEW_KEY,
    PREVIEW_KEYS,
    RECORD_KEY,
    SUMMARY_KEY,
    WINDOWS_KEY,
    public_name,
)
from app.services.optical.grid import OpticalGrid
from app.services.storage.object_store import ObjectInfo, ObjectNotFoundError, S3ObjectStorage
from app.services.storage.payloads import OpticalWindowPayload
from app.services.storage.qdrant import QdrantStore
from app.workers import stages
from app.workers.repository import RetryableTaskError, UserFacingTaskError, WorkerRepository

PHASE0 = Path(__file__).resolve().parents[2] / "training" / "optical_phase0"
GRID = OpticalGrid(epsg=32644, x0=735120.0, y_top=1962000.0, width=128, height=136)
STACK_DATES = ["2025-11-02T05:06:51+00:00", "2026-02-05T05:08:01+00:00", "2026-04-12T05:06:49.024000+00:00"]
DAYS = [d[:10] for d in STACK_DATES]
# Cloudy columns per date: 75% clear, ~6% clear (below the 30% bar), fully clear.
CLOUD_COLS = {"2025-11-02": 32, "2026-02-05": 120, "2026-04-12": 0}
SCENE_NAME = "Land cover 2025-11-02 (17.720, 83.245)"
CLASS_NAMES = ["Built-up", "Agriculture", "Forest & natural vegetation", "Barren / wasteland", "Water bodies & wetlands"]
FEATURE_DIM = 48  # small stand-in for the encoder's 768; the collection must take it from the vectors
SLUG = "land_cover_2025_11_02_17_720_83_245"
MODEL_SUMMARY = {
    "name": "Raikou OlmoEarth India LULC (5 classes)",
    "version": "0.1.0",
    "base_model": "allenai/OlmoEarth-v1_2-Base",
    "licence": "OlmoEarth Artifact License (see LICENSE_OlmoEarth_Artifact.txt)",
    "single_date": {"agreement_overall": 0.8712, "mean_iou": 0.61, "macro_f1": 0.74, "per_class_iou": {}},
    "seasonal_stack": {"agreement_overall": 0.8934, "mean_iou": 0.65, "macro_f1": 0.77, "per_class_iou": {}},
    "reference": "label_raw = WorldCover 2021 remapped, unfiltered (agreement, not ground truth)",
    "temperature": 1.31,
    "calibration_meaning": "calibrated probability that a pixel agrees with the WorldCover-2021-derived class",
    "attribution": {},
}


def _bind(real: type, name: str, *args: Any, **kwargs: Any) -> None:
    """Fail unless this call would bind to the real method (``None`` stands in for self)."""
    inspect.signature(getattr(real, name)).bind(None, *args, **kwargs)


def _release_signature(module_file: Path, name: str) -> inspect.Signature:
    """A release function's signature, read from source without importing torch."""
    tree = ast.parse(module_file.read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
    for arg in function.args.posonlyargs + function.args.args + function.args.kwonlyargs:
        arg.annotation = None
    function.returns, function.decorator_list, function.body = None, [], [ast.Pass()]
    namespace: dict[str, Any] = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), str(module_file), "exec"), namespace)
    return inspect.signature(namespace[name])


def _bands(day: str) -> np.ndarray:
    """The 13-band array Sentinel Hub 'returns' for one date: 12 bands of DN, then SCL."""
    rng = np.random.default_rng(int(day.replace("-", "")))
    data = rng.integers(200, 3000, size=(13, GRID.height, GRID.width)).astype(np.uint16)
    data[12] = 4
    data[12, :, : CLOUD_COLS[day]] = 9
    return data


def _geotiff(data: np.ndarray) -> bytes:
    with MemoryFile() as memory:
        with memory.open(driver="GTiff", width=GRID.width, height=GRID.height, count=13, dtype="uint16",
                         crs=f"EPSG:{GRID.epsg}", transform=Affine(*GRID.transform)) as dataset:
            dataset.write(data)
        return memory.read()


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeRepository:
    def __init__(self, scene: dict[str, Any], acquisition: dict[str, Any]) -> None:
        self.scene_row = scene
        self.acquisition = acquisition
        self.artifacts: dict[str, dict[str, Any]] = {}
        self.events: list[str] = []
        self.evidence: dict[str, Any] | None = None
        self.source_artifact_id: str | None = None

    def scene(self, task):
        _bind(WorkerRepository, "scene", task)
        return self.scene_row

    def scene_acquisition(self, task):
        _bind(WorkerRepository, "scene_acquisition", task)
        return self.acquisition

    def job_sources(self, task):
        _bind(WorkerRepository, "job_sources", task)
        return [a for a in self.artifacts.values() if a["kind"] in {"source_archive", "source_raster", "metadata"}]

    def artifact_by_logical_key(self, task, logical_key):
        _bind(WorkerRepository, "artifact_by_logical_key", task, logical_key)
        return self.artifacts.get(logical_key)

    def upsert_artifact(self, task, **fields):
        _bind(WorkerRepository, "upsert_artifact", task, **fields)
        prior = self.artifacts.get(fields["logical_key"])
        row = {"id": prior["id"] if prior else str(uuid4()), "status": "available", **fields}
        self.artifacts[fields["logical_key"]] = row
        return row

    def mark_acquisition_downloading(self, acquisition_id, owner_id):
        _bind(WorkerRepository, "mark_acquisition_downloading", acquisition_id, owner_id)
        self.acquisition["status"] = "downloading"

    def mark_acquisition_downloaded(self, acquisition_id, owner_id, **fields):
        _bind(WorkerRepository, "mark_acquisition_downloaded", acquisition_id, owner_id, **fields)
        self.acquisition.update(status="downloaded", processing_units=fields.get("processing_units"))

    def mark_acquisition_failed(self, acquisition_id, owner_id, **fields):
        _bind(WorkerRepository, "mark_acquisition_failed", acquisition_id, owner_id, **fields)
        self.acquisition.update(status="failed", failure_code=fields["code"])

    def set_scene_source_artifact(self, task, artifact_id):
        _bind(WorkerRepository, "set_scene_source_artifact", task, artifact_id)
        self.source_artifact_id = str(artifact_id)

    def record_progress_event(self, task, **fields):
        _bind(WorkerRepository, "record_progress_event", task, **fields)
        self.events.append(fields["message"])

    def update_scene_metadata(self, task, metadata):
        _bind(WorkerRepository, "update_scene_metadata", task, metadata)
        # The SQL is `metadata = coalesce(metadata, '{}') || new`: a top-level merge.
        self.scene_row["metadata"] = {**(self.scene_row.get("metadata") or {}), **metadata}

    def upsert_evidence_record(self, task, **fields):
        _bind(WorkerRepository, "upsert_evidence_record", task, **fields)
        self.evidence = fields

    def renew_task_lease(self, task, worker_id):
        _bind(WorkerRepository, "renew_task_lease", task, worker_id)
        return True


class FakeStorage:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        return self.root / key.replace(":", "_")

    def _info(self, key: str, content_type: str | None) -> ObjectInfo:
        return ObjectInfo(key=key, size_bytes=self._path(key).stat().st_size, content_type=content_type,
                          checksum_sha256=None, etag=None, metadata={})

    def upload_file(self, key, source, content_type, metadata=None):
        _bind(S3ObjectStorage, "upload_file", key, source, content_type, metadata)
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        return self._info(key, content_type)

    def download_file(self, key, destination):
        _bind(S3ObjectStorage, "download_file", key, destination)
        shutil.copyfile(self._path(key), destination)
        return self._info(key, None)

    def head_object(self, key):
        _bind(S3ObjectStorage, "head_object", key)
        if not self._path(key).is_file():
            raise ObjectNotFoundError(key)
        return self._info(key, None)

    def read_json(self, key: str) -> dict[str, Any]:
        return json.loads(self._path(key).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def release_code():
    """The release's own tiles and gis_export modules (numpy only at import)."""
    sys.path.insert(0, str(PHASE0))
    try:
        yield SimpleNamespace(
            tiles=importlib.import_module("raikou_phase0.tiles"),
            gis_export=importlib.import_module("raikou_phase0.gis_export"),
            run_aoi=_release_signature(PHASE0 / "raikou_phase0" / "bundle.py", "run_aoi"),
            predict_tile=_release_signature(PHASE0 / "raikou_phase0" / "infer.py", "predict_tile"),
        )
    finally:
        sys.path.remove(str(PHASE0))
        for name in [n for n in sys.modules if n == "raikou_phase0" or n.startswith("raikou_phase0.")]:
            del sys.modules[name]


def _model_outputs(valid: np.ndarray, use: list[int]) -> dict[str, Any]:
    """What predict_tile returns: blended logits, has_data and encoder features (D, H/4, W/4).

    The map is built-up on the left half and forest on the right; the
    features follow the same split, so window vectors and similarity have
    structure to check.
    """
    height, width = GRID.height, GRID.width
    classes = np.zeros((height, width), np.uint8)
    classes[:, width // 2 :] = 2  # forest & natural vegetation
    logits = np.full((len(CLASS_NAMES), height, width), -2.0, np.float32)
    for k in range(len(CLASS_NAMES)):
        logits[k][classes == k] = 3.0
    rng = np.random.default_rng(5)
    left, right = rng.normal(size=FEATURE_DIM), rng.normal(size=FEATURE_DIM)
    feats = np.empty((FEATURE_DIM, height // 4, width // 4), np.float32)
    feats[:, :, : width // 8] = left[:, None, None]
    feats[:, :, width // 8 :] = right[:, None, None]
    feats += 0.01 * rng.normal(size=feats.shape)
    return {
        "logits": logits,
        "has_data": valid[use].any(0),
        "timesteps_used": list(use),
        "features": feats.astype(np.float16),
    }


CLASS_COLORS = [(250, 0, 0), (240, 150, 255), (0, 100, 0), (180, 180, 180), (0, 100, 200)]


def _write_class_raster(path: Path, classes: np.ndarray, has_data: np.ndarray, *, palette: bool = True) -> None:
    """A class map as the release writes it: 1..5, 0 is no data and fully transparent in the colour table."""
    export = np.where(has_data, classes.astype(np.int16) + 1, 0).astype(np.uint8)
    with rasterio.open(path, "w", driver="GTiff", width=GRID.width, height=GRID.height, count=1, dtype="uint8",
                       crs=f"EPSG:{GRID.epsg}", transform=Affine(*GRID.transform), nodata=0) as dst:
        dst.write(export, 1)
        if palette:
            dst.write_colormap(1, {0: (0, 0, 0, 0), **{i + 1: (*rgb, 255) for i, rgb in enumerate(CLASS_COLORS)}})


def _fake_bundle(release_code, calls: list[dict[str, Any]], predict_calls: list[dict[str, Any]]) -> SimpleNamespace:
    """run_aoi and predict_tile with the release's signatures; run_aoi finds predict_tile on its module, as the real one does."""
    G = release_code.gis_export
    bundle = SimpleNamespace()

    def predict_tile(*args, **kwargs):
        bound = release_code.predict_tile.bind(*args, **kwargs)
        bound.apply_defaults()
        predict_calls.append(dict(bound.arguments))
        return _model_outputs(bound.arguments["valid"], bound.arguments["use_timesteps"])

    def run_aoi(*args, **kwargs):
        bound = release_code.run_aoi.bind(*args, **kwargs)
        bound.apply_defaults()
        call = dict(bound.arguments)
        calls.append(call)
        out, name = Path(call["out_dir"]), call["name"]
        out.mkdir(parents=True, exist_ok=True)
        res = bundle.predict_tile(call["model"], call["dn"], call["valid"], call["dates"],
                                  use_timesteps=call["use_timesteps"], amp=call["amp"], want_features=True)
        classes = res["logits"].argmax(0).astype(np.uint8)
        reference = np.where(np.random.default_rng(3).random(classes.shape) < 0.8, classes, (classes + 1) % G.C.NUM_CLASSES).astype(np.uint8)
        G.write_agreement_csv(str(out / "agreement_matrix.csv"), G.agreement_matrix(classes, reference), "dates", {"T1": 0.13})
        G.write_csv(str(out / "lulc_stats.csv"), G.class_stats(classes, reference, np.full(classes.shape, 80.0)))
        _write_class_raster(out / f"{name}_lulc_10m.tif", classes, res["has_data"])
        _write_class_raster(out / f"{name}_worldcover2021_10m.tif", reference, np.ones_like(res["has_data"]))
        for suffix in PREVIEW_KEYS:
            Image.new("RGB", (8, 8)).save(out / f"{name}_{suffix}")
        # Everything else the release leaves in its working folder; only some of it reaches the user.
        for suffix in ("lulc_10m.qml", "lulc_10m.clr", "lulc_10m.tif.aux.xml", "score_10m.tif", "score_10m.qml",
                       "worldcover2021_10m.qml", "worldcover2021_10m.clr", "worldcover2021_10m.tif.aux.xml", "polygons.gpkg",
                       "model_view_pca.tif", "similarity.tif", "similarity.qml"):
            (out / f"{name}_{suffix}").write_bytes(b"stand-in")
        (out / "VALIDATION_AND_CITATION.txt").write_text("cite", encoding="utf-8")
        (out / "run_summary.json").write_text("{}", encoding="utf-8")
        return {"out_dir": str(out), "raster_format": "COG", "polygons": 42, "dates": "", "valid_fraction": 0.81}

    bundle.predict_tile = predict_tile
    bundle.run_aoi = run_aoi
    return bundle


class FakeQdrant:
    """Optical side of QdrantStore; every call bound to the real signature, every payload validated."""

    def __init__(self) -> None:
        self.collections: dict[str, int] = {}
        self.points: dict[str, tuple[list[float], dict[str, Any]]] = {}

    def initialize_collection(self, collection_name, vector_size=768):
        _bind(QdrantStore, "initialize_collection", collection_name, vector_size=vector_size)
        self.collections[collection_name] = vector_size

    def delete_vectors_by_scene(self, collection_name, *, owner_id, project_id, scene_id):
        _bind(QdrantStore, "delete_vectors_by_scene", collection_name, owner_id=owner_id, project_id=project_id, scene_id=scene_id)
        self.points = {k: v for k, v in self.points.items() if v[1]["scene_id"] != str(scene_id)}

    def upsert_optical_windows(self, collection_name, points):
        _bind(QdrantStore, "upsert_optical_windows", collection_name, points)
        for point in points:
            payload = OpticalWindowPayload.model_validate(point["payload"]).as_qdrant_payload()
            assert len(point["vector"]) == self.collections[collection_name]
            self.points[point["id"]] = (point["vector"], payload)

    def count_vectors_by_scene(self, collection_name, *, owner_id, project_id, scene_id):
        _bind(QdrantStore, "count_vectors_by_scene", collection_name, owner_id=owner_id, project_id=project_id, scene_id=scene_id)
        return sum(1 for _, p in self.points.values() if p["scene_id"] == str(scene_id) and p["owner_id"] == str(owner_id))


@pytest.fixture
def optical(tmp_path, monkeypatch, release_code):
    monkeypatch.setattr(settings, "M3_WORKER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    monkeypatch.setattr(settings, "COPERNICUS_ENABLED", True)
    monkeypatch.setattr(settings, "COPERNICUS_CLIENT_ID", "test-client")
    monkeypatch.setattr(settings, "COPERNICUS_CLIENT_SECRET", "test-secret")
    monkeypatch.setattr(stages, "invalidate_project_evidence_cache_sync", lambda **_: None)
    copernicus.reset_token_cache()

    process_calls: list[str] = []

    def sentinel_hub(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "token-abc", "expires_in": 600})
        start = json.loads(request.content)["input"]["data"][0]["dataFilter"]["timeRange"]["from"]
        day = (datetime.fromisoformat(start.replace("Z", "+00:00")) + timedelta(minutes=30)).date().isoformat()
        process_calls.append(day)
        return httpx.Response(200, content=_geotiff(_bands(day)), headers={"x-processingunits-spent": "0.29"})

    transport = httpx.MockTransport(sentinel_hub)
    monkeypatch.setattr(
        copernicus, "_http_client",
        lambda read_seconds, *, follow_redirects=False: httpx.Client(transport=transport, follow_redirects=follow_redirects),
    )

    model_calls: list[dict[str, Any]] = []
    predict_calls: list[dict[str, Any]] = []
    bundle = _fake_bundle(release_code, model_calls, predict_calls)
    phase0 = SimpleNamespace(bundle=bundle, tiles=release_code.tiles, infer=None)
    monkeypatch.setattr(lulc_model, "phase0", lambda: phase0)
    monkeypatch.setattr(lulc_model, "get_model", lambda: SimpleNamespace(P=4))
    monkeypatch.setattr(
        lulc_model, "card", lambda: {"calibration": {"temperature": 1.31}, "classes": [{"name": n} for n in CLASS_NAMES]}
    )
    qdrant = FakeQdrant()
    monkeypatch.setattr(QdrantStore, "get_instance", staticmethod(lambda: qdrant))
    monkeypatch.setattr(lulc_model, "benchmark", lambda: {"summary": {"T1": {"overall_accuracy": 0.87}}})
    monkeypatch.setattr(lulc_model, "card_summary", lambda: dict(MODEL_SUMMARY))
    monkeypatch.setattr(lulc_model, "amp", lambda: "fp16")

    owner, project, scene_id, job = (str(uuid4()) for _ in range(4))
    scene = {
        "id": scene_id, "owner_id": owner, "project_id": project, "name": SCENE_NAME,
        "metadata": {"pipeline": "optical", "optical": {"mode": "seasonal_stack", "dates": DAYS,
                                                         "aoi": {"west": 83.2, "south": 17.68, "east": 83.29, "north": 17.77}}},
    }
    acquisition = {
        "id": str(uuid4()), "owner_id": owner, "collection": "SENTINEL-2", "status": "queued",
        "grid": GRID.as_dict(),
        "stack": [{"product_id": str(uuid4()), "product_name": f"S2B_MSIL2A_{d}", "sensing_start": d,
                   "cloud_cover": 5.0, "season": s} for d, s in zip(STACK_DATES, ("post_monsoon", "rabi", "summer"))],
    }
    repository = FakeRepository(scene, acquisition)
    storage = FakeStorage(tmp_path / "bucket")
    pipeline = object.__new__(stages.M3Pipeline)
    pipeline.repository, pipeline.storage, pipeline.bucket = repository, storage, "test-bucket"

    def run(stage: str):
        task = {"id": str(uuid4()), "processing_job_id": job, "scene_id": scene_id, "owner_id": owner,
                "project_id": project, "stage": stage, "attempt": 1, "max_attempts": 3, "locked_by": ""}
        return pipeline.run(task)

    yield SimpleNamespace(run=run, repository=repository, storage=storage, process_calls=process_calls,
                          model_calls=model_calls, predict_calls=predict_calls, qdrant=qdrant, bundle=bundle)
    copernicus.reset_token_cache()


def _run_all(optical) -> list[Any]:
    results, stage = [], "fetch_source"
    while stage is not None:
        result = optical.run(stage)
        results.append((stage, result))
        stage = result.next_stage[0] if result.next_stage else None
    return results


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_a_three_date_run_goes_from_fetch_to_finalize(optical):
    results = _run_all(optical)

    assert [stage for stage, _ in results] == [
        "fetch_source", "validate_upload", "extract_metadata", "build_overview",
        "embed_patches", "build_evidence", "index_vectors", "finalize",
    ]
    assert dict(results)["build_overview"].next_stage == ("embed_patches", "optical")
    assert dict(results)["finalize"].result["pipeline"] == "optical"


def test_fetch_stores_one_checked_source_per_date(optical):
    optical.run("fetch_source")
    repository = optical.repository

    sources = [a for a in repository.artifacts.values() if a["kind"] == "source_raster"]
    assert [a["metadata"]["timestep"]["sensing_start"][:10] for a in sources] == DAYS
    assert [a["metadata"]["timestep"]["valid_fraction"] for a in sources] == [0.75, 0.0625, 1.0]
    assert all(a["metadata"]["grid"] == GRID.as_dict() for a in sources)
    assert repository.acquisition["status"] == "downloaded"
    assert repository.acquisition["processing_units"] == pytest.approx(0.87)
    assert repository.source_artifact_id == sources[0]["id"]
    assert repository.events[0] == "Fetched Sentinel-2 2025-11-02 (1/3): 75% clear."
    assert optical.process_calls == DAYS


def test_a_retried_fetch_reuses_what_is_already_stored(optical):
    optical.run("fetch_source")
    first_ids = sorted(a["id"] for a in optical.repository.artifacts.values())

    optical.run("fetch_source")

    assert optical.process_calls == DAYS  # no second round of processing units
    assert sorted(a["id"] for a in optical.repository.artifacts.values()) == first_ids
    assert optical.repository.acquisition["processing_units"] == pytest.approx(0.87)


def test_metadata_keeps_what_the_api_wrote(optical):
    for stage in ("fetch_source", "validate_upload", "extract_metadata"):
        optical.run(stage)

    metadata = optical.repository.scene_row["metadata"]
    assert metadata["pipeline"] == "optical" and metadata["sensor"] == "Sentinel-2"
    assert metadata["optical"]["aoi"]["west"] == 83.2
    assert metadata["optical"]["dates"] == DAYS
    assert metadata["optical"]["mode"] == "seasonal_stack"
    assert len(metadata["optical"]["timesteps"]) == 3
    assert len(metadata["optical"]["corners_lonlat"]) == 4


def test_the_overview_is_the_clearest_date(optical):
    for stage in ("fetch_source", "validate_upload", "extract_metadata", "build_overview"):
        optical.run(stage)

    overview = optical.repository.artifacts[OVERVIEW_KEY]
    assert overview["kind"] == "overview" and overview["content_type"] == "image/jpeg"
    assert overview["metadata"]["date"].startswith("2026-04-12")
    with Image.open(optical.storage._path(overview["storage_key"])) as image:
        assert image.size == (GRID.width, GRID.height)


def test_the_model_sees_the_stored_pixels_and_only_the_clear_dates(optical):
    _run_all(optical)

    (call,) = optical.model_calls
    assert call["use_timesteps"] == [0, 2]
    assert call["dates"] == DAYS
    assert call["temperature"] == 1.31 and call["amp"] == "fp16"
    assert call["name"] == SLUG
    assert call["dn"].dtype == np.uint16 and call["dn"].shape == (3, 12, GRID.height, GRID.width)
    for index, day in enumerate(DAYS):
        bands = _bands(day)
        assert np.array_equal(call["dn"][index], bands[:12])
        assert call["valid"][index].mean() == pytest.approx(1.0 - CLOUD_COLS[day] / GRID.width)
    tile = call["tile"]
    assert (tile.epsg, tile.x0, tile.y_top, tile.width, tile.height) == (GRID.epsg, GRID.x0, GRID.y_top, GRID.width, GRID.height)


def test_the_bundle_previews_and_summary_are_stored_under_their_public_names(optical):
    _run_all(optical)
    artifacts = optical.repository.artifacts

    for logical_key in PREVIEW_KEYS.values():
        assert artifacts[logical_key]["kind"] == "evidence"
        assert len(artifacts[logical_key]["metadata"]["corners_lonlat"]) == 4
    style = artifacts[f"derived:optical:file:{SLUG}_landcover.qml:v1"]
    assert style["kind"] == "other" and style["content_type"] == "application/xml"
    colour_map = artifacts[f"derived:optical:file:{SLUG}_landcover.clr:v1"]
    assert colour_map["kind"] == "other" and colour_map["content_type"] == "text/plain"

    bundle = artifacts[BUNDLE_KEY]
    with zipfile.ZipFile(optical.storage._path(bundle["storage_key"])) as archive:
        names = archive.namelist()
    root = f"{SLUG}_gis_bundle/"
    # Only the rasters and their style files, not the release's whole working folder.
    assert set(names) == {root + n for n in (
        f"{SLUG}_landcover.tif", f"{SLUG}_landcover.qml", f"{SLUG}_landcover.clr",
        f"{SLUG}_confidence.tif", f"{SLUG}_confidence.qml",
        f"extras/{SLUG}_worldcover2021.tif", f"extras/{SLUG}_worldcover2021.qml", f"extras/{SLUG}_worldcover2021.clr",
        f"extras/{SLUG}_class_probabilities.tif",
    )}
    assert {Path(n).suffix for n in names} == {".tif", ".qml", ".clr"}
    assert sorted(bundle["metadata"]["files"]) == sorted(n[len(root):] for n in names)

    summary = optical.storage.read_json(artifacts[SUMMARY_KEY]["storage_key"])
    assert summary["timesteps_used"] == [0, 2]
    assert summary["dates_used"] == [DAYS[0], DAYS[2]]
    assert len(summary["stats"]) == 5 and len(summary["agreement"]["matrix_km2"]) == 5
    assert summary["artifacts"]["bundle_zip"] == bundle["id"]
    assert summary["artifacts"]["true_color"] == artifacts[PREVIEW_KEYS["true_color.png"]]["id"]
    assert summary["artifacts"]["model_view"] == artifacts[PREVIEW_KEYS["model_view_pca.png"]]["id"]
    assert f"{SLUG}_landcover.tif" in summary["artifacts"] and f"{SLUG}_lulc_10m.tif" not in summary["artifacts"]
    assert not any(k.endswith((".xlsx", ".txt", ".gpkg", ".csv")) for k in summary["artifacts"])


def test_the_record_and_evidence_row_describe_the_run(optical):
    _run_all(optical)
    repository = optical.repository

    record_artifact = repository.artifacts[RECORD_KEY]
    record = optical.storage.read_json(record_artifact["storage_key"])
    assert record_artifact["kind"] == "scene_record"
    assert record["imagery"]["mode"] == "seasonal_stack"
    # Two of three dates were usable, so the single-date benchmark is quoted.
    assert record["lulc"]["evaluation"]["applies_to"] == "single_date"
    assert record["reference"]["is_change_detection"] is False
    assert repository.evidence["summary"].startswith("Sentinel-2 land cover (two dates, 2025-11-02, 2026-04-12): ")
    assert repository.evidence["metadata"]["is_detector_evidence"] is False
    assert repository.evidence["metadata"]["record_artifact_id"] == record_artifact["id"]
    assert repository.evidence["model_name"] == MODEL_SUMMARY["name"]
    assert record["similarity"]["available"] is True and record["similarity"]["indexed_windows"] == 4
    assert len(record["spectral"]["rows"]) == 3 * len(CLASS_NAMES)


def test_the_model_runs_once_and_its_outputs_are_captured(optical):
    _run_all(optical)

    (predict,) = optical.predict_calls
    assert predict["use_timesteps"] == [0, 2] and predict["want_features"] is True
    # The wrapper is gone after the stage: the next run starts from the release's own function.
    assert optical.bundle.predict_tile.__name__ == "predict_tile"


def test_embeddings_keep_what_the_model_sees_and_stay_internal(optical):
    _run_all(optical)
    artifact = optical.repository.artifacts[EMBEDDINGS_KEY]
    meta = artifact["metadata"]

    with np.load(optical.storage._path(artifact["storage_key"])) as data:
        reduced, valid = data["reduced"], data["valid"]
    assert reduced.shape == (GRID.height // 4, GRID.width // 4, meta["dims"]) and reduced.dtype == np.float16
    assert meta["dims"] <= settings.OPTICAL_EMBEDDING_DIMS
    assert meta["energy_captured"] > 0.99  # two directions plus a little noise
    assert (meta["token_px"], meta["token_m"]) == (4, 40.0)
    assert valid.all()  # the third date is fully clear
    assert artifact["kind"] == "other" and public_name(EMBEDDINGS_KEY) is None and public_name(WINDOWS_KEY) is None


def test_window_vectors_are_indexed_with_their_place_and_map_mix(optical):
    results = dict(_run_all(optical))
    qdrant = optical.qdrant
    windows = optical.repository.artifacts[WINDOWS_KEY]

    # The collection takes its size from the model's vectors, not from a constant.
    assert qdrant.collections == {settings.OPTICAL_QDRANT_COLLECTION: FEATURE_DIM}
    # 34 x 32 tokens in 16 x 16 windows: four full windows; the 2-token bottom strip is below the minimum.
    assert windows["metadata"]["window_count"] == 4 == len(qdrant.points)
    assert results["index_vectors"].result["indexed"] == 4
    assert results["finalize"].result["vector_count"] == 4
    west, south, east, north = GRID.bbox_lonlat()
    payloads = sorted((p for _, p in qdrant.points.values()), key=lambda p: (p["window_row"], p["window_col"]))
    for payload in payloads:
        w, s, e, n = payload["bbox_lonlat"]
        assert west - 1e-6 <= w < e <= east + 1e-6 and south - 1e-6 <= s < n <= north + 1e-6
        assert payload["dates"] == [DAYS[0], DAYS[2]] and payload["window_px"] == 64
    assert [p["dominant_class"] for p in payloads] == ["Built-up", "Forest & natural vegetation"] * 2
    assert payloads[0]["class_shares"] == {"Built-up": 100.0}


def test_reindexing_replaces_the_scene_windows(optical):
    _run_all(optical)
    first = dict(optical.qdrant.points)

    optical.run("index_vectors")

    assert set(optical.qdrant.points) == set(first)


def test_the_bundle_gains_spectral_indices_and_class_probabilities(optical):
    _run_all(optical)
    artifacts = optical.repository.artifacts

    with zipfile.ZipFile(optical.storage._path(artifacts[BUNDLE_KEY]["storage_key"])) as archive:
        names = set(archive.namelist())
    assert f"{SLUG}_gis_bundle/extras/{SLUG}_class_probabilities.tif" in names
    assert not any(n.endswith(".csv") for n in names)  # the spectral rows live in the scene record, not the zip
    probabilities = artifacts[f"derived:optical:file:{SLUG}_class_probabilities.tif:v1"]
    with rasterio.open(optical.storage._path(probabilities["storage_key"])) as ds:
        assert (ds.count, ds.dtypes[0], ds.nodata) == (5, "uint8", 255)
        assert ds.descriptions[0] == "P(Built-up) %"
        assert ds.crs.to_epsg() == GRID.epsg and tuple(ds.transform)[:6] == GRID.transform
        built_up, forest = ds.read(1), ds.read(3)
    # Logits 3 against -2 at temperature 1.31 give the mapped class about 92%.
    assert built_up[:, : GRID.width // 2].min() >= 85 and forest[:, GRID.width // 2 :].min() >= 85
    summary = optical.storage.read_json(artifacts[SUMMARY_KEY]["storage_key"])
    assert len(summary["spectral_indices"]) == 3 * len(CLASS_NAMES)
    built_up_rows = [r for r in summary["spectral_indices"] if r["class_name"] == "Built-up"]
    assert [r["used_by_model"] for r in built_up_rows] == [True, False, True]


# --------------------------------------------------------------------------
# Map previews
# --------------------------------------------------------------------------


def test_class_previews_are_painted_from_the_tiff_palette_with_transparent_no_data(tmp_path):
    from app.workers.optical_stages import _class_preview

    classes = np.zeros((GRID.height, GRID.width), np.uint8)
    classes[:, GRID.width // 2 :] = 2
    has_data = np.ones_like(classes, bool)
    has_data[:20, :20] = False
    tif, png = tmp_path / "map.tif", tmp_path / "map.png"
    _write_class_raster(tif, classes, has_data)
    Image.new("RGB", (8, 8), (0, 0, 0)).save(png)  # the release's opaque preview

    _class_preview(tif, png)

    with Image.open(png) as image:
        assert image.mode == "RGBA" and image.size == (GRID.width, GRID.height)
        pixels = np.asarray(image)
    assert pixels[5, 5, 3] == 0  # no data is a hole in the preview, not black
    assert tuple(pixels[50, 10]) == (*CLASS_COLORS[0], 255)  # built-up, as the GeoTIFF's own table says
    assert tuple(pixels[50, GRID.width - 10]) == (*CLASS_COLORS[2], 255)  # forest


def test_a_class_map_without_a_palette_keeps_the_releases_preview(tmp_path):
    from app.workers.optical_stages import _class_preview

    tif, png = tmp_path / "map.tif", tmp_path / "map.png"
    _write_class_raster(tif, np.zeros((GRID.height, GRID.width), np.uint8), np.ones((GRID.height, GRID.width), bool), palette=False)
    Image.new("RGB", (8, 8), (9, 9, 9)).save(png)

    _class_preview(tif, png)  # cosmetic, so it never fails the run

    with Image.open(png) as image:
        assert image.size == (8, 8)
    _class_preview(tmp_path / "missing.tif", png)  # and neither does a raster that is not there


def test_the_stored_class_previews_have_a_transparent_background_slot(optical):
    _run_all(optical)
    artifact = optical.repository.artifacts[PREVIEW_KEYS["lulc.png"]]
    with Image.open(optical.storage._path(artifact["storage_key"])) as image:
        assert image.mode == "RGBA" and image.size == (GRID.width, GRID.height)
    reference = optical.repository.artifacts[PREVIEW_KEYS["worldcover2021.png"]]
    with Image.open(optical.storage._path(reference["storage_key"])) as image:
        assert image.mode == "RGBA"


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


def test_a_sentinel1_acquisition_is_refused(optical):
    optical.repository.acquisition["collection"] = "SENTINEL-1"

    with pytest.raises(UserFacingTaskError) as info:
        optical.run("fetch_source")

    assert info.value.code == "ACQUISITION_NOT_OPTICAL"
    assert optical.process_calls == []


def test_a_date_without_data_fails_the_acquisition(optical, monkeypatch):
    real = _bands

    def empty_first_date(day: str) -> np.ndarray:
        data = real(day)
        if day == DAYS[0]:
            data[:] = 0
        return data

    monkeypatch.setattr(sys.modules[__name__], "_bands", empty_first_date)

    with pytest.raises(UserFacingTaskError) as info:
        optical.run("fetch_source")

    assert info.value.code == "OPTICAL_NO_DATA"
    assert optical.repository.acquisition["failure_code"] == "OPTICAL_NO_DATA"


def test_every_date_under_cloud_stops_before_the_model(optical, monkeypatch):
    monkeypatch.setitem(CLOUD_COLS, "2025-11-02", GRID.width)
    monkeypatch.setitem(CLOUD_COLS, "2026-04-12", GRID.width)
    monkeypatch.setitem(CLOUD_COLS, "2026-02-05", GRID.width)
    for stage in ("fetch_source", "validate_upload", "extract_metadata", "build_overview"):
        optical.run(stage)

    with pytest.raises(UserFacingTaskError) as info:
        optical.run("embed_patches")

    assert info.value.code == "OPTICAL_ALL_CLOUD"
    assert optical.model_calls == []


def test_finalize_waits_for_every_required_artifact(optical):
    with pytest.raises(RetryableTaskError):
        optical.run("finalize")


def test_finalize_waits_for_every_window_vector(optical):
    for stage in ("fetch_source", "validate_upload", "extract_metadata", "build_overview",
                  "embed_patches", "build_evidence", "index_vectors"):
        optical.run(stage)
    optical.qdrant.points.popitem()

    with pytest.raises(RetryableTaskError):
        optical.run("finalize")
