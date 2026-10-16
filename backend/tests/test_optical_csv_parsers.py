"""The worker's CSV parsers against the release's own CSV writers.

The optical stage reads lulc_stats.csv and agreement_matrix.csv written by
raikou_phase0.gis_export. Importing that writer from training/optical_phase0,
the code the release was built from, keeps the two in step: a renamed column
fails here instead of as a record full of nulls on the box. gis_export needs
only numpy at import time.
"""

from __future__ import annotations

import importlib
from pathlib import Path
import sys

import numpy as np
import pytest

from app.services.optical.record import build_optical_record
from app.workers.optical_stages import _parse_agreement_csv, _parse_stats_csv

PHASE0 = Path(__file__).resolve().parents[2] / "training" / "optical_phase0"


@pytest.fixture(scope="module")
def gis_export():
    sys.path.insert(0, str(PHASE0))
    try:
        yield importlib.import_module("raikou_phase0.gis_export")
    finally:
        sys.path.remove(str(PHASE0))
        for name in [n for n in sys.modules if n == "raikou_phase0" or n.startswith("raikou_phase0.")]:
            del sys.modules[name]


def _maps(gis_export, *, drop_class: int | None = None):
    """A model map that agrees with its reference on about 80% of pixels."""
    ignore = gis_export.C.IGNORE
    rng = np.random.default_rng(7)
    model = rng.integers(0, gis_export.C.NUM_CLASSES, size=(64, 64)).astype(np.uint8)
    if drop_class is not None:
        model[model == drop_class] = (drop_class + 1) % gis_export.C.NUM_CLASSES
    reference = model.copy()
    flip = rng.random(model.shape) < 0.2
    reference[flip] = (reference[flip] + 1) % gis_export.C.NUM_CLASSES
    model[:4] = ignore  # unmapped: cloud in every date
    reference[:, :3] = ignore  # outside the WorldCover tile
    return model, reference


def _same(got, expected) -> bool:
    return got is None if expected is None else got == pytest.approx(expected)


@pytest.mark.parametrize("heldout", [{"T1": 0.13, "T3": 0.11}, None])
def test_agreement_parser_reads_what_the_release_writes(gis_export, tmp_path, heldout):
    model, reference = _maps(gis_export)
    counts = gis_export.agreement_matrix(model, reference)
    path = tmp_path / "agreement_matrix.csv"
    gis_export.write_agreement_csv(str(path), counts, "2026-04-12", heldout)

    parsed = _parse_agreement_csv(path)

    assert parsed["classes"] == gis_export.C.CLASS_NAMES
    assert np.allclose(parsed["matrix_km2"], counts * gis_export.PX_KM2, atol=5e-5)
    assert parsed["notes"][0].startswith("AGREEMENT matrix, not a change map")
    assert any("T1: 13.0%" in note for note in parsed["notes"]) is (heldout is not None)


@pytest.mark.parametrize("drop_class", [None, 3])
def test_stats_parser_feeds_the_record_unchanged(gis_export, tmp_path, drop_class):
    model, reference = _maps(gis_export, drop_class=drop_class)
    score = np.where(model != gis_export.C.IGNORE, 80.0, np.nan)
    rows = gis_export.class_stats(model, reference, score)
    path = tmp_path / "lulc_stats.csv"
    gis_export.write_csv(str(path), rows)

    record = build_optical_record(
        scene={"id": "scene-1", "name": "test"},
        acquisition=None,
        summary={"stats": _parse_stats_csv(path), "timesteps": [{}], "timesteps_used": [0]},
    )

    by_name = {c["class_name"]: c for c in record["lulc"]["classes"]}
    assert list(by_name) == gis_export.C.CLASS_NAMES
    for row in rows:
        got = by_name[row["class_name"]]
        assert got["class_id"] == row["class_id"]
        assert _same(got["area_sqkm"], row["area_sqkm"])
        assert _same(got["share_pct"], row["share_of_mapped_%"])
        assert _same(got["mean_score_pct"], row["mean_score_%"])
        assert _same(got["worldcover_2021_area_sqkm"], row["worldcover_2021_area_sqkm"])
        assert _same(got["same_class_in_worldcover_2021_pct"], row["same_class_in_worldcover_2021_%"])
    assert sum(c["share_pct"] for c in by_name.values()) == pytest.approx(100, abs=0.05)
    if drop_class is not None:
        absent = by_name[gis_export.C.CLASS_NAMES[drop_class]]
        assert absent["share_pct"] == 0.0 and absent["mean_score_pct"] is None
