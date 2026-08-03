"""Describe a scene by scattering mechanism rather than by land-cover label.

The land-cover classifier answers a question its label space cannot support
outside Europe: CORINE has no tropical category, so the network returns the
nearest European one and "Coniferous forest" appears on the Andhra coast.  The
scattering mechanism is a different question with a region-independent answer,
because it is set by surface geometry rather than by ecology:

* **Surface scattering** -- a smooth interface reflects away from the sensor and
  depolarises very little, so VV is low and VH collapses toward the noise floor.
  Open water, wet mud, dry sand.
* **Volume scattering** -- a canopy scatters repeatedly off randomly oriented
  elements, which depolarises the return and lifts VH relative to VV.  Forest,
  plantation, tall crops.  A canopy does this in Finland and in a mangrove
  equally; the physics does not know the biome.
* **Double-bounce** -- a ground-wall corner returns almost all the incident
  power along the incoming path, so VV spikes while VH stays comparatively low.
  Buildings, ships, infrastructure.

What this cannot do is name a land use.  Paddy, mangrove and plantation are all
canopies that depolarise, and no dual-pol intensity measurement separates them.
The honest output is the mechanism and its share of the scene, not a crop type.

GRD is amplitude-detected with no phase, so the coherent decompositions
(H/A/alpha from the covariance matrix) are unavailable.  Intensity ratios are
not affected by that and are what this module uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


# Speckle is multiplicative, so a single pixel's ratio is dominated by noise.
# Averaging in linear power across the window before ratioing is the standard
# multi-look, and it is what makes the ratio stable enough to threshold.
@dataclass(frozen=True)
class WindowScattering:
    """Multi-looked dual-pol summary of one window."""

    vv_db: float
    vh_db: float
    cross_pol_ratio_db: float
    radar_vegetation_index: float
    noise_limited_fraction: float


def _to_linear(db: np.ndarray) -> np.ndarray:
    return np.power(10.0, np.asarray(db, dtype=np.float64) / 10.0)


def summarize_window(
    vv_db: np.ndarray,
    vh_db: np.ndarray,
    *,
    noise_floor_db: float = -50.0,
) -> WindowScattering:
    """Multi-look one window and derive its dual-pol descriptors.

    ``noise_limited_fraction`` is the share of pixels that denoising pushed to
    the clamp, meaning the signal never rose above the receiver.  It is carried
    rather than hidden: a window that is mostly floor has a ratio computed from
    almost nothing, and the caller should discard it instead of averaging it in.
    """
    vv_lin = _to_linear(vv_db)
    vh_lin = _to_linear(vh_db)
    floor = 10.0 ** (noise_floor_db / 10.0)

    # Count the clamp before averaging, while the pixels are still identifiable.
    noise_limited = float(np.mean(vh_lin <= floor * 1.000001))

    vv_mean = float(np.mean(vv_lin))
    vh_mean = float(np.mean(vh_lin))
    total = vv_mean + vh_mean

    return WindowScattering(
        vv_db=float(10.0 * np.log10(max(vv_mean, floor))),
        vh_db=float(10.0 * np.log10(max(vh_mean, floor))),
        cross_pol_ratio_db=float(10.0 * np.log10(max(vh_mean, floor) / max(vv_mean, floor))),
        # Dual-pol RVI. Bounded in [0, 2] by construction but in practice ~0-1:
        # near 0 for a pure surface return, rising as depolarisation increases.
        radar_vegetation_index=float(4.0 * vh_mean / total) if total > 0 else 0.0,
        noise_limited_fraction=noise_limited,
    )


@dataclass(frozen=True)
class ScatteringThresholds:
    """Decision boundaries between mechanisms, in calibrated dB and RVI.

    Deliberately data rather than constants in the classifier.  C-band textbook
    values were tried first against this instrument and put 0.1% of a coastal
    scene in the water class against an independent heuristic's 36%, because a
    monsoon-season sea is rough and never reaches the calm-water backscatter the
    textbook assumes.  Thresholds must be fitted to the product and checked
    against the scene, not asserted.
    """

    water_max_vv_db: float
    water_max_ratio_db: float
    urban_min_vv_db: float
    urban_max_ratio_db: float
    volume_min_rvi: float

    def as_dict(self) -> dict[str, float]:
        return {
            "water_max_vv_db": self.water_max_vv_db,
            "water_max_ratio_db": self.water_max_ratio_db,
            "urban_min_vv_db": self.urban_min_vv_db,
            "urban_max_ratio_db": self.urban_max_ratio_db,
            "volume_min_rvi": self.volume_min_rvi,
        }


MECHANISMS = ("smooth_surface", "double_bounce", "volume", "rough_surface")

MECHANISM_DESCRIPTIONS = {
    "smooth_surface": "open water, wet flats or dry sand",
    "double_bounce": "built-up structures returning a corner reflection",
    "volume": "vegetation canopy depolarising the return",
    "rough_surface": "bare or sparsely vegetated ground",
}


def classify_window(window: WindowScattering, thresholds: ScatteringThresholds) -> str:
    """Assign one window to a scattering mechanism.

    Order matters: the two specific mechanisms are tested before the two general
    ones, because a smooth water surface and a bare field both have low RVI and
    only the absolute level separates them.
    """
    if (
        window.vv_db <= thresholds.water_max_vv_db
        and window.cross_pol_ratio_db <= thresholds.water_max_ratio_db
    ):
        return "smooth_surface"
    if (
        window.vv_db >= thresholds.urban_min_vv_db
        and window.cross_pol_ratio_db <= thresholds.urban_max_ratio_db
    ):
        return "double_bounce"
    if window.radar_vegetation_index >= thresholds.volume_min_rvi:
        return "volume"
    return "rough_surface"


def build_scattering_block(
    windows: list[WindowScattering],
    thresholds: ScatteringThresholds,
    *,
    is_denoised: bool,
    max_noise_limited_fraction: float = 0.5,
) -> dict[str, Any] | None:
    """Aggregate window mechanisms into the scene-record ``scattering`` block.

    Returns ``None`` when nothing usable survives, so the caller can treat this
    the way it treats every other optional block.
    """
    usable = [w for w in windows if w.noise_limited_fraction <= max_noise_limited_fraction]
    if not usable:
        return None

    assigned = [classify_window(window, thresholds) for window in usable]
    counts = {name: assigned.count(name) for name in MECHANISMS}
    total = len(assigned)

    return {
        "method": "dual_pol_intensity_scattering_mechanism",
        "is_denoised": is_denoised,
        "windows_scored": total,
        "windows_discarded_noise_limited": len(windows) - total,
        "thresholds": thresholds.as_dict(),
        "mechanisms": [
            {
                "mechanism": name,
                "fraction": round(counts[name] / total, 4),
                "description": MECHANISM_DESCRIPTIONS[name],
                "median_vv_db": round(
                    float(np.median([w.vv_db for w, a in zip(usable, assigned) if a == name])), 2
                ),
                "median_cross_pol_ratio_db": round(
                    float(
                        np.median(
                            [w.cross_pol_ratio_db for w, a in zip(usable, assigned) if a == name]
                        )
                    ),
                    2,
                ),
            }
            for name in MECHANISMS
            if counts[name]
        ],
        # A mechanism is a measurement, not a classifier output, and it is not a
        # land use. Both facts have to travel with the block.
        "is_land_use_classification": False,
        "is_detector_evidence": False,
        "limitations": [
            "Scattering mechanism is measured from calibrated VV/VH intensity; it "
            "indicates surface geometry, not land use.",
            "Vegetation types cannot be separated: canopy, plantation and tall crops "
            "all scatter by volume.",
            "GRD is amplitude-detected, so coherent polarimetric decomposition is "
            "unavailable and only intensity ratios are used.",
        ],
    }
