"""The optical scene record: what Raikou may say about an optical run.

Built in code from measured outputs only (the LULC stage's summary, the
acquisition row and the model card), so every statement traces to a field.
Wording rules, from the approved plan:
* model numbers are *agreement with WorldCover-2021-derived labels on held-out
  Indian tiles*, never "accuracy";
* the WorldCover comparison is an agreement matrix, never "change detection".
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

RECORD_TYPE = "optical_scene_analysis"
SCHEMA_VERSION = 1

# Coarse training-domain box (the 100 Phase 0 tiles sit inside it). Outside,
# the map is still produced but flagged.
INDIA_BOX = (68.0, 6.0, 97.5, 37.5)


def _round(value: Any, digits: int = 4) -> float | None:
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _centroid(corners: list[list[float]]) -> list[float] | None:
    if not corners:
        return None
    return [round(sum(c[0] for c in corners) / len(corners), 6), round(sum(c[1] for c in corners) / len(corners), 6)]


def _in_india(centroid: list[float] | None) -> bool | None:
    if centroid is None:
        return None
    w, s, e, n = INDIA_BOX
    return w <= centroid[0] <= e and s <= centroid[1] <= n


def build_optical_record(*, scene: dict[str, Any], acquisition: dict[str, Any] | None, summary: dict[str, Any]) -> dict[str, Any]:
    model = summary.get("model") or {}
    run = summary.get("run") or {}
    grid = summary.get("grid") or {}
    corners = summary.get("corners_lonlat") or []
    centroid = _centroid(corners)
    timesteps = summary.get("timesteps") or []
    used = summary.get("timesteps_used") or []
    mode = "seasonal_stack" if len(timesteps) > 1 else "single_date"
    # The release was benchmarked on one date (T1) and on three (T3). Quote T3
    # only when the model really saw three; anything less gets the T1 figures.
    eval_key = "seasonal_stack" if len(used) >= 3 else "single_date"
    evaluation = model.get(eval_key) or {}

    classes = []
    for row in summary.get("stats") or []:
        classes.append(
            {
                "class_id": int(row.get("class_id", 0)),
                "class_name": row.get("class_name"),
                "area_sqkm": _round(row.get("area_sqkm")),
                "area_hectares": _round(row.get("area_hectares"), 2),
                "share_pct": _round(row.get("share_of_mapped_%"), 2),
                "mean_score_pct": _round(row.get("mean_score_%"), 1),
                "worldcover_2021_area_sqkm": _round(row.get("worldcover_2021_area_sqkm")),
                "same_class_in_worldcover_2021_pct": _round(row.get("same_class_in_worldcover_2021_%"), 1),
            }
        )
    mapped = [c for c in classes if (c["share_pct"] or 0) > 0]
    dominant = max(mapped, key=lambda c: c["share_pct"] or 0)["class_name"] if mapped else None
    valid_fraction = _round(run.get("valid_fraction"))
    in_india = _in_india(centroid)

    limitations = [
        "The map shows land cover on the image date(s), from a model trained on 2020-21 Indian Sentinel-2 imagery "
        "labelled with ESA WorldCover 2021 and remapped to five classes.",
        "Scores and benchmark figures are agreement with WorldCover-2021-derived labels on held-out Indian tiles, "
        "not field-verified accuracy.",
        "Differences from WorldCover 2021 mix real change with errors in both maps; this run does not detect change.",
        "Objects (buildings, vehicles, ships) are not detected or counted; regions smaller than about 400 m2 are removed by the sieve.",
    ]
    if mode == "single_date":
        limitations.append("A single date cannot see the crop calendar, so cropland versus bare or fallow land is less reliable than with a seasonal stack.")
    if timesteps and len(used) < len(timesteps):
        limitations.append(f"Only {len(used)} of the {len(timesteps)} dates were clear enough to use.")
    if len(used) == 2:
        limitations.append("The model was benchmarked on one date and on three; with two dates the single-date figures are quoted.")
    if valid_fraction is not None and valid_fraction < 0.98:
        limitations.append(f"{100 * (1 - valid_fraction):.0f}% of the area had no clear observation and is left unmapped.")
    if in_india is False:
        limitations.append("This area lies outside India, the model's training domain; treat the map as unvalidated here.")
    # On the release's held-out tiles most water disagreement was bare land mapped
    # as water: salt flats in the Little Rann of Kutch, flooded on the image date.
    if any("water" in str(c["class_name"] or "").lower() and (c["share_pct"] or 0) > 0 for c in classes):
        limitations.append(
            "Land that floods seasonally, such as salt flats, floodplains and paddies, can be mapped as water when "
            "the image date shows it flooded, while WorldCover 2021 may call it bare land or cropland, so agreement "
            "on water varies with the date."
        )

    agreement = summary.get("agreement") or {}
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": RECORD_TYPE,
        "session_id": str(scene.get("id")),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scene": {
            "scene_id": str(scene.get("id")),
            "name": scene.get("name"),
            "grid": grid,
            "corners_lonlat": corners,
            "centroid_lonlat": centroid,
            "area_sqkm": _round((grid.get("width", 0) * grid.get("height", 0) * (grid.get("res", 10.0) ** 2)) / 1e6, 3),
            "in_training_domain": in_india,
        },
        "imagery": {
            "source": "Copernicus Sentinel-2 L2A, CDSE Process API, harmonized DN, nearest-neighbour onto a native-UTM 10 m grid",
            "mode": mode,
            "timesteps": timesteps,
            "timesteps_used": used,
            "processing_units": _round((acquisition or {}).get("processing_units"), 3),
        },
        "lulc": {
            "model": {k: model.get(k) for k in ("name", "version", "base_model", "licence")},
            "classes": classes,
            "dominant_class": dominant,
            "mapped_fraction": valid_fraction,
            "score": {
                "temperature": model.get("temperature"),
                "meaning": model.get("calibration_meaning"),
                "basis": "agreement with WorldCover-2021-derived labels on held-out Indian tiles",
            },
            "evaluation": {
                "applies_to": eval_key,
                "agreement_overall": evaluation.get("agreement_overall"),
                "mean_iou": evaluation.get("mean_iou"),
                "macro_f1": evaluation.get("macro_f1"),
                "per_class_iou": evaluation.get("per_class_iou"),
                "reference": model.get("reference"),
                "wording": "agreement with WorldCover-2021-derived labels on held-out Indian tiles, not field-verified accuracy",
            },
            "artifacts": summary.get("artifacts") or {},
        },
        "reference": {
            "dataset": "ESA WorldCover 2021 v200, remapped to the same five classes",
            "agreement_matrix_km2": agreement.get("matrix_km2"),
            "rows": "WorldCover 2021",
            "columns": "this map",
            "class_order": agreement.get("classes"),
            "is_change_detection": False,
        },
        "provenance": {
            "attribution": model.get("attribution") or {},
            "licence": model.get("licence"),
        },
        "limitations": limitations,
    }


def summary_sentence(record: dict[str, Any]) -> str:
    """One deterministic sentence for the evidence row; every number from the record."""
    lulc = record.get("lulc") or {}
    imagery = record.get("imagery") or {}
    # Name only the dates the model used; a cloudy date that was dropped is in the limitations.
    steps = imagery.get("timesteps") or []
    used = imagery.get("timesteps_used") or list(range(len(steps)))
    picked = [steps[i] for i in used if isinstance(i, int) and 0 <= i < len(steps)]
    dates = ", ".join(str(t.get("sensing_start", ""))[:10] for t in picked if t.get("sensing_start"))
    shares = sorted(
        (c for c in lulc.get("classes") or [] if (c.get("share_pct") or 0) > 0),
        key=lambda c: c.get("share_pct") or 0,
        reverse=True,
    )
    parts = ", ".join(f"{c['class_name']} {c['share_pct']:.1f}%" for c in shares)
    ev = lulc.get("evaluation") or {}
    agree = ev.get("agreement_overall")
    mode = "three-season stack" if len(picked) >= 3 else "two dates" if len(picked) == 2 else "single date"
    tail = f"; the model agrees with WorldCover-derived labels on {100 * agree:.0f}% of pixels on held-out Indian tiles" if isinstance(agree, (int, float)) else ""
    return f"Sentinel-2 land cover ({mode}, {dates}): {parts}{tail}."
