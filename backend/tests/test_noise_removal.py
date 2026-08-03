"""Tests for thermal-noise removal.

The failure mode this guards against is silent: a noise LUT applied with the
wrong sign, the wrong units, or the wrong sub-swath still produces plausible
decibels, and the error only shows up as a cross-pol ratio that is wrong over
water -- the one place the ratio is load-bearing.
"""

from __future__ import annotations

import io

import numpy as np
import pytest

from app.services.ingestion.calibration import (
    CalibrationError,
    NoiseLUT,
    dn_to_sigma0_db,
    noise_members,
    parse_noise_xml,
)


NOISE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<noise>
  <adsHeader><polarisation>VH</polarisation></adsHeader>
  <noiseRangeVectorList count="2">
    <noiseRangeVector>
      <line>0</line>
      <pixel count="2">0 100</pixel>
      <noiseRangeLut count="2">100.0 200.0</noiseRangeLut>
    </noiseRangeVector>
    <noiseRangeVector>
      <line>100</line>
      <pixel count="2">0 100</pixel>
      <noiseRangeLut count="2">300.0 400.0</noiseRangeLut>
    </noiseRangeVector>
  </noiseRangeVectorList>
  <noiseAzimuthVectorList count="2">
    <noiseAzimuthVector>
      <swath>IW1</swath>
      <firstAzimuthLine>0</firstAzimuthLine>
      <firstRangeSample>0</firstRangeSample>
      <lastAzimuthLine>100</lastAzimuthLine>
      <lastRangeSample>49</lastRangeSample>
      <line count="2">0 100</line>
      <noiseAzimuthLut count="2">2.0 2.0</noiseAzimuthLut>
    </noiseAzimuthVector>
    <noiseAzimuthVector>
      <swath>IW2</swath>
      <firstAzimuthLine>0</firstAzimuthLine>
      <firstRangeSample>50</firstRangeSample>
      <lastAzimuthLine>100</lastAzimuthLine>
      <lastRangeSample>100</lastRangeSample>
      <line count="2">0 100</line>
      <noiseAzimuthLut count="2">1.0 1.0</noiseAzimuthLut>
    </noiseAzimuthVector>
  </noiseAzimuthVectorList>
</noise>
"""


def _lut() -> NoiseLUT:
    return parse_noise_xml(io.BytesIO(NOISE_XML.encode("utf-8")))


def test_parse_noise_reads_range_grid_and_azimuth_blocks() -> None:
    lut = _lut()

    assert lut.polarisation == "VH"
    assert lut.lines.tolist() == [0, 100]
    assert lut.pixels.tolist() == [0, 100]
    assert lut.range_lut.tolist() == [[100.0, 200.0], [300.0, 400.0]]
    assert [block.swath for block in lut.azimuth_blocks] == ["IW1", "IW2"]


def test_noise_window_applies_the_per_swath_azimuth_scale() -> None:
    lut = _lut()

    window = lut.window(row_off=0, col_off=0, height=101, width=101)

    # An IW GRD is assembled from three sub-swaths with their own receive
    # timing, so the azimuth term is keyed on range sample. Applying one swath's
    # factor across the full width is the mistake this catches: column 0 is IW1
    # (x2.0) and column 100 is IW2 (x1.0), from the same range node value.
    assert window[0, 0] == pytest.approx(200.0)
    assert window[0, 100] == pytest.approx(200.0)
    assert window[100, 0] == pytest.approx(600.0)
    assert window[100, 100] == pytest.approx(400.0)


def test_noise_window_agrees_with_itself_at_an_offset() -> None:
    lut = _lut()
    whole = lut.window(row_off=0, col_off=0, height=101, width=101)

    offset = lut.window(row_off=25, col_off=40, height=10, width=10)

    np.testing.assert_allclose(offset, whole[25:35, 40:50])


def test_ragged_noise_grid_is_rejected() -> None:
    ragged = NOISE_XML.replace(
        '<pixel count="2">0 100</pixel>\n      <noiseRangeLut count="2">300.0 400.0</noiseRangeLut>',
        '<pixel count="3">0 50 100</pixel>\n      <noiseRangeLut count="3">300.0 350.0 400.0</noiseRangeLut>',
    )

    with pytest.raises(CalibrationError, match="common range grid"):
        parse_noise_xml(io.BytesIO(ragged.encode("utf-8")))


def test_noise_members_excludes_the_calibration_luts() -> None:
    names = [
        "S1.SAFE/annotation/calibration/noise-s1a-iw-grd-vh-002.xml",
        "S1.SAFE/annotation/calibration/calibration-s1a-iw-grd-vh-002.xml",
        "S1.SAFE/annotation/s1a-iw-grd-vh-002.xml",
    ]

    assert noise_members(names) == ["S1.SAFE/annotation/calibration/noise-s1a-iw-grd-vh-002.xml"]


def test_subtracting_noise_lowers_sigma0() -> None:
    # DN^2 = 250000, noise = 90000, A = 500 -> (250000-90000)/250000 = 0.64
    calibration = np.full((2, 2), 500.0)
    dn = np.full((2, 2), 500.0)

    plain = dn_to_sigma0_db(dn, calibration)
    denoised = dn_to_sigma0_db(dn, calibration, noise=np.full((2, 2), 90000.0))

    assert plain == pytest.approx(np.zeros((2, 2)), abs=1e-9)
    np.testing.assert_allclose(denoised, np.full((2, 2), 10 * np.log10(0.64)), atol=1e-9)
    # Denoising can only remove power. A sign error here reads as a plausible
    # brightening rather than as an error.
    assert np.all(denoised < plain)


def test_signal_at_or_below_the_noise_floor_clamps_instead_of_going_nan() -> None:
    calibration = np.full((3, 3), 500.0)
    dn = np.full((3, 3), 300.0)

    # Noise exceeds the measured power: the pixel never rose above the receiver.
    result = dn_to_sigma0_db(dn, calibration, noise=np.full((3, 3), 200000.0))

    assert np.all(np.isfinite(result))
    assert np.all(result == -50.0)


def test_omitting_noise_is_unchanged() -> None:
    # The BEN land-cover path calls this without noise and must keep the exact
    # representation its published metrics were measured against.
    calibration = np.full((4, 4), 620.0)
    dn = np.linspace(1, 5000, 16).reshape(4, 4)

    np.testing.assert_array_equal(
        dn_to_sigma0_db(dn, calibration), dn_to_sigma0_db(dn, calibration, noise=None)
    )
