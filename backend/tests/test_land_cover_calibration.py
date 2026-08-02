"""Tests for radiometric calibration and the land-cover domain guard.

These cover the parts that fail silently in production: a calibration LUT that
is interpolated wrongly still returns plausible decibels, and a land-cover block
that fails open still returns plausible class names.
"""

from __future__ import annotations

import io
import math

import numpy as np
import pytest

from app.services.ingestion.calibration import (
    CalibrationError,
    SigmaNoughtLUT,
    calibration_members,
    dn_to_sigma0_db,
    parse_calibration_xml,
)
from app.services.models.land_cover import (
    BEN19_LABELS,
    BEN_S1_MEAN_DB,
    BEN_S1_STD_DB,
    LandCoverResult,
    assess_domain,
    build_land_cover_block,
    normalize_sigma0_db,
)
from app.services.processing.chat_policy import environment_answer


CALIBRATION_XML = """<?xml version="1.0" encoding="UTF-8"?>
<calibration>
  <adsHeader><polarisation>VV</polarisation></adsHeader>
  <calibrationInformation>
    <absoluteCalibrationConstant>1.147000e+00</absoluteCalibrationConstant>
  </calibrationInformation>
  <calibrationVectorList count="2">
    <calibrationVector>
      <line>0</line>
      <pixel count="2">0 100</pixel>
      <sigmaNought count="2">100.0 200.0</sigmaNought>
    </calibrationVector>
    <calibrationVector>
      <line>100</line>
      <pixel count="2">0 100</pixel>
      <sigmaNought count="2">300.0 400.0</sigmaNought>
    </calibrationVector>
  </calibrationVectorList>
</calibration>
"""


def _lut() -> SigmaNoughtLUT:
    return parse_calibration_xml(io.BytesIO(CALIBRATION_XML.encode("utf-8")))


def test_parse_calibration_reads_grid_and_constant() -> None:
    lut = _lut()

    assert lut.polarisation == "VV"
    assert lut.absolute_calibration_constant == pytest.approx(1.147)
    assert lut.lines.tolist() == [0, 100]
    assert lut.pixels.tolist() == [0, 100]
    assert lut.sigma_nought.tolist() == [[100.0, 200.0], [300.0, 400.0]]


def test_window_interpolates_bilinearly_across_both_axes() -> None:
    lut = _lut()

    window = lut.window(row_off=0, col_off=0, height=101, width=101)

    assert window.shape == (101, 101)
    # Corners reproduce the LUT nodes exactly.
    assert window[0, 0] == pytest.approx(100.0)
    assert window[0, 100] == pytest.approx(200.0)
    assert window[100, 0] == pytest.approx(300.0)
    assert window[100, 100] == pytest.approx(400.0)
    # Centre is the mean of the four corners for a bilinear surface.
    assert window[50, 50] == pytest.approx(250.0)
    # A window read at an offset must agree with the same pixels read whole; an
    # off-by-one in the azimuth weighting shows up here and nowhere else.
    offset = lut.window(row_off=25, col_off=40, height=10, width=10)
    np.testing.assert_allclose(offset, window[25:35, 40:50])


def test_window_clamps_beyond_the_last_calibration_node() -> None:
    lut = _lut()

    beyond = lut.window(row_off=150, col_off=150, height=2, width=2)

    # The LUT normally overruns the raster, but the final rows of a scene can
    # sit past the last node; clamping keeps them finite instead of
    # extrapolating a LUT that has no physical basis outside its support.
    assert np.all(np.isfinite(beyond))
    assert beyond[0, 0] == pytest.approx(400.0)


def test_dn_to_sigma0_db_matches_the_esa_definition() -> None:
    # sigma0 = DN^2 / A^2, so DN == A is 0 dB and DN == A/10 is -20 dB.
    calibration = np.full((2, 2), 500.0)

    np.testing.assert_allclose(
        dn_to_sigma0_db(np.full((2, 2), 500.0), calibration), np.zeros((2, 2)), atol=1e-9
    )
    np.testing.assert_allclose(
        dn_to_sigma0_db(np.full((2, 2), 50.0), calibration), np.full((2, 2), -20.0), atol=1e-9
    )


def test_dn_to_sigma0_db_keeps_nodata_finite() -> None:
    result = dn_to_sigma0_db(np.zeros((3, 3)), np.full((3, 3), 500.0))

    # Zero DN marks no-data and would otherwise be -inf, which poisons every
    # percentile and mean computed downstream.
    assert np.all(np.isfinite(result))
    assert np.all(result == -50.0)


def test_ragged_calibration_grid_is_rejected() -> None:
    ragged = CALIBRATION_XML.replace(
        '<pixel count="2">0 100</pixel>\n      <sigmaNought count="2">300.0 400.0</sigmaNought>',
        '<pixel count="3">0 50 100</pixel>\n      <sigmaNought count="3">300.0 350.0 400.0</sigmaNought>',
    )

    with pytest.raises(CalibrationError, match="common range grid"):
        parse_calibration_xml(io.BytesIO(ragged.encode("utf-8")))


def test_calibration_members_excludes_the_noise_luts() -> None:
    names = [
        "S1.SAFE/annotation/calibration/calibration-s1a-iw-grd-vv-001.xml",
        "S1.SAFE/annotation/calibration/noise-s1a-iw-grd-vv-001.xml",
        "S1.SAFE/annotation/s1a-iw-grd-vv-001.xml",
        "S1.SAFE/annotation/rfi/rfi-s1a-iw-grd-vv-001.xml",
    ]

    # noise-*.xml shares the calibrationVector layout, so a looser filter parses
    # thermal noise as if it were the calibration constant.
    assert calibration_members(names) == [
        "S1.SAFE/annotation/calibration/calibration-s1a-iw-grd-vv-001.xml"
    ]


def test_normalization_puts_training_mean_at_zero() -> None:
    vv = np.full((2, 2), BEN_S1_MEAN_DB["VV"])
    vh = np.full((2, 2), BEN_S1_MEAN_DB["VH"])

    stacked = normalize_sigma0_db(vv, vh)

    assert stacked.shape == (2, 2, 2)
    np.testing.assert_allclose(stacked, np.zeros((2, 2, 2)), atol=1e-6)
    # Channel 0 must be VV: v0.1.1 of these weights used the reverse order and
    # the mistake is invisible because both channels are plausible backscatter.
    one_sigma = normalize_sigma0_db(vv + BEN_S1_STD_DB["VV"], vh)
    assert one_sigma[0].mean() == pytest.approx(1.0, abs=1e-5)
    assert one_sigma[1].mean() == pytest.approx(0.0, abs=1e-5)


def test_label_order_is_lexicographic() -> None:
    # The head is trained against sorted(NEW_LABELS_ORIGINAL_ORDER); reading it
    # in the CORINE declaration order silently relabels every class.
    assert list(BEN19_LABELS) == sorted(BEN19_LABELS)
    assert len(BEN19_LABELS) == 19
    assert BEN19_LABELS[11] == "Marine waters"
    assert BEN19_LABELS[18] == "Urban fabric"


@pytest.mark.parametrize(
    ("longitude", "latitude", "expected"),
    [
        (14.4, 46.1, "plausibly_in_domain"),   # Austria, inside the training set
        (79.7, 13.8, "out_of_domain"),          # Bay of Bengal coast
        (-73.9, 40.7, "out_of_domain"),         # New York
        (None, None, "unknown"),
    ],
)
def test_domain_assessment(longitude, latitude, expected) -> None:
    assert assess_domain(longitude, latitude)["assessment"] == expected


def _result() -> LandCoverResult:
    return LandCoverResult(
        per_class_mean_probability={label: 0.0 for label in BEN19_LABELS},
        per_class_presence_fraction={
            **{label: 0.0 for label in BEN19_LABELS},
            "Marine waters": 0.28,
        },
        windows_scored=384,
        windows_attempted=400,
    )


def test_land_cover_block_fails_closed_without_a_georeference() -> None:
    block = build_land_cover_block(_result(), domain=assess_domain(None, None))

    # A Sentinel-1 GRD measurement band has no CRS, so an unverifiable location
    # is the common case. Defaulting it to usable would publish European land
    # cover for an arbitrary point on Earth.
    assert block["status"] == "domain_unverified"
    assert block["usable_as_land_cover"] is False


def test_land_cover_block_marks_out_of_domain_but_keeps_scores() -> None:
    block = build_land_cover_block(_result(), domain=assess_domain(79.7, 13.8))

    assert block["status"] == "out_of_domain"
    assert block["usable_as_land_cover"] is False
    # Retained for a reviewer even though chat may not quote them.
    assert [item["label"] for item in block["classes"]] == ["Marine waters"]


def test_land_cover_block_is_never_detector_evidence() -> None:
    block = build_land_cover_block(_result(), domain=assess_domain(14.4, 46.1))

    assert block["status"] == "available"
    assert block["is_detector_evidence"] is False
    assert block["is_calibrated_confidence"] is False
    assert block["review_required"] is True
    assert block["provenance"]["reported_metrics"]["average_precision_macro"] == pytest.approx(0.628376)


def test_environment_answer_prefers_land_cover_when_in_domain() -> None:
    block = build_land_cover_block(_result(), domain=assess_domain(14.4, 46.1))

    answer = environment_answer("Is there any water?", {"label": "mixed_or_indeterminate"}, block)

    assert "Marine waters" in answer
    assert "not a segmentation" in answer
    assert "not detector evidence" in answer


def test_environment_answer_withholds_out_of_domain_classes() -> None:
    block = build_land_cover_block(_result(), domain=assess_domain(79.7, 13.8))

    answer = environment_answer("Is there any vegetation?", {"label": "likely_water_dominant"}, block)

    # The class names must not leak into an answer for a scene the model cannot
    # describe, and the fallback must still be the honest heuristic.
    assert "Marine waters" not in answer
    assert "cannot confirm vegetation" in answer
    assert "outside the training footprint" in answer
    assert "water-dominant" in answer


def test_environment_answer_still_works_without_land_cover() -> None:
    answer = environment_answer("Is there any vegetation?", {"label": "likely_water_dominant"})

    assert "cannot confirm vegetation" in answer
    assert "not land-cover classification" in answer
