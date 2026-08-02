"""Tests for footprint geometry and the prose scene description.

These cover the two ways this code fails quietly: a footprint parsed with the
corner order transposed still yields plausible-looking extents, and a composed
description that drops a caveat still reads fluently.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from app.api.routes.evidence import (
    _compose_scene_description,
    _describe_land_cover,
    _describe_land_water,
    _describe_observations,
    _scene_geography,
)
from app.services.processing.scene_geography import (
    footprint_payload,
    format_coordinate,
    parse_footprint,
)


# The real footprint of S1D_IW_GRDH_1SDV_20260630T003057, as the manifest writes it.
REAL_FOOTPRINT = (
    "12.809979,80.688751 13.256035,78.380257 14.763914,78.676155 14.321618,81.000298"
)
SCENE_ID = UUID("b9cb2143-08a6-4722-954c-06bf7156b555")


def test_footprint_extent_matches_the_pixel_grid() -> None:
    footprint = parse_footprint(REAL_FOOTPRINT)

    assert footprint is not None
    # The raster is 25523 x 16749 px and IW GRDH ships at 10 m spacing, so the
    # pixel grid implies ~255 x 167 km. Agreement between two independent sources
    # is what proves the corners are read as 'lat,lon' and in polygon order; a
    # transposed pair still parses but lands hundreds of km away, not four.
    #
    # The azimuth edge measures ~171 km rather than 167: the footprint is a
    # parallelogram, not a rectangle, because the ground track is slanted, so its
    # edge is a little longer than the across-track extent. Measuring the corners
    # is still preferred over pixels x an assumed spacing, which silently breaks
    # for any product that is not IW GRDH.
    assert footprint.width_km == pytest.approx(255, abs=8)
    assert footprint.height_km == pytest.approx(167, abs=8)
    assert footprint.centroid_latitude == pytest.approx(13.7879, abs=1e-3)
    assert footprint.centroid_longitude == pytest.approx(79.6864, abs=1e-3)


def test_footprint_payload_is_small_enough_for_the_prompt() -> None:
    import json

    payload = footprint_payload(parse_footprint(REAL_FOOTPRINT))

    assert payload is not None
    # Regression anchor on the real product: corner-derived, so 171 rather than
    # the 167 the pixel grid implies. See the extent test for why they differ.
    assert payload["ground_extent_km"] == [255, 171]
    # This rides in every scene-scoped prompt; a verbose form crowds out evidence.
    assert len(json.dumps(payload, separators=(",", ":"))) < 220


@pytest.mark.parametrize(
    "bounding_box",
    ["", None, "not-a-footprint", "12.8,80.6 13.2", "999.0,80.6 13.2,78.3 14.7,78.6 14.3,81.0"],
)
def test_unparseable_footprints_return_none(bounding_box) -> None:
    # The three bare GeoTIFF scenes have no footprint at all; a scene must still
    # describe itself without one rather than raising mid-request.
    assert parse_footprint(bounding_box) is None


def test_format_coordinate_uses_hemispheres() -> None:
    assert format_coordinate(13.7879, 79.6864) == "13.79°N, 79.69°E"
    assert format_coordinate(-33.87, -151.21) == "33.87°S, 151.21°W"


def test_scene_geography_prefers_the_record_incidence_angle() -> None:
    # Ingestion's annotation glob matched the calibration/ subdirectory and wrote
    # null for every SAFE product, so the scenes row is stale while the record is
    # rebuilt from freshly extracted metadata.
    scene = {"metadata": {"bounding_box": REAL_FOOTPRINT, "incidence_angle": None, "orbit_direction": "DESCENDING"}}
    record = {"scene": {"metadata": {"incidence_angle": 38.95}, "raster": {"width_px": 25523, "height_px": 16749}}}

    geography = _scene_geography(scene, record)

    assert geography is not None
    assert geography["incidence_angle_deg"] == 38.95
    assert geography["orbit_direction"] == "descending"
    assert geography["raster_px"] == [25523, 16749]


def test_scene_geography_is_none_when_nothing_is_known() -> None:
    assert _scene_geography({"metadata": {}}, {}) is None


def test_land_water_reads_as_a_sentence_not_a_dict() -> None:
    described = _describe_land_water(
        {"label": "mixed_or_indeterminate", "land_fraction_estimate": 0.636,
         "water_fraction_estimate": 0.364, "backscatter_threshold_db": 20.3345,
         "is_calibrated_confidence": False}
    )

    assert described is not None
    assert "64% of the scene returns land-like backscatter and 36% water-like" in described
    assert "not a coastline map" in described
    # The old form printed these at the reader verbatim.
    assert "backscatter_threshold_db" not in described
    assert "is_calibrated_confidence" not in described
    assert "=" not in described


def test_land_cover_withheld_reason_is_stated_plainly() -> None:
    described = _describe_land_cover({"status": "out_of_domain", "classes": [{"label": "Marine waters"}]})

    assert described is not None
    assert "European" in described
    # An out-of-domain label space must never surface its class names as fact.
    assert "Marine waters" not in described


def test_land_cover_in_domain_names_classes() -> None:
    described = _describe_land_cover(
        {"status": "available", "classes": [{"label": "Marine waters", "present_in_window_fraction": 0.28}]}
    )

    assert described is not None
    assert "Marine waters across 28% of sampled windows" in described
    assert "not where" in described


def test_observations_read_as_a_sentence() -> None:
    described = _describe_observations(["bright", "no", "uniform", "smooth"])

    assert described is not None
    assert described.startswith("A visual probe of the downscaled overview finds the scene reads as bright overall")
    assert "without strong bright-dark contrast" in described
    assert "uniform in texture" in described
    assert "with smooth region boundaries" in described
    # The old positional zip produced this, which is not a sentence.
    assert "both bright and dark regions no" not in described


def test_observations_handle_the_yes_branch_and_the_none_boundary() -> None:
    described = _describe_observations(["mixed", "yes", "varied", "none"])

    assert described is not None
    assert "with both bright and dark regions present" in described
    # Question 4 offers "none", which must not become "with none region boundaries".
    assert "with no clear region boundaries" in described
    assert "none region boundaries" not in described


@pytest.mark.parametrize("observations", [[], ["", "", "", ""], None])
def test_observations_absent_yield_nothing(observations) -> None:
    assert _describe_observations(observations) is None


def _context(**overrides) -> dict:
    scene = {
        "scene_id": str(SCENE_ID),
        "sensor": "SENTINEL-1D",
        "acquisition_time": "2026-06-30T00:30:57.767975+00:00",
        "polarizations": ["VV", "VH"],
        "geography": {
            "footprint": {"centroid": {"latitude": 13.7879, "longitude": 79.6864},
                          "ground_extent_km": [255, 167]},
            "incidence_angle_deg": 38.95,
            "orbit_direction": "descending",
        },
        "land_water": {"label": "mixed_or_indeterminate", "land_fraction_estimate": 0.636,
                       "water_fraction_estimate": 0.364},
        "land_cover": {"status": "out_of_domain"},
        "validated_detector_facts": [],
    }
    scene.update(overrides)
    return {"scene_context": [scene], "patches": [{"patch_id": "a"}] * 6}


def test_scene_description_leads_with_place_and_geometry() -> None:
    described = _compose_scene_description(context=_context(), scene_id=SCENE_ID, observations=[])

    assert "SENTINEL-1D scene" in described
    assert "descending pass" in described
    assert "VV/VH polarisation" in described
    # The two facts the product knew and never told anyone.
    assert "13.79°N, 79.69°E" in described
    assert "255 x 167 km" in described
    assert "39° incidence angle" in described


def test_scene_description_keeps_every_caveat_while_dropping_the_dump() -> None:
    described = _compose_scene_description(
        context=_context(), scene_id=SCENE_ID,
        observations=["bright", "no", "uniform", "smooth"],
    )

    # Honest limits survive the rewrite.
    assert "absence of evidence, not evidence of absence" in described
    assert "not a coastline map" in described
    assert "European" in described
    assert "rather than classified" in described
    assert "nothing vessel-sized is resolvable" in described
    # And the field dump does not.
    assert "land_fraction_estimate" not in described
    assert "Overall brightness:" not in described
    assert "Uncertain observations" not in described


def test_scene_description_survives_a_scene_with_no_geography() -> None:
    # The three bare GeoTIFF scenes: no footprint, no orbit, no incidence angle.
    described = _compose_scene_description(
        context=_context(geography=None, land_cover=None), scene_id=SCENE_ID, observations=[]
    )

    assert "SENTINEL-1D scene" in described
    assert "Centred" not in described
    assert "absence of evidence" in described


def test_scene_description_reports_detector_facts_when_present() -> None:
    described = _compose_scene_description(
        context=_context(validated_detector_facts=[{"label": "ship"}, {"label": "ship"}, {"label": "bridge"}]),
        scene_id=SCENE_ID, observations=[],
    )

    assert "Detector-confirmed objects: bridge (1); ship (2)." in described
    assert "absence of evidence" not in described
