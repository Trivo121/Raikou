"""What the API and the record read from the Phase 0 release, without torch."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.core.config import settings
from app.services.optical import lulc_model

CARD = {
    "name": "Raikou OlmoEarth India LULC (5 classes)",
    "version": "0.1.0",
    "base_model": {"repo": "allenai/OlmoEarth-v1_2-Base", "licence": "OlmoEarth Artifact License"},
    "classes": [{"id": 1, "name": "Built-up"}],
    "evaluation": {
        "reference": "label_raw = WorldCover 2021 remapped, unfiltered (agreement, not ground truth)",
        "summary_sieved_fp32": {
            "T1": {"overall_accuracy": 0.8712, "mean_iou": 0.61, "macro_f1": 0.74,
                   "per_class": {"Built-up": {"iou": 0.55, "producers_accuracy": 0.7, "users_accuracy": 0.72}}},
            "T3": {"overall_accuracy": 0.8934, "mean_iou": 0.65, "macro_f1": 0.77,
                   "per_class": {"Built-up": {"iou": 0.58, "producers_accuracy": 0.73, "users_accuracy": 0.75}}},
        },
    },
    "calibration": {"temperature": 1.31, "meaning": "calibrated probability that a pixel agrees with the reference"},
    "attribution": {"olmoearth": "Built on OlmoEarth v1.2 Base by Ai2"},
}


@pytest.fixture
def release(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OLMOEARTH_LULC_RELEASE_DIR", str(tmp_path))
    lulc_model.card.cache_clear()
    lulc_model.benchmark.cache_clear()
    yield tmp_path
    lulc_model.card.cache_clear()
    lulc_model.benchmark.cache_clear()


def _write_sums(root: Path, files: list[str]) -> None:
    lines = [f"{hashlib.sha256((root / name).read_bytes()).hexdigest()}  {name}" for name in files]
    (root / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_card_summary_quotes_agreement_never_accuracy(release):
    (release / "model_card.json").write_text(json.dumps(CARD), encoding="utf-8")

    summary = lulc_model.card_summary()

    assert summary["single_date"]["agreement_overall"] == 0.8712
    assert summary["seasonal_stack"]["per_class_iou"] == {"Built-up": 0.58}
    assert (summary["temperature"], summary["base_model"]) == (1.31, "allenai/OlmoEarth-v1_2-Base")
    assert "accuracy" not in json.dumps(summary)


def test_a_missing_card_makes_the_model_unavailable(release):
    with pytest.raises(lulc_model.ReleaseUnavailable):
        lulc_model.card_summary()


def test_the_benchmark_is_the_fp32_entry_and_optional(release):
    assert lulc_model.benchmark() is None

    lulc_model.benchmark.cache_clear()
    (release / "india_benchmark_metrics.json").write_text(
        json.dumps({"fp32": {"summary": {"T1": {}}}, "fp16": {"summary": {}}}), encoding="utf-8"
    )

    assert lulc_model.benchmark() == {"summary": {"T1": {}}}


def test_checksums_refuse_a_changed_or_missing_file(release):
    (release / "raikou_phase0").mkdir()
    (release / "raikou_phase0" / "__init__.py").write_text("__version__ = '0.1.0'\n", encoding="utf-8")
    (release / "weights.pt").write_bytes(b"weights")
    _write_sums(release, ["raikou_phase0/__init__.py", "weights.pt"])

    lulc_model._verify_checksums()

    (release / "weights.pt").write_bytes(b"tampered")
    with pytest.raises(lulc_model.ReleaseUnavailable, match="weights.pt"):
        lulc_model._verify_checksums()

    (release / "weights.pt").unlink()
    with pytest.raises(lulc_model.ReleaseUnavailable, match="weights.pt"):
        lulc_model._verify_checksums()


def test_a_release_without_its_code_is_refused(release):
    with pytest.raises(lulc_model.ReleaseUnavailable):
        lulc_model.phase0()


def test_autocast_is_used_only_on_cuda(monkeypatch):
    monkeypatch.setattr(settings, "OLMOEARTH_LULC_AMP", "fp16")
    monkeypatch.setattr(settings, "OLMOEARTH_LULC_DEVICE", "cpu")
    assert lulc_model.amp() == "fp32"

    monkeypatch.setattr(settings, "OLMOEARTH_LULC_DEVICE", "cuda:0")
    assert lulc_model.amp() == "fp16"


INSTALLED = Path(settings.OLMOEARTH_LULC_RELEASE_DIR)


@pytest.mark.skipif(not (INSTALLED / "model_card.json").is_file(), reason="the Phase 0 release is only on the box")
def test_the_installed_release_has_everything_the_api_quotes():
    lulc_model.card.cache_clear()
    lulc_model.benchmark.cache_clear()

    summary = lulc_model.card_summary()
    benchmark = lulc_model.benchmark()

    assert len(summary["classes"]) == 5
    for mode in ("single_date", "seasonal_stack"):
        assert 0.0 < summary[mode]["agreement_overall"] <= 1.0
        assert 0.0 < summary[mode]["mean_iou"] <= 1.0
    assert summary["temperature"] and summary["calibration_meaning"]
    assert {"T1", "T3"} <= set((benchmark or {}).get("summary") or {})
    assert (INSTALLED / settings.OLMOEARTH_LULC_WEIGHTS).is_file()
    assert (INSTALLED / "raikou_phase0" / "bundle.py").is_file()
