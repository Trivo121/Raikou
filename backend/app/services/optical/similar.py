"""Serve "looks similar to the model" for a finished optical run (API side).

Two scopes, both answering "where else does this place look the same to the
model?" and neither claiming more:

* ``run``: a heatmap over the run's own 40 m tokens, from its reduced token
  embeddings (one small file per run, cached here after the first click);
* ``library``: the nearest 640 m windows from the caller's *other* runs, from
  Qdrant ``optical_patches``, always filtered to the caller.
"""

from __future__ import annotations

import base64
from collections import OrderedDict
import os
import tempfile
import threading
from typing import Any

import numpy as np

from app.core.config import settings
from app.services.optical import embeddings as emb
from app.services.optical.grid import OpticalGrid

LIBRARY_MEANING = (
    "How close each 640 m window from your other optical runs is to the clicked spot's window in the fine-tuned "
    "OlmoEarth encoder's features (cosine similarity). It is not a land-cover class, a probability or a measurement."
)
AREA_THRESHOLDS = (0.6, 0.8, 0.9)

_cache: OrderedDict[str, tuple[np.ndarray, np.ndarray]] = OrderedDict()
_lock = threading.Lock()


class SimilarError(Exception):
    """A request the API should refuse with ``status`` and a plain-language ``detail``."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def load_embeddings(storage: Any, artifact: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    """(reduced (h, w, k) float16, valid (h, w) bool) for one run, cached by artifact version."""
    key = f"{artifact['id']}:{artifact.get('checksum_sha256') or artifact.get('size_bytes')}"
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    fd, tmp = tempfile.mkstemp(prefix="raikou-optical-", suffix=".npz")
    os.close(fd)
    try:
        storage.download_file(str(artifact["storage_key"]), tmp)
        with np.load(tmp) as data:
            loaded = (np.asarray(data["reduced"]), np.asarray(data["valid"], bool))
    finally:
        os.unlink(tmp)
    with _lock:
        _cache[key] = loaded
        _cache.move_to_end(key)
        while len(_cache) > settings.OPTICAL_SIMILAR_CACHE_RUNS:
            _cache.popitem(last=False)
    return loaded


def seed_token(grid: OpticalGrid, token_px: int, lon: float, lat: float) -> tuple[int, int]:
    pixel = grid.pixel_of(lon, lat)
    if pixel is None:
        raise SimilarError(422, "That point is outside this run's area.")
    return pixel[0] // token_px, pixel[1] // token_px


def run_heatmap(reduced: np.ndarray, valid: np.ndarray, row: int, col: int, *, grid: OpticalGrid, token_px: int) -> dict[str, Any]:
    """The heatmap PNG, its placement, and how much of the run looks alike at a few thresholds."""
    if not (0 <= row < valid.shape[0] and 0 <= col < valid.shape[1]):
        raise SimilarError(422, "That point is outside this run's area.")
    if not valid[row, col]:
        raise SimilarError(422, "That spot has no clear observation in this run; click somewhere mapped.")
    sim = emb.similarity_map(reduced, valid, row, col)
    png = emb.png_bytes(emb.colorize(sim))
    ok = ~np.isnan(sim)
    n_ok = int(ok.sum())
    cell_km2 = (token_px * grid.res) ** 2 / 1e6
    areas = []
    for threshold in AREA_THRESHOLDS:
        n = int(np.count_nonzero(sim[ok] >= threshold))
        areas.append(
            {
                "threshold": threshold,
                # 0.0001 km2 (100 m2) resolves single 40 m cells (0.0016 km2).
                "area_sqkm": round(n * cell_km2, 4),
                "share_pct": round(100.0 * n / n_ok, 1) if n_ok else 0.0,
            }
        )
    height, width = sim.shape
    return {
        "image": {
            "data_url": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
            "width": width,
            "height": height,
            "corners_lonlat": grid.corners_lonlat(),
            "value_range": [emb.VMIN, emb.VMAX],
            "colormap": "magma",
        },
        "areas": areas,
    }


def window_match(payload: dict[str, Any], score: float | None = None, scene_name: str | None = None) -> dict[str, Any]:
    return {
        "scene_id": payload.get("scene_id"),
        "scene_name": scene_name,
        "score": None if score is None else round(float(score), 4),
        "bbox_lonlat": list(payload.get("bbox_lonlat") or []),
        "center_lonlat": list(payload.get("center_lonlat") or []),
        "dates": list(payload.get("dates") or []),
        "dominant_class": payload.get("dominant_class"),
        "class_shares": dict(payload.get("class_shares") or {}),
    }


def library_matches(
    store: Any,
    *,
    scene_id: str,
    owner_id: str,
    model_version: str,
    window_tokens: int,
    row: int,
    col: int,
    limit: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], tuple[int, int]]:
    """The seed window's point and its nearest windows in the owner's other runs."""
    window = (row // window_tokens, col // window_tokens)
    point_id = emb.window_point_id(scene_id, model_version, *window)
    collection = settings.OPTICAL_QDRANT_COLLECTION
    seed = store.retrieve_owned_point(collection, point_id, owner_id=owner_id)
    if seed is None:
        raise SimilarError(422, "The 640 m window around that spot has too little clear ground to compare; click somewhere else.")
    hits = store.search_owner_windows(collection, seed["vector"], owner_id=owner_id, exclude_scene_id=scene_id, limit=limit)
    return seed, hits, window
