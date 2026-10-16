"""The optical scene record: built from measured outputs only, worded as agreement."""

from __future__ import annotations

from app.services.optical.record import RECORD_TYPE, build_optical_record, summary_sentence

SCENE = {"id": "5d0c9a8e-0d55-4b0e-9a57-0c0f6d1b2a10", "name": "Land cover 2026-04-12"}
CORNERS_VIZAG = [[83.19, 17.78], [83.30, 17.78], [83.30, 17.67], [83.19, 17.67]]
CORNERS_EUROPE = [[10.00, 50.10], [10.14, 50.10], [10.14, 50.01], [10.00, 50.01]]
ONE_DATE = ("2026-04-12T05:06:49.024000+00:00",)
THREE_DATES = ("2025-11-02T05:06:51+00:00", "2026-02-05T05:08:01+00:00", "2026-04-12T05:06:49.024000+00:00")
MODEL = {
    "name": "Raikou OlmoEarth India LULC (5 classes)",
    "version": "0.1.0",
    "base_model": "allenai/OlmoEarth-v1_2-Base",
    "licence": "OlmoEarth Artifact License (see LICENSE_OlmoEarth_Artifact.txt)",
    "single_date": {"agreement_overall": 0.8712, "mean_iou": 0.61, "macro_f1": 0.74, "per_class_iou": {"Built-up": 0.55}},
    "seasonal_stack": {"agreement_overall": 0.8934, "mean_iou": 0.65, "macro_f1": 0.77, "per_class_iou": {"Built-up": 0.58}},
    "reference": "label_raw = WorldCover 2021 remapped, unfiltered (agreement, not ground truth)",
    "temperature": 1.31,
    "calibration_meaning": "calibrated probability that a pixel agrees with the WorldCover-2021-derived class on held-out Indian tiles",
    "attribution": {"worldcover": "ESA WorldCover 2021 v200, CC BY 4.0"},
}
# lulc_stats.csv rows as csv.DictReader returns them: every value a string, None as "".
STATS = [
    {"class_id": "1", "class_name": "Built-up", "area_sqkm": "24.7296", "area_hectares": "2472.96",
     "share_of_mapped_%": "22.4", "mean_score_%": "81.2", "worldcover_2021_area_sqkm": "19.0",
     "same_class_in_worldcover_2021_%": "77.0"},
    {"class_id": "2", "class_name": "Agriculture", "area_sqkm": "53.1", "area_hectares": "5310.0",
     "share_of_mapped_%": "48.1", "mean_score_%": "74.9", "worldcover_2021_area_sqkm": "55.2",
     "same_class_in_worldcover_2021_%": "83.4"},
    {"class_id": "3", "class_name": "Forest & natural vegetation", "area_sqkm": "23.2", "area_hectares": "2320.0",
     "share_of_mapped_%": "21.0", "mean_score_%": "79.0", "worldcover_2021_area_sqkm": "26.3",
     "same_class_in_worldcover_2021_%": "71.8"},
    {"class_id": "4", "class_name": "Barren / wasteland", "area_sqkm": "0.0", "area_hectares": "0.0",
     "share_of_mapped_%": "0.0", "mean_score_%": "", "worldcover_2021_area_sqkm": "0.4",
     "same_class_in_worldcover_2021_%": ""},
    {"class_id": "5", "class_name": "Water bodies & wetlands", "area_sqkm": "9.4", "area_hectares": "940.0",
     "share_of_mapped_%": "8.5", "mean_score_%": "95.6", "worldcover_2021_area_sqkm": "9.5",
     "same_class_in_worldcover_2021_%": "96.1"},
]


def _summary(*, dates=ONE_DATE, used=None, valid_fraction=0.995, corners=CORNERS_VIZAG) -> dict:
    return {
        "grid": {"epsg": 32644, "width": 1104, "height": 1000, "res": 10.0},
        "corners_lonlat": corners,
        "timesteps": [{"index": i, "sensing_start": d, "valid_fraction": 0.9} for i, d in enumerate(dates)],
        "timesteps_used": list(range(len(dates))) if used is None else used,
        "run": {"valid_fraction": valid_fraction, "polygons": 812},
        "stats": STATS,
        "agreement": {"classes": [row["class_name"] for row in STATS], "matrix_km2": [[1.0] * 5] * 5},
        "model": MODEL,
        "artifacts": {"bundle_zip": "artifact-1", "lulc": "artifact-2"},
    }


def _record(**kwargs) -> dict:
    return build_optical_record(scene=SCENE, acquisition={"processing_units": 16.53}, summary=_summary(**kwargs))


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def test_the_record_traces_every_number_to_its_inputs():
    record = _record()
    classes = {c["class_name"]: c for c in record["lulc"]["classes"]}

    assert record["record_type"] == RECORD_TYPE
    assert record["scene"]["scene_id"] == SCENE["id"]
    assert record["scene"]["area_sqkm"] == 110.4
    assert record["imagery"]["processing_units"] == 16.53
    assert record["lulc"]["dominant_class"] == "Agriculture"
    assert classes["Built-up"]["share_pct"] == 22.4 and classes["Built-up"]["class_id"] == 1
    # An empty CSV cell stays unknown; it never becomes 0.
    assert classes["Barren / wasteland"]["mean_score_pct"] is None
    assert classes["Barren / wasteland"]["same_class_in_worldcover_2021_pct"] is None
    assert record["lulc"]["artifacts"] == {"bundle_zip": "artifact-1", "lulc": "artifact-2"}


def test_numbers_are_worded_as_agreement_never_as_accuracy_or_change():
    for record in (_record(), _record(dates=THREE_DATES)):
        for text in _strings(record):
            assert "accuracy" not in text.replace("not field-verified accuracy", "").lower(), text
            assert "change detection" not in text.lower(), text
        assert record["reference"]["is_change_detection"] is False
        assert record["lulc"]["score"]["basis"].startswith("agreement with WorldCover-2021-derived labels")


def test_the_quoted_benchmark_matches_the_dates_the_model_saw():
    single = _record()
    stack = _record(dates=THREE_DATES)
    two_of_three = _record(dates=THREE_DATES, used=[0, 2])

    assert (single["imagery"]["mode"], single["lulc"]["evaluation"]["applies_to"]) == ("single_date", "single_date")
    assert single["lulc"]["evaluation"]["agreement_overall"] == 0.8712
    assert (stack["imagery"]["mode"], stack["lulc"]["evaluation"]["applies_to"]) == ("seasonal_stack", "seasonal_stack")
    assert stack["lulc"]["evaluation"]["agreement_overall"] == 0.8934
    # Three dates fetched, one too cloudy: only the single-date benchmark applies.
    assert two_of_three["imagery"]["mode"] == "seasonal_stack"
    assert two_of_three["lulc"]["evaluation"]["applies_to"] == "single_date"
    notes = " ".join(two_of_three["limitations"])
    assert "Only 2 of the 3 dates were clear enough to use." in notes
    assert "with two dates the single-date figures are quoted" in notes


def test_limitations_follow_the_run():
    cloudy_single = " ".join(_record(valid_fraction=0.9)["limitations"])
    clear_stack = " ".join(_record(dates=THREE_DATES)["limitations"])
    abroad = " ".join(_record(corners=CORNERS_EUROPE)["limitations"])

    assert "crop calendar" in cloudy_single
    assert "10% of the area had no clear observation" in cloudy_single
    assert "crop calendar" not in clear_stack and "no clear observation" not in clear_stack
    assert "outside India" in abroad and "outside India" not in cloudy_single


def test_seasonal_flooding_is_flagged_only_when_the_map_has_water():
    with_water = " ".join(_record()["limitations"])
    dry_stats = [dict(row, **({"share_of_mapped_%": "0.0"} if row["class_id"] == "5" else {})) for row in STATS]
    dry = build_optical_record(scene=SCENE, acquisition=None, summary={**_summary(), "stats": dry_stats})

    assert "floods seasonally" in with_water
    assert "floods seasonally" not in " ".join(dry["limitations"])


def test_the_summary_sentence_quotes_only_record_numbers():
    assert summary_sentence(_record()) == (
        "Sentinel-2 land cover (single date, 2026-04-12): Agriculture 48.1%, Built-up 22.4%, "
        "Forest & natural vegetation 21.0%, Water bodies & wetlands 8.5%; the model agrees with "
        "WorldCover-derived labels on 87% of pixels on held-out Indian tiles."
    )
    assert summary_sentence(_record(dates=THREE_DATES)).startswith(
        "Sentinel-2 land cover (three-season stack, 2025-11-02, 2026-02-05, 2026-04-12): "
    )
    assert summary_sentence(_record(dates=THREE_DATES, used=[0, 2])).startswith(
        "Sentinel-2 land cover (two dates, 2025-11-02, 2026-04-12): "
    )
