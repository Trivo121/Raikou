"""Tests for dual-pol scattering description.

The quiet failure here is a ratio computed in decibels instead of linear power.
Speckle is multiplicative, so averaging dB averages the logarithm of a noisy
quantity and biases every window low; the result still looks like plausible
backscatter and only shows up as mechanism shares that drift with window size.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.services.processing.scattering import (
    MECHANISM_DESCRIPTIONS,
    ScatteringThresholds,
    build_scattering_block,
    classify_window,
    summarize_window,
    WindowScattering,
)


THRESHOLDS = ScatteringThresholds(
    water_max_vv_db=-15.0,
    water_max_ratio_db=-9.0,
    urban_min_vv_db=-7.0,
    urban_max_ratio_db=-8.0,
    volume_min_rvi=0.55,
)


def _flat(vv_db: float, vh_db: float, size: int = 16):
    return np.full((size, size), vv_db), np.full((size, size), vh_db)


def test_summary_multilooks_in_linear_power_not_decibels() -> None:
    # Two pixels 20 dB apart. The linear mean is dominated by the bright one and
    # lands at -3.0 dB; averaging in dB would give -10.0 and understate the
    # window by 7 dB. Speckle makes this the common case, not a corner case.
    vv = np.array([[0.0, -20.0]])
    vh = np.array([[-10.0, -10.0]])

    summary = summarize_window(vv, vh)

    assert summary.vv_db == pytest.approx(-3.0, abs=0.05)
    assert summary.vh_db == pytest.approx(-10.0, abs=1e-6)


def test_cross_pol_ratio_is_vh_minus_vv() -> None:
    summary = summarize_window(*_flat(-8.0, -16.0))

    assert summary.cross_pol_ratio_db == pytest.approx(-8.0, abs=1e-6)


def test_rvi_rises_with_depolarisation() -> None:
    # A surface return barely depolarises; a canopy does so strongly.
    surface = summarize_window(*_flat(-10.0, -25.0))
    canopy = summarize_window(*_flat(-10.0, -11.0))

    assert surface.radar_vegetation_index < 0.15
    assert canopy.radar_vegetation_index > 0.7
    assert 0.0 <= surface.radar_vegetation_index <= 2.0


def test_noise_limited_pixels_are_counted_not_hidden() -> None:
    # Half the VH pixels sit on the denoising clamp: the signal never rose above
    # the receiver, so a ratio computed here is meaningless.
    vv = np.full((2, 2), -12.0)
    vh = np.array([[-50.0, -50.0], [-18.0, -18.0]])

    summary = summarize_window(vv, vh, noise_floor_db=-50.0)

    assert summary.noise_limited_fraction == pytest.approx(0.5)


@pytest.mark.parametrize(
    ("vv_db", "vh_db", "expected"),
    [
        (-20.0, -32.0, "smooth_surface"),   # dark and barely depolarising: water
        (-3.0, -14.0, "double_bounce"),     # very bright, low ratio: built-up
        (-10.0, -11.0, "volume"),           # strong depolarisation: canopy
        (-13.0, -25.0, "rough_surface"),    # mid brightness, low ratio: bare
    ],
)
def test_mechanism_assignment(vv_db, vh_db, expected) -> None:
    assert classify_window(summarize_window(*_flat(vv_db, vh_db)), THRESHOLDS) == expected


def test_water_is_tested_before_bare_ground() -> None:
    # Water and bare soil both barely depolarise, so only the absolute level
    # separates them. If the general rules ran first, every water window would
    # fall through to rough_surface.
    water = summarize_window(*_flat(-22.0, -34.0))
    bare = summarize_window(*_flat(-13.0, -25.0))

    assert classify_window(water, THRESHOLDS) == "smooth_surface"
    assert classify_window(bare, THRESHOLDS) == "rough_surface"


def test_block_reports_shares_and_carries_its_limits() -> None:
    windows = (
        [summarize_window(*_flat(-22.0, -34.0)) for _ in range(2)]
        + [summarize_window(*_flat(-10.0, -11.0)) for _ in range(6)]
        + [summarize_window(*_flat(-3.0, -14.0)) for _ in range(2)]
    )

    block = build_scattering_block(windows, THRESHOLDS, is_denoised=True)

    assert block is not None
    assert block["windows_scored"] == 10
    shares = {m["mechanism"]: m["fraction"] for m in block["mechanisms"]}
    assert shares["smooth_surface"] == pytest.approx(0.2)
    assert shares["volume"] == pytest.approx(0.6)
    assert shares["double_bounce"] == pytest.approx(0.2)
    # This is a measurement of geometry, and it is not a land use. Both must
    # travel with the block or a reader will take "volume" to mean "forest".
    assert block["is_land_use_classification"] is False
    assert block["is_detector_evidence"] is False
    assert any("not land use" in item for item in block["limitations"])


def test_block_discards_noise_limited_windows() -> None:
    good = summarize_window(*_flat(-10.0, -11.0))
    dead = WindowScattering(-12.0, -50.0, -38.0, 0.0, noise_limited_fraction=0.9)

    block = build_scattering_block([good, dead], THRESHOLDS, is_denoised=True)

    assert block is not None
    assert block["windows_scored"] == 1
    assert block["windows_discarded_noise_limited"] == 1


def test_block_is_none_when_everything_is_noise_limited() -> None:
    dead = [WindowScattering(-12.0, -50.0, -38.0, 0.0, noise_limited_fraction=1.0)] * 4

    assert build_scattering_block(dead, THRESHOLDS, is_denoised=True) is None


def test_every_mechanism_has_a_plain_language_description() -> None:
    # These reach the user directly; a mechanism without one would render as a
    # bare identifier like "double_bounce" in chat.
    for mechanism in MECHANISM_DESCRIPTIONS.values():
        assert mechanism and mechanism[0].islower()
