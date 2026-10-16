"""The optical run grid: snapped, sized for the model, always one Process API request."""

from __future__ import annotations

import math

import pytest

from app.core.config import Settings, settings
from app.services.optical import grid

# About 9.5 x 10 km over Visakhapatnam, the smoke-test area.
VIZAG = (83.20, 17.68, 83.29, 17.77)


def _field_le(name: str) -> float:
    return next(item.le for item in Settings.model_fields[name].metadata if hasattr(item, "le"))


@pytest.mark.parametrize(
    ("lon", "lat", "epsg"),
    [(68.7, 23.0, 32642), (83.24, 17.72, 32644), (96.9, 27.5, 32647), (83.24, -17.72, 32744)],
)
def test_utm_zone_follows_the_box_centre(lon, lat, epsg):
    assert grid.utm_epsg(lon, lat) == epsg


def test_grid_is_snapped_and_sized_for_the_model():
    g = grid.grid_for_box(*VIZAG, max_side_km=24)

    assert g.epsg == 32644
    assert g.x0 % grid.SNAP_M == 0 and g.y_top % grid.SNAP_M == 0
    assert g.width % grid.MULTIPLE_PX == 0 and g.height % grid.MULTIPLE_PX == 0
    assert settings.OPTICAL_MIN_SIDE_PX <= min(g.width, g.height)
    assert max(g.width, g.height) <= grid.MAX_PX
    assert g.transform == (10.0, 0.0, g.x0, 0.0, -10.0, g.y_top)


def test_grid_contains_the_whole_drawn_box():
    west, south, east, north = VIZAG
    g = grid.grid_for_box(west, south, east, north, max_side_km=24)
    left, bottom, right, top = g.bounds
    mid_lon, mid_lat = (west + east) / 2, (south + north) / 2
    lons = [west, east, east, west, mid_lon, mid_lon, west, east]
    lats = [south, south, north, north, south, north, mid_lat, mid_lat]

    xs, ys = grid._transform(4326, g.epsg, lons, lats)

    assert all(left <= x <= right for x in xs)
    assert all(bottom <= y <= top for y in ys)


def test_a_tiny_box_is_padded_to_one_model_window():
    g = grid.grid_for_box(83.240, 17.720, 83.241, 17.721, max_side_km=24)

    assert min(g.width, g.height) >= settings.OPTICAL_MIN_SIDE_PX


def test_a_box_over_the_side_cap_is_refused_with_the_cap_in_the_message():
    with pytest.raises(grid.GridError, match="at most 15 km"):
        grid.grid_for_box(83.0, 17.5, 83.2, 17.7, max_side_km=15)


@pytest.mark.parametrize(
    "box",
    [
        (83.3, 17.7, 83.2, 17.8),  # west past east
        (83.2, 17.8, 83.3, 17.7),  # south above north
        (179.9, 0.0, -179.9, 1.0),  # across the antimeridian
    ],
)
def test_inverted_or_antimeridian_boxes_are_refused(box):
    with pytest.raises(grid.GridError):
        grid.grid_for_box(*box, max_side_km=24)


@pytest.mark.parametrize("name", ["OPTICAL_MAX_SIDE_KM_SINGLE", "OPTICAL_MAX_SIDE_KM_STACK"])
def test_no_configurable_side_cap_can_exceed_one_request(name):
    """Snapping to 60 m and rounding up to 8 px can add up to 140 m to a side."""
    worst_px = math.ceil((_field_le(name) * 1000 + grid.SNAP_M) / grid.RES_M)
    worst_px = math.ceil(worst_px / grid.MULTIPLE_PX) * grid.MULTIPLE_PX

    assert worst_px <= grid.MAX_PX


@pytest.mark.parametrize(
    ("lon", "lat"),
    # Zone 44's centre and its edges, south to north, where UTM convergence
    # makes a lat/lon box widest on the grid.
    [(81.0, 20.0), (78.02, 30.0), (83.75, 8.5), (72.1, 34.5)],
)
def test_any_box_that_passes_the_side_check_fits_one_request(lon, lat):
    cap = _field_le("OPTICAL_MAX_SIDE_KM_SINGLE")
    for side_deg in (0.20, 0.215, 0.225, 0.235):
        try:
            g = grid.grid_for_box(lon, lat, lon + side_deg, lat + side_deg, max_side_km=cap)
        except grid.GridError as exc:
            # Refused by the side check, never by the 2500 px guard behind it.
            assert "Draw at most" in str(exc)
            continue
        assert max(g.width, g.height) <= grid.MAX_PX


def test_grid_round_trips_through_its_stored_form():
    g = grid.grid_for_box(*VIZAG, max_side_km=24)
    stored = g.as_dict()

    assert grid.OpticalGrid.from_dict(stored) == g
    assert stored["transform"] == [10.0, 0.0, g.x0, 0.0, -10.0, g.y_top]
    assert stored["bounds"] == list(g.bounds)


def test_an_incomplete_stored_grid_is_refused():
    with pytest.raises(grid.GridError):
        grid.OpticalGrid.from_dict({"epsg": 32644, "x0": 735120.0})


def test_corners_are_a_maplibre_image_quad():
    top_left, top_right, bottom_right, bottom_left = grid.grid_for_box(*VIZAG, max_side_km=24).corners_lonlat()

    assert top_left[0] < top_right[0] and bottom_left[0] < bottom_right[0]
    assert top_left[1] > bottom_left[1] and top_right[1] > bottom_right[1]


def test_processing_unit_estimate_follows_the_sentinel_hub_rule():
    # 1000 x 1000 px is 3.81 tiles of 512^2, times 13/3 for the bands.
    assert grid.estimate_pu(1000, 1000) == pytest.approx(16.53, abs=0.01)
    assert grid.estimate_pu(1000, 1000, dates=3) == pytest.approx(49.59, abs=0.01)
    # Sentinel Hub bills at least 0.01 of a tile.
    assert grid.estimate_pu(10, 10) == pytest.approx(0.04, abs=0.001)
