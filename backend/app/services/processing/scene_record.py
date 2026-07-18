"""Create the durable, evidence-backed record for one ingested SAR scene.

The record deliberately separates detector evidence from language-model output.
Only a configured detector sidecar may add an object to ``objects``; this module
does not turn bright pixels or VLM text into a ship detection.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
import rasterio
from rasterio.enums import Resampling
from rasterio.windows import Window

from app.services.processing.patch_pipeline import PATCH_SIZE, preprocess_patch


logger = logging.getLogger(__name__)

SCENE_RECORD_FILENAME = "scene_record.json"
DETECTOR_RESULTS_FILENAME = "detector_results.json"
SCHEMA_VERSION = "1.0"
DEFAULT_NMS_IOU_THRESHOLD = 0.5


def build_scene_record(
    *,
    session_id: str,
    session_dir: str,
    vrt_path: str,
    scene_metadata: dict[str, Any],
    detector_results_path: str | None = None,
) -> dict[str, Any]:
    """Build and atomically persist the canonical scene record.

    A detector writes a JSON sidecar before this function is called.  This keeps
    detector implementation/model weights independent from the ingestion API,
    while ensuring every supplied detection is validated, deduplicated and tied
    to a crop and coordinate system before it can reach chat.
    """
    detector_results_path = detector_results_path or os.path.join(
        session_dir, DETECTOR_RESULTS_FILENAME
    )

    with rasterio.open(vrt_path) as dataset:
        scene = _scene_details(dataset, session_id, scene_metadata)
        land_water = _estimate_land_water_context(dataset)
        raw_detections, detector, validation_errors = _read_detector_results(
            detector_results_path,
            width=dataset.width,
            height=dataset.height,
        )
        objects, crops = _deduplicate_and_materialize_objects(
            dataset=dataset,
            raw_detections=raw_detections,
            session_dir=session_dir,
        )

    detector.update(
        {
            "raw_detection_count": len(raw_detections),
            "deduplicated_object_count": len(objects),
            "validation_error_count": len(validation_errors),
            "validation_errors": validation_errors,
            "nms": {
                "method": "class_aware_iou",
                "iou_threshold": DEFAULT_NMS_IOU_THRESHOLD,
            },
        }
    )

    record = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "sar_scene_analysis",
        "session_id": session_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scene": scene,
        "context": {"land_water": land_water},
        "detector": detector,
        "objects": objects,
        "supporting_crops": crops,
        "limitations": [
            "Object entries originate only from the configured detector output; no VLM text is used as a detection.",
            "The land/water estimate is a backscatter heuristic, not a calibrated semantic-segmentation result.",
            "A single SAR acquisition supports observations of returns and geometry, not vessel identity or activity intent.",
        ],
    }
    _write_json_atomic(os.path.join(session_dir, SCENE_RECORD_FILENAME), record)
    return record


def load_scene_record(session_dir: str) -> dict[str, Any] | None:
    path = os.path.join(session_dir, SCENE_RECORD_FILENAME)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _scene_details(
    dataset: rasterio.io.DatasetReader,
    session_id: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    crs = dataset.crs.to_string() if dataset.crs else None
    bounds = dataset.bounds
    return {
        "scene_id": session_id,
        "name": metadata.get("scene_name", "Unknown"),
        "raster": {
            "width_px": dataset.width,
            "height_px": dataset.height,
            "band_count": dataset.count,
            "dtypes": list(dataset.dtypes),
            "nodata": dataset.nodata,
        },
        "metadata": {
            "sensor": metadata.get("sensor", "Unknown"),
            "acquisition_date": metadata.get("acquisition_date"),
            "polarization": metadata.get("polarization", []),
            "incidence_angle": metadata.get("incidence_angle"),
        },
        "coordinate_reference": {
            "pixel_convention": "x is column; y is row; origin is the upper-left pixel corner",
            "crs": crs,
            "transform": list(dataset.transform)[:6],
            "bounds": {
                "left": bounds.left,
                "bottom": bounds.bottom,
                "right": bounds.right,
                "top": bounds.top,
            },
        },
    }


def _estimate_land_water_context(
    dataset: rasterio.io.DatasetReader,
    sample_size: int = 512,
) -> dict[str, Any]:
    """Return a deliberately conservative low-backscatter water estimate.

    Water often has lower SAR backscatter than surrounding land, but wind,
    incidence angle, urban shadow and sensor calibration make that insufficient
    for a definitive classifier.  The method and limitation travel with the
    record so callers cannot mistake this for a detector confidence.
    """
    out_height = min(sample_size, dataset.height)
    out_width = min(sample_size, dataset.width)
    if out_height < 2 or out_width < 2:
        return _indeterminate_land_water("scene is too small to sample")

    try:
        sample = dataset.read(
            out_shape=(dataset.count, out_height, out_width),
            masked=True,
            resampling=Resampling.average,
        )
    except Exception as exc:
        logger.warning("Could not estimate land/water context: %s", exc)
        return _indeterminate_land_water("overview sampling failed")

    values = np.ma.filled(sample.astype(np.float64), np.nan)
    # The existing preprocessing treats input values as non-negative power-like
    # values.  Match that convention here, while retaining a clear heuristic
    # label rather than claiming calibrated radar backscatter.
    power = np.nanmean(np.abs(values), axis=0)
    db = 10.0 * np.log10(np.maximum(power, 1e-12))
    valid = db[np.isfinite(db)]
    if valid.size < 64:
        return _indeterminate_land_water("insufficient valid pixels")

    p02, p98 = np.percentile(valid, [2, 98])
    if not np.isfinite(p02) or not np.isfinite(p98) or p98 - p02 < 0.25:
        return _indeterminate_land_water("backscatter contrast is too low")

    clipped = np.clip(valid, p02, p98)
    threshold = _otsu_threshold(clipped)
    water_fraction = float(np.mean(clipped <= threshold))
    separation = min(1.0, max(0.0, (p98 - p02) / 12.0))

    if water_fraction >= 0.7:
        label = "likely_water_dominant"
    elif water_fraction <= 0.3:
        label = "likely_land_dominant"
    else:
        label = "mixed_or_indeterminate"

    return {
        "label": label,
        "method": "low_backscatter_otsu_heuristic",
        "water_fraction_estimate": round(water_fraction, 4),
        "land_fraction_estimate": round(1.0 - water_fraction, 4),
        "backscatter_threshold_db": round(float(threshold), 4),
        "separability_score": round(separation, 4),
        "is_calibrated_confidence": False,
        "review_required": True,
    }


def _indeterminate_land_water(reason: str) -> dict[str, Any]:
    return {
        "label": "indeterminate",
        "method": "low_backscatter_otsu_heuristic",
        "reason": reason,
        "is_calibrated_confidence": False,
        "review_required": True,
    }


def _otsu_threshold(values: np.ndarray, bins: int = 256) -> float:
    histogram, edges = np.histogram(values, bins=bins)
    total = histogram.sum()
    if total == 0:
        return float(np.median(values))
    centers = (edges[:-1] + edges[1:]) / 2.0
    weight_background = np.cumsum(histogram)
    weight_foreground = total - weight_background
    sum_background = np.cumsum(histogram * centers)
    sum_foreground = sum_background[-1] - sum_background

    valid = (weight_background > 0) & (weight_foreground > 0)
    variance = np.zeros_like(centers)
    mean_background = np.divide(
        sum_background, weight_background, out=np.zeros_like(centers), where=weight_background > 0
    )
    mean_foreground = np.divide(
        sum_foreground, weight_foreground, out=np.zeros_like(centers), where=weight_foreground > 0
    )
    variance[valid] = (
        weight_background[valid]
        * weight_foreground[valid]
        * (mean_background[valid] - mean_foreground[valid]) ** 2
    )
    return float(centers[int(np.argmax(variance))])


def _read_detector_results(
    path: str,
    *,
    width: int,
    height: int,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """Validate a detector sidecar without assuming a particular ML framework.

    Contract::

        {
          "detector": {"name": "sar-ship-detector", "version": "..."},
          "detections": [
            {"label": "ship", "confidence": 0.91,
             "bbox_xyxy": [x_min, y_min, x_max, y_max]}
          ]
        }
    """
    if not os.path.exists(path):
        return [], {
            "status": "awaiting_detector_output",
            "results_path": DETECTOR_RESULTS_FILENAME,
            "message": "No detector results were supplied; object list is intentionally empty.",
        }, []

    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        return [], {
            "status": "invalid_detector_output",
            "results_path": os.path.basename(path),
            "message": "Detector output could not be parsed; object list is intentionally empty.",
        }, [str(exc)]

    if isinstance(payload, list):
        raw_items = payload
        supplied_detector = {}
    elif isinstance(payload, dict):
        raw_items = payload.get("detections", [])
        supplied_detector = payload.get("detector", {})
    else:
        raw_items = []
        supplied_detector = {}

    if not isinstance(raw_items, list):
        raw_items = []

    valid: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, item in enumerate(raw_items):
        try:
            valid.append(_validate_detection(item, index=index, width=width, height=height))
        except ValueError as exc:
            errors.append(f"detection[{index}]: {exc}")

    detector = {
        "status": "completed" if valid else "completed_no_valid_detections",
        "results_path": os.path.basename(path),
        "name": supplied_detector.get("name", "external_sar_detector"),
        "version": supplied_detector.get("version"),
        "confidence_semantics": supplied_detector.get(
            "confidence_semantics", "Detector-supplied probability; calibrate per model before operational use."
        ),
    }
    return valid, detector, errors


def _validate_detection(
    item: Any,
    *,
    index: int,
    width: int,
    height: int,
) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError("must be an object")

    label = str(item.get("label") or item.get("class") or "unknown").strip().lower()
    if not label:
        raise ValueError("label must not be blank")

    try:
        confidence = float(item["confidence"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("confidence must be a number in [0, 1]") from exc
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence must be in [0, 1]")

    bbox = item.get("bbox_xyxy")
    if bbox is None:
        bbox = [item.get(key) for key in ("x_min", "y_min", "x_max", "y_max")]
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        raise ValueError("bbox_xyxy must contain [x_min, y_min, x_max, y_max]")
    try:
        x_min, y_min, x_max, y_max = (float(value) for value in bbox)
    except (TypeError, ValueError) as exc:
        raise ValueError("bounding-box values must be numeric") from exc
    if not all(math.isfinite(value) for value in (x_min, y_min, x_max, y_max)):
        raise ValueError("bounding-box values must be finite")
    if x_max <= x_min or y_max <= y_min:
        raise ValueError("bounding box must have positive area")

    original = [x_min, y_min, x_max, y_max]
    x_min = min(max(x_min, 0.0), float(width))
    y_min = min(max(y_min, 0.0), float(height))
    x_max = min(max(x_max, 0.0), float(width))
    y_max = min(max(y_max, 0.0), float(height))
    if x_max <= x_min or y_max <= y_min:
        raise ValueError("bounding box lies outside the scene")

    return {
        "source_id": str(item.get("id") or f"raw-{index + 1}"),
        "label": label,
        "confidence": confidence,
        "bbox_xyxy": [x_min, y_min, x_max, y_max],
        "was_clipped_to_scene": original != [x_min, y_min, x_max, y_max],
    }


def _deduplicate_and_materialize_objects(
    *,
    dataset: rasterio.io.DatasetReader,
    raw_detections: list[dict[str, Any]],
    session_dir: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    retained = _class_aware_nms(raw_detections, DEFAULT_NMS_IOU_THRESHOLD)
    crop_dir = os.path.join(session_dir, "crops")
    objects: list[dict[str, Any]] = []
    crops: list[dict[str, Any]] = []

    for number, detection in enumerate(retained, start=1):
        object_id = f"object-{number:04d}"
        bbox = detection["bbox_xyxy"]
        centroid_x = (bbox[0] + bbox[2]) / 2.0
        centroid_y = (bbox[1] + bbox[3]) / 2.0
        crop = _write_supporting_crop(
            dataset=dataset,
            bbox_xyxy=bbox,
            crop_path=os.path.join(crop_dir, f"{object_id}.jpg"),
        )
        if crop:
            crop["object_id"] = object_id
            crops.append(crop)

        objects.append(
            {
                "id": object_id,
                "label": detection["label"],
                "confidence": round(detection["confidence"], 6),
                "bounding_box_px": _bbox_payload(bbox),
                "centroid_px": {"x": round(centroid_x, 3), "y": round(centroid_y, 3)},
                "location": _georeference_point(dataset, centroid_x, centroid_y),
                "evidence": {
                    "detector_source_ids": detection["source_ids"],
                    "supporting_crop": crop["path"] if crop else None,
                },
                "was_clipped_to_scene": detection["was_clipped_to_scene"],
            }
        )

    return objects, crops


def _class_aware_nms(
    detections: list[dict[str, Any]], threshold: float) -> list[dict[str, Any]]:
    retained: list[dict[str, Any]] = []
    for label in sorted({detection["label"] for detection in detections}):
        candidates = sorted(
            (detection for detection in detections if detection["label"] == label),
            key=lambda detection: detection["confidence"],
            reverse=True,
        )
        while candidates:
            selected = candidates.pop(0)
            source_ids = [selected["source_id"]]
            survivors = []
            for candidate in candidates:
                if _iou(selected["bbox_xyxy"], candidate["bbox_xyxy"]) > threshold:
                    source_ids.append(candidate["source_id"])
                else:
                    survivors.append(candidate)
            retained.append({**selected, "source_ids": source_ids})
            candidates = survivors
    return sorted(retained, key=lambda detection: detection["confidence"], reverse=True)


def _iou(left: list[float], right: list[float]) -> float:
    inter_left = max(left[0], right[0])
    inter_top = max(left[1], right[1])
    inter_right = min(left[2], right[2])
    inter_bottom = min(left[3], right[3])
    inter_width = max(0.0, inter_right - inter_left)
    inter_height = max(0.0, inter_bottom - inter_top)
    intersection = inter_width * inter_height
    if intersection == 0.0:
        return 0.0
    left_area = (left[2] - left[0]) * (left[3] - left[1])
    right_area = (right[2] - right[0]) * (right[3] - right[1])
    return intersection / (left_area + right_area - intersection)


def _bbox_payload(bbox: list[float]) -> dict[str, float]:
    return {
        "x_min": round(bbox[0], 3),
        "y_min": round(bbox[1], 3),
        "x_max": round(bbox[2], 3),
        "y_max": round(bbox[3], 3),
    }


def _georeference_point(
    dataset: rasterio.io.DatasetReader,
    x: float,
    y: float,
) -> dict[str, Any]:
    if not dataset.crs:
        return {"status": "unavailable", "reason": "source raster has no CRS"}

    map_x, map_y = dataset.transform * (x, y)
    result: dict[str, Any] = {
        "status": "available",
        "native": {
            "crs": dataset.crs.to_string(),
            "x": round(map_x, 8),
            "y": round(map_y, 8),
        },
    }
    try:
        from rasterio.warp import transform

        longitude, latitude = transform(dataset.crs, "EPSG:4326", [map_x], [map_y])
        result["wgs84"] = {
            "longitude": round(longitude[0], 8),
            "latitude": round(latitude[0], 8),
        }
    except Exception as exc:
        result["wgs84"] = {"status": "unavailable", "reason": str(exc)}
    return result


def _write_supporting_crop(
    *,
    dataset: rasterio.io.DatasetReader,
    bbox_xyxy: list[float],
    crop_path: str,
) -> dict[str, Any] | None:
    center_x = (bbox_xyxy[0] + bbox_xyxy[2]) / 2.0
    center_y = (bbox_xyxy[1] + bbox_xyxy[3]) / 2.0
    max_col = max(dataset.width - PATCH_SIZE, 0)
    max_row = max(dataset.height - PATCH_SIZE, 0)
    col = int(min(max(round(center_x - PATCH_SIZE / 2), 0), max_col))
    row = int(min(max(round(center_y - PATCH_SIZE / 2), 0), max_row))

    if dataset.width < PATCH_SIZE or dataset.height < PATCH_SIZE:
        logger.warning("Skipping supporting crop because scene is smaller than %s px", PATCH_SIZE)
        return None

    try:
        raw_patch = dataset.read(window=Window(col, row, PATCH_SIZE, PATCH_SIZE))
        processed = preprocess_patch(raw_patch)
        if processed is None:
            return None
        os.makedirs(os.path.dirname(crop_path), exist_ok=True)
        Image.fromarray(processed).save(crop_path, format="JPEG")
        return {
            "path": str(Path("crops") / os.path.basename(crop_path)).replace("\\", "/"),
            "pixel_window": {
                "x_min": col,
                "y_min": row,
                "x_max": col + PATCH_SIZE,
                "y_max": row + PATCH_SIZE,
            },
        }
    except Exception as exc:
        logger.warning("Failed to write supporting crop %s: %s", crop_path, exc)
        return None


def _write_json_atomic(path: str, payload: dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    temporary_path = f"{path}.tmp"
    with open(temporary_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, allow_nan=False)
    os.replace(temporary_path, path)
