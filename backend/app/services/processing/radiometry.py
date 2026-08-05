"""How a scene's pixel values encode backscatter.

A SAFE product ships raw detected amplitude. It only becomes sigma0 once the
calibration and noise LUTs shipped alongside it are applied, which is what
``dn_to_sigma0_db`` does and why those annotations are not optional.

A Sentinel Hub subset arrives already calibrated and orthorectified: the same
physical quantity, in linear power, with the derivation done upstream. Handing
that to the LUT path would be wrong twice over -- there are no LUTs to apply,
and the values are not amplitudes.

Every consumer downstream wants sigma0 in dB, so this module is the single
place that knows which of the two forms a scene arrived in. Keeping the
distinction here rather than in each consumer is deliberate: the previous
failure mode was three separate call sites silently returning ``None`` when the
LUTs were absent, which produced a scene that reached ``ready`` with no
scattering block, no mechanism map and no land cover, and no error anywhere.
"""

from __future__ import annotations

import math

import numpy as np


# Detected amplitude plus the SAFE calibration/noise LUTs. The historical path.
DN_WITH_LUTS = "dn"
# Provider-calibrated sigma0 in linear power, already orthorectified.
SIGMA0_LINEAR = "sigma0_linear"

SUPPORTED_RADIOMETRY = (DN_WITH_LUTS, SIGMA0_LINEAR)

# Matches SIGMA0_FLOOR_DB in the calibration module, so a window is clamped the
# same way whichever route its values took.
DEFAULT_FLOOR_DB = -50.0


def normalize_radiometry(value: str | None) -> str:
    """Fall back to the SAFE path for anything unrecognised or unset.

    Scenes recorded before this distinction existed carry no marker, and every
    one of them is a SAFE product.
    """
    text = str(value or "").strip().lower()
    return text if text in SUPPORTED_RADIOMETRY else DN_WITH_LUTS


def uses_luts(radiometry: str | None) -> bool:
    return normalize_radiometry(radiometry) == DN_WITH_LUTS


def sigma0_db_from_linear(values: np.ndarray, floor_db: float = DEFAULT_FLOOR_DB) -> np.ndarray:
    """Convert provider sigma0 in linear power to dB, clamped at the floor.

    Zero is the no-data marker in both forms, and the clamp keeps it finite
    rather than -inf so downstream means are not poisoned by a single pixel.
    """
    floor = 10.0 ** (floor_db / 10.0)
    linear = np.asarray(values, dtype=np.float64)
    return 10.0 * np.log10(np.maximum(linear, floor))


def linear_from_dataset_values(values: np.ndarray) -> np.ndarray:
    """Provider sigma0 is already linear power; only guard against negatives."""
    return np.maximum(np.asarray(values, dtype=np.float64), 0.0)


def ground_sampling_metres(dataset, *, fallback: float = 10.0) -> float:
    """Pixel size on the ground, measured rather than assumed where possible.

    A GRD product carries no CRS at all -- it is in radar geometry -- so the
    only honest answer there is the 10 m an IW GRDH ships at, which is what
    ``fallback`` is for. An orthorectified subset does carry a transform, and a
    subset may be requested at any resolution, so assuming 10 m for one of
    those would mislabel every distance the map reports.
    """
    transform = getattr(dataset, "transform", None)
    crs = getattr(dataset, "crs", None)
    if transform is None or crs is None:
        return float(fallback)
    pixel_width = abs(float(transform.a))
    if pixel_width <= 0:
        return float(fallback)
    if getattr(crs, "is_geographic", False):
        # Degrees. Convert at the raster's own latitude, where a degree of
        # longitude is shortest; the north-south degree is ~111.32 km anywhere.
        try:
            centre_lat = float(transform.f) + (float(transform.e) * dataset.height / 2.0)
        except Exception:
            centre_lat = 0.0
        metres_per_degree = 111_320.0 * max(math.cos(math.radians(centre_lat)), 1e-6)
        return float(pixel_width * metres_per_degree)
    return float(pixel_width)
