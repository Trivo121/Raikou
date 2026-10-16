"""The Phase 0 OlmoEarth India release, loaded once per optical worker process.

The release folder holds the weights, the model card, the base architecture
config and the exact ``raikou_phase0`` code the model was trained, calibrated
and exported with. The worker imports that copy, not anything in this repo, so
inference here is the code path Notebook 3 benchmarked.

Only the optical worker calls ``get_model`` (it needs torch). The CPU stages and
the API read the card and benchmark through ``card()`` / ``benchmark()``, which
are plain JSON reads.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import threading
from functools import lru_cache
from types import SimpleNamespace
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_state: dict[str, Any] = {}


class ReleaseUnavailable(RuntimeError):
    """The release folder is missing, incomplete or does not match its checksums."""


def release_dir() -> str:
    return os.path.abspath(os.path.expanduser(settings.OLMOEARTH_LULC_RELEASE_DIR))


def _path(name: str) -> str:
    return os.path.join(release_dir(), name)


def _read_json(name: str) -> dict[str, Any]:
    path = _path(name)
    if not os.path.isfile(path):
        raise ReleaseUnavailable(f"{name} is missing from the optical model release at {release_dir()}.")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


@lru_cache(maxsize=1)
def card() -> dict[str, Any]:
    return _read_json("model_card.json")


@lru_cache(maxsize=1)
def benchmark() -> dict[str, Any] | None:
    try:
        data = _read_json("india_benchmark_metrics.json")
    except ReleaseUnavailable:
        return None
    return data.get("fp32") if isinstance(data, dict) else None


def card_summary() -> dict[str, Any]:
    """What the record and the API quote about the model, from the card alone."""
    c = card()
    evaluation = (c.get("evaluation") or {}).get("summary_sieved_fp32") or {}
    calibration = c.get("calibration") or {}

    def mode(m: str) -> dict[str, Any] | None:
        s = evaluation.get(m)
        if not s:
            return None
        return {
            "agreement_overall": s.get("overall_accuracy"),
            "mean_iou": s.get("mean_iou"),
            "macro_f1": s.get("macro_f1"),
            "per_class_iou": {k: (v or {}).get("iou") for k, v in (s.get("per_class") or {}).items()},
        }

    return {
        "name": c.get("name"),
        "version": c.get("version"),
        "base_model": (c.get("base_model") or {}).get("repo"),
        "classes": c.get("classes") or [],
        "single_date": mode("T1"),
        "seasonal_stack": mode("T3"),
        "reference": (c.get("evaluation") or {}).get("reference"),
        "temperature": calibration.get("temperature"),
        "calibration_meaning": calibration.get("meaning"),
        "licence": (c.get("base_model") or {}).get("licence"),
        "attribution": c.get("attribution") or {},
    }


def _verify_checksums() -> None:
    sums = _path("SHA256SUMS.txt")
    if not os.path.isfile(sums):
        raise ReleaseUnavailable("SHA256SUMS.txt is missing from the optical model release.")
    bad: list[str] = []
    with open(sums, encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            digest, rel = line.rstrip("\n").split("  ", 1)
            target = _path(rel)
            h = hashlib.sha256()
            try:
                with open(target, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 22), b""):
                        h.update(chunk)
            except OSError:
                bad.append(rel)
                continue
            if h.hexdigest() != digest:
                bad.append(rel)
    if bad:
        raise ReleaseUnavailable(f"The optical model release does not match its checksums: {bad[:5]}")


def phase0() -> SimpleNamespace:
    """Import the release's own raikou_phase0 package."""
    if not os.path.isfile(_path(os.path.join("raikou_phase0", "__init__.py"))):
        raise ReleaseUnavailable(f"No raikou_phase0 code inside the release at {release_dir()}.")
    root = release_dir()
    if root not in sys.path:
        sys.path.insert(0, root)
    from raikou_phase0 import bundle, infer, tiles  # noqa: PLC0415

    return SimpleNamespace(bundle=bundle, infer=infer, tiles=tiles)


def device() -> str:
    if settings.OLMOEARTH_LULC_DEVICE:
        return settings.OLMOEARTH_LULC_DEVICE
    import torch

    return "cuda" if torch.cuda.is_available() else "cpu"


def amp() -> str:
    # Autocast is CUDA-only here; on CPU the model runs in fp32.
    return settings.OLMOEARTH_LULC_AMP if device().startswith("cuda") else "fp32"


def get_model():
    """Load once per process (checksums first), then reuse."""
    with _lock:
        if "model" in _state:
            return _state["model"]
        _verify_checksums()
        rp = phase0()
        weights = _path(settings.OLMOEARTH_LULC_WEIGHTS)
        if not os.path.isfile(weights):
            raise ReleaseUnavailable(f"{settings.OLMOEARTH_LULC_WEIGHTS} is missing from the release.")
        model = rp.infer.load_model(weights, _path("model_card.json"), device=device())
        logger.info("Loaded the OlmoEarth India LULC release from %s on %s", release_dir(), device())
        _state["model"] = model
        return model
