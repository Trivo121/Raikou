"""Spectral index medians per date and mapped class, over clear pixels only."""

from __future__ import annotations

import csv

import numpy as np
import pytest

from app.services.optical import indices
from app.services.optical.process_api import S2_BANDS

NAMES = ["Built-up", "Agriculture", "Forest & natural vegetation", "Barren / wasteland", "Water bodies & wetlands"]


def _stack(T: int = 2, h: int = 4, w: int = 4) -> np.ndarray:
    dn = np.full((T, 12, h, w), 1000, np.uint16)
    dn[:, S2_BANDS.index("B04")] = 1000
    dn[:, S2_BANDS.index("B08")] = 3000  # NDVI = 0.5
    dn[:, S2_BANDS.index("B03")] = 1500
    dn[:, S2_BANDS.index("B11")] = 500  # MNDWI = 0.5, NDBI = (500 - 3000) / 3500
    return dn


def test_band_positions_match_the_stored_stack():
    for band, position in indices.BAND_INDEX.items():
        assert S2_BANDS[position] == band


def test_medians_are_the_normalised_differences():
    dn = _stack(T=1)
    valid = np.ones((1, 4, 4), bool)
    class_map = np.full((4, 4), 2, np.uint8)

    rows = indices.class_index_medians(dn, valid, class_map, NAMES, ["2026-04-13"], used=[0])

    agriculture = next(r for r in rows if r["class_name"] == "Agriculture")
    assert agriculture["clear_pixels"] == 16
    assert agriculture["clear_area_sqkm"] == pytest.approx(0.0016)
    assert agriculture["ndvi_median"] == pytest.approx(0.5)
    assert agriculture["mndwi_median"] == pytest.approx(0.5)
    assert agriculture["ndbi_median"] == pytest.approx(round(-2500 / 3500, 4))


def test_cloudy_pixels_and_absent_classes_are_left_out():
    dn = _stack(T=2)
    dn[1, S2_BANDS.index("B08"), :, :2] = 1000  # NDVI 0 where date 2 is cloudy
    valid = np.ones((2, 4, 4), bool)
    valid[1, :, :2] = False
    class_map = np.full((4, 4), 3, np.uint8)
    class_map[0, 0] = 0  # unmapped

    rows = indices.class_index_medians(dn, valid, class_map, NAMES, ["2025-11-02", "2026-04-13"], used=[1])

    assert len(rows) == 2 * len(NAMES)
    forest = [r for r in rows if r["class_id"] == 3]
    assert [r["clear_pixels"] for r in forest] == [15, 8]
    assert forest[1]["ndvi_median"] == pytest.approx(0.5)  # the cloudy half never counts
    assert [r["used_by_model"] for r in forest] == [False, True]
    water = next(r for r in rows if r["class_id"] == 5 and r["date"] == "2025-11-02")
    assert water["clear_pixels"] == 0
    assert water["ndvi_median"] is None and water["mndwi_median"] is None


def test_the_csv_explains_itself_and_leaves_unknowns_blank(tmp_path):
    rows = indices.class_index_medians(
        _stack(T=1), np.ones((1, 4, 4), bool), np.full((4, 4), 1, np.uint8), NAMES, ["2026-04-13"], used=[0]
    )
    path = tmp_path / "spectral_indices.csv"

    indices.write_csv(path, rows)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("# Medians over pixels clear on that date")
    parsed = list(csv.DictReader(lines[1:]))
    assert list(parsed[0]) == indices.FIELDS
    assert parsed[0]["class_name"] == "Built-up" and parsed[0]["ndvi_median"] == "0.5"
    assert parsed[1]["ndvi_median"] == ""  # Agriculture is absent from this map
