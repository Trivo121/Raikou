"""Scene-level land-cover context from BigEarthNet v2.0 (reBEN) Sentinel-1 weights.

This is a *context* signal, not a detector.  It answers "what kind of place is
this" with a reported macro average precision of 0.628 on the reBEN test split,
which is useful for framing an environment answer and useless as proof that any
particular object or boundary exists.  Nothing here may be promoted into
validated detector facts.

Three things have to be exactly right or the output is confidently wrong:

* **Calibration.** The model was trained on sigma nought in decibels.  Feeding
  it raw digital numbers, or ``10*log10(DN+1)``, puts the input hundreds of dB
  away from the training distribution.  See
  :mod:`app.services.ingestion.calibration`.
* **Band order.** ``resnet50-s1-v0.2.0`` expects ``[VV, VH]``.  The v0.1.1
  weights used the reverse; the mistake is silent because both channels are
  plausible SAR backscatter.
* **Ground sample distance.** reBEN patches are 120x120 px at 10 m.  A
  Sentinel-1 GRD pixel is also 10 m, so windows are cut at native resolution
  rather than resampled -- matching both the extent and the scale the network
  was trained at.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from typing import Any

import numpy as np

from app.services.ingestion.calibration import SigmaNoughtLUT, dn_to_sigma0_db


logger = logging.getLogger(__name__)

MODEL_ID = "BIFOLD-BigEarthNetv2-0/resnet50-s1-v0.2.0"
MODEL_ARCHITECTURE = "resnet50"
BEN_IMAGE_SIZE = 120
BEN_CHANNELS = 2

# Reported by the model card on the reBEN test split.  Carried as provenance so
# a reader can see the ceiling on this signal; never treated as per-scene
# confidence.
REPORTED_METRICS = {
    "average_precision_macro": 0.628376,
    "average_precision_micro": 0.800728,
    "f1_macro": 0.576080,
    "f1_micro": 0.701954,
    "evaluated_on": "BigEarthNet v2.0 (reBEN) test split",
}

# Band statistics from the reBEN reference implementation
# (configilm/extra/BENv2_utils.py, `means`/`stds`, key "120_nearest", which is
# the default in `band_combi_to_mean_std` and matches the 120 px model input).
# The S1 statistics are identical across every interpolation key because they
# are inherited unchanged from BigEarthNet v1.  Units are dB.
BEN_S1_BAND_ORDER = ("VV", "VH")
BEN_S1_MEAN_DB = {"VV": -12.643863677978516, "VH": -19.352558135986328}
BEN_S1_STD_DB = {"VV": 5.133493900299072, "VH": 5.590505599975586}

# `NEW_LABELS = sorted(NEW_LABELS_ORIGINAL_ORDER)` in the reference
# implementation, and the classification head is trained against that
# lexicographic order.  Index 18 is "Urban fabric", not index 0.
BEN19_LABELS = (
    "Agro-forestry areas",
    "Arable land",
    "Beaches, dunes, sands",
    "Broad-leaved forest",
    "Coastal wetlands",
    "Complex cultivation patterns",
    "Coniferous forest",
    "Industrial or commercial units",
    "Inland waters",
    "Inland wetlands",
    "Land principally occupied by agriculture, with significant areas of natural vegetation",
    "Marine waters",
    "Mixed forest",
    "Moors, heathland and sclerophyllous vegetation",
    "Natural grassland and sparsely vegetated areas",
    "Pastures",
    "Permanent crops",
    "Transitional woodland, shrub",
    "Urban fabric",
)

# BEN is multi-label with a sigmoid head.  0.5 is the *training* operating
# point, tuned for per-class F1 on a balanced benchmark; it is the wrong
# threshold for "what characterises this scene".  Measured on a Sentinel-1 IW
# scene, the classes the model is genuinely sure about hold their window share
# almost unchanged as the threshold rises (Marine waters 0.312 -> 0.284 from
# p>=0.5 to p>=0.95) while diffuse uncertainty collapses (Coniferous forest
# 0.237 -> 0.034, Transitional woodland 0.185 -> 0.003).  Scene context wants
# precision over recall: a wrong class asserted in chat costs more than a
# missing one.
PRESENCE_THRESHOLD = 0.95
# A class must be confidently called across a real share of the scene, not in
# one stray window.
SCENE_PRESENCE_FRACTION = 0.05
MAX_SAMPLE_WINDOWS = 400
NODATA_DISCARD_FRACTION = 0.40

# reBEN draws all 549,488 patches from ten European countries and labels them
# from CORINE Land Cover 2018.  Both the imagery and the *label space* are
# European: there is no tropical, arid or monsoon class to assign, so on a scene
# outside that footprint the network still emits 19 confident-looking scores by
# mapping whatever it sees onto the nearest European category.  A negative
# control makes the failure mode concrete -- fed Gaussian noise, or a flat image
# sitting exactly at the training mean, this checkpoint returns eight of the
# nineteen classes at p >= 0.95.  Sigmoid scores are therefore only meaningful
# for in-distribution input, which makes the geographic check a precondition
# for reading them at all, not a footnote.
BEN_TRAINING_COUNTRIES = (
    "Austria", "Belgium", "Finland", "Ireland", "Kosovo",
    "Lithuania", "Luxembourg", "Portugal", "Serbia", "Switzerland",
)
# Coarse envelope of those countries.  Falling outside it is conclusive
# evidence of out-of-domain input; falling inside it is necessary but not
# sufficient, so the assessment inside the box is deliberately hedged.
BEN_TRAINING_BOUNDS = {"min_lon": -31.3, "max_lon": 31.6, "min_lat": 36.0, "max_lat": 70.1}

_model = None


class LandCoverUnavailable(RuntimeError):
    """The scene cannot support a calibrated two-channel land-cover estimate."""


@dataclass(frozen=True)
class LandCoverResult:
    per_class_mean_probability: dict[str, float]
    per_class_presence_fraction: dict[str, float]
    windows_scored: int
    windows_attempted: int

    def scene_classes(self) -> list[dict[str, Any]]:
        ranked = sorted(
            self.per_class_presence_fraction.items(),
            key=lambda item: (item[1], self.per_class_mean_probability[item[0]]),
            reverse=True,
        )
        return [
            {
                "label": label,
                "present_in_window_fraction": round(fraction, 4),
                "mean_probability": round(self.per_class_mean_probability[label], 4),
            }
            for label, fraction in ranked
            if fraction >= SCENE_PRESENCE_FRACTION
        ]


def get_model(checkpoint_dir: str, device: str | None = None):
    """Load the BEN classifier as a plain timm resnet50.

    The published checkpoint is a configilm ``BigEarthNetv2_0ImageClassifier``,
    but its tensors are exactly a timm resnet50 under a
    ``model.vision_encoder.`` prefix.  Rebuilding it with timm directly avoids
    installing ``configilm``, which pins ``timm<1.0`` and would break the
    ``open-clip`` version SARCLIP runs on.
    """
    global _model
    if _model is not None:
        return _model

    import timm
    import torch
    from safetensors.torch import load_file

    weights_path = os.path.join(checkpoint_dir, "model.safetensors")
    if not os.path.exists(weights_path):
        raise LandCoverUnavailable(f"BEN weights not found at {weights_path}")

    model = timm.create_model(
        MODEL_ARCHITECTURE,
        pretrained=False,
        in_chans=BEN_CHANNELS,
        num_classes=len(BEN19_LABELS),
    )
    prefix = "model.vision_encoder."
    state = {
        key[len(prefix):]: value
        for key, value in load_file(weights_path).items()
        if key.startswith(prefix)
    }
    missing, unexpected = model.load_state_dict(state, strict=False)
    if missing or unexpected:
        # A silent partial load would leave randomly initialised layers behind
        # and produce plausible-looking nonsense.
        raise LandCoverUnavailable(
            f"BEN checkpoint did not match the resnet50 graph: "
            f"{len(missing)} missing, {len(unexpected)} unexpected tensors"
        )

    resolved = device or ("cuda" if torch.cuda.is_available() else "cpu")
    model.eval().to(resolved)
    _model = model
    logger.info("Loaded BEN land-cover model %s on %s", MODEL_ID, resolved)
    return _model


def normalize_sigma0_db(vv_db: np.ndarray, vh_db: np.ndarray) -> np.ndarray:
    """Stack and standardise calibrated dB windows into model input order."""
    stacked = np.stack(
        [
            (vv_db - BEN_S1_MEAN_DB["VV"]) / BEN_S1_STD_DB["VV"],
            (vh_db - BEN_S1_MEAN_DB["VH"]) / BEN_S1_STD_DB["VH"],
        ],
        axis=0,
    )
    return stacked.astype(np.float32)


def classify_scene(
    dataset,
    calibration: dict[str, SigmaNoughtLUT],
    *,
    checkpoint_dir: str,
    device: str | None = None,
    max_windows: int = MAX_SAMPLE_WINDOWS,
    batch_size: int = 32,
) -> LandCoverResult:
    """Score a uniform grid of native-resolution windows across the scene.

    ``dataset`` is an open rasterio dataset whose band 1 is VV and band 2 is VH,
    matching the VRT built at ingestion.
    """
    import torch
    from rasterio.windows import Window

    if dataset.count < 2:
        raise LandCoverUnavailable(
            "BEN Sentinel-1 weights need both VV and VH; this scene has "
            f"{dataset.count} band(s)"
        )
    missing_pol = [pol for pol in BEN_S1_BAND_ORDER if pol not in calibration]
    if missing_pol:
        raise LandCoverUnavailable(
            "no sigmaNought calibration LUT for " + ", ".join(missing_pol)
        )

    origins = _grid_origins(dataset.width, dataset.height, max_windows)
    if not origins:
        raise LandCoverUnavailable("scene is smaller than one 120 px window")

    model = get_model(checkpoint_dir, device)
    model_device = next(model.parameters()).device

    probabilities: list[np.ndarray] = []
    batch: list[np.ndarray] = []

    def flush() -> None:
        if not batch:
            return
        tensor = torch.from_numpy(np.stack(batch)).to(model_device)
        with torch.no_grad():
            logits = model(tensor)
            probabilities.append(torch.sigmoid(logits).cpu().numpy())
        batch.clear()

    for row_off, col_off in origins:
        window = Window(col_off, row_off, BEN_IMAGE_SIZE, BEN_IMAGE_SIZE)
        raw = dataset.read(indexes=[1, 2], window=window)
        if raw.shape[1:] != (BEN_IMAGE_SIZE, BEN_IMAGE_SIZE):
            continue
        if np.count_nonzero(raw == 0) / raw.size > NODATA_DISCARD_FRACTION:
            continue

        vv_db = dn_to_sigma0_db(
            raw[0], calibration["VV"].window(row_off, col_off, BEN_IMAGE_SIZE, BEN_IMAGE_SIZE)
        )
        vh_db = dn_to_sigma0_db(
            raw[1], calibration["VH"].window(row_off, col_off, BEN_IMAGE_SIZE, BEN_IMAGE_SIZE)
        )
        batch.append(normalize_sigma0_db(vv_db, vh_db))
        if len(batch) >= batch_size:
            flush()
    flush()

    if not probabilities:
        raise LandCoverUnavailable("every sampled window was discarded as no-data")

    scores = np.concatenate(probabilities, axis=0)
    return LandCoverResult(
        per_class_mean_probability={
            label: float(scores[:, index].mean()) for index, label in enumerate(BEN19_LABELS)
        },
        per_class_presence_fraction={
            label: float((scores[:, index] >= PRESENCE_THRESHOLD).mean())
            for index, label in enumerate(BEN19_LABELS)
        },
        windows_scored=int(scores.shape[0]),
        windows_attempted=len(origins),
    )


def _grid_origins(width: int, height: int, max_windows: int) -> list[tuple[int, int]]:
    """Uniformly spaced window origins covering the scene, capped in count."""
    if width < BEN_IMAGE_SIZE or height < BEN_IMAGE_SIZE:
        return []
    aspect = max(width / height, 1e-9)
    columns = max(1, int(round(np.sqrt(max_windows * aspect))))
    rows = max(1, int(round(max_windows / columns)))
    max_row = height - BEN_IMAGE_SIZE
    max_col = width - BEN_IMAGE_SIZE
    row_positions = np.unique(np.linspace(0, max_row, rows).astype(int))
    col_positions = np.unique(np.linspace(0, max_col, columns).astype(int))
    return [(int(r), int(c)) for r in row_positions for c in col_positions]


def assess_domain(longitude: float | None, latitude: float | None) -> dict[str, Any]:
    """Decide whether a scene centroid falls inside reBEN's training footprint.

    This gates whether the scores may be read as land cover at all.  Outside the
    footprint the answer is not "less accurate", it is "answering a different
    question": CORINE has no class for a tropical delta or a monsoon paddy
    landscape, so the network necessarily returns the closest European label.
    """
    bounds = dict(BEN_TRAINING_BOUNDS)
    if longitude is None or latitude is None:
        return {
            "assessment": "unknown",
            "within_training_bounds": None,
            "reason": "scene has no georeference, so the training footprint cannot be checked",
            "training_region": f"{len(BEN_TRAINING_COUNTRIES)} European countries",
            "training_countries": list(BEN_TRAINING_COUNTRIES),
            "label_space": "CORINE Land Cover 2018 (European)",
            "training_bounds": bounds,
        }

    inside = (
        bounds["min_lon"] <= longitude <= bounds["max_lon"]
        and bounds["min_lat"] <= latitude <= bounds["max_lat"]
    )
    return {
        "assessment": "plausibly_in_domain" if inside else "out_of_domain",
        "within_training_bounds": inside,
        "scene_centroid": {"longitude": round(longitude, 6), "latitude": round(latitude, 6)},
        "reason": (
            "scene centroid falls inside the bounding envelope of the reBEN training "
            "countries; this is necessary but not sufficient for in-domain input"
            if inside
            else "scene centroid lies outside the reBEN training footprint, so the "
            "CORINE label space has no correct class to assign"
        ),
        "training_region": f"{len(BEN_TRAINING_COUNTRIES)} European countries",
        "training_countries": list(BEN_TRAINING_COUNTRIES),
        "label_space": "CORINE Land Cover 2018 (European)",
        "training_bounds": bounds,
    }


def build_land_cover_block(
    result: LandCoverResult | None,
    *,
    domain: dict[str, Any] | None = None,
    unavailable_reason: str | None = None,
) -> dict[str, Any]:
    """Shape the scene-record ``land_cover`` block, provenance included."""
    provenance = {
        "model_id": MODEL_ID,
        "architecture": MODEL_ARCHITECTURE,
        "training_dataset": "BigEarthNet v2.0 (reBEN), Sentinel-1 VV/VH",
        "input": "sigma nought dB, 120x120 px at native 10 m GSD, band order [VV, VH]",
        "reported_metrics": REPORTED_METRICS,
        "license": "MIT",
    }
    domain = domain or assess_domain(None, None)
    if result is None:
        return {
            "status": "unavailable",
            "reason": unavailable_reason or "land-cover estimate could not be produced",
            "domain": domain,
            "provenance": provenance,
            "is_calibrated_confidence": False,
            "is_detector_evidence": False,
            "review_required": True,
        }

    # Fail closed.  A Sentinel-1 GRD measurement band carries no CRS, so an
    # unverifiable location is the common case rather than the exotic one, and
    # defaulting it to "available" would publish European land cover for an
    # arbitrary point on Earth.  Only a positive in-domain finding unlocks the
    # classes for chat.
    assessment = domain.get("assessment")
    status = {
        "plausibly_in_domain": "available",
        "out_of_domain": "out_of_domain",
    }.get(str(assessment), "domain_unverified")
    block = {
        # Scores outside the usable case are retained rather than dropped so a
        # reviewer can see what the model said, but the status keeps chat from
        # quoting them.
        "status": status,
        "method": "ben_v2_multilabel_scene_aggregate",
        "classes": result.scene_classes(),
        "presence_threshold": PRESENCE_THRESHOLD,
        "scene_presence_fraction_threshold": SCENE_PRESENCE_FRACTION,
        "windows_scored": result.windows_scored,
        "windows_attempted": result.windows_attempted,
        "domain": domain,
        "provenance": provenance,
        # Multi-label scene context with a 0.628 macro AP ceiling. It frames an
        # environment answer; it never establishes an object or a boundary.
        "is_calibrated_confidence": False,
        "is_detector_evidence": False,
        "review_required": True,
    }
    if status != "available":
        block["usable_as_land_cover"] = False
    return block
