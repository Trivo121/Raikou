"""Labels from WorldCover, cleaned by multi-season spectral agreement.

label_raw   : WorldCover remapped to 5 classes (IGNORE for nodata/snow). Used to
              score the test split and to calibrate, because filtering a test set
              to the pixels that already agree with a rule inflates its numbers.
label_clean : label_raw where the 2020-21 spectral profile agrees with the class,
              minus a 1 px band at class boundaries. Used for training and for
              picking the best checkpoint.
"""

from __future__ import annotations

import warnings

import numpy as np

from . import config as C

_EPS = 1e-6


def spectral_stats(dn: np.ndarray, valid: np.ndarray) -> dict[str, np.ndarray]:
    """Per-pixel multi-season index statistics over the valid timesteps.

    dn: (T, 12, H, W) harmonized DN; valid: (T, H, W) bool.
    """
    b = C.BAND_INDEX
    refl = lambda name: dn[:, b[name]].astype(np.float32) / 10000.0  # noqa: E731
    b02, b03, b04, b08, b11 = (refl(n) for n in ("B02", "B03", "B04", "B08", "B11"))
    ndvi = (b08 - b04) / (b08 + b04 + _EPS)
    mndwi = (b03 - b11) / (b03 + b11 + _EPS)
    ndbi = (b11 - b08) / (b11 + b08 + _EPS)
    bsi = ((b11 + b04) - (b08 + b02)) / ((b11 + b04) + (b08 + b02) + _EPS)

    n_valid = valid.sum(0)
    any_valid = n_valid > 0

    def vmax(x):
        return np.where(any_valid, np.where(valid, x, -np.inf).max(0), np.nan)

    def vmin(x):
        return np.where(any_valid, np.where(valid, x, np.inf).min(0), np.nan)

    def vmean(x):
        s = np.where(valid, x, 0.0).sum(0)
        return np.where(any_valid, s / np.maximum(n_valid, 1), np.nan)

    ndvi_max, ndvi_min = vmax(ndvi), vmin(ndvi)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN columns are expected
        ndvi_median = np.nanmedian(np.where(valid, ndvi, np.nan), axis=0)
    return {
        "n_valid": n_valid.astype(np.uint8),
        "ndvi_max": ndvi_max.astype(np.float32),
        "ndvi_min": ndvi_min.astype(np.float32),
        "ndvi_amp": (ndvi_max - ndvi_min).astype(np.float32),
        "ndvi_median": ndvi_median.astype(np.float32),
        "mndwi_max": vmax(mndwi).astype(np.float32),
        "ndbi_mean": vmean(ndbi).astype(np.float32),
        "bsi_max": vmax(bsi).astype(np.float32),
    }


def consensus_keep(wc: np.ndarray, st: dict[str, np.ndarray], rules: dict = C.CONSENSUS_RULES) -> np.ndarray:
    """True where the WorldCover code agrees with the multi-season profile.

    Comparisons with NaN are False, so pixels with no clear timestep are never kept.
    """
    with np.errstate(invalid="ignore"):
        r = rules
        keep = np.zeros(wc.shape, bool)

        def rule(codes, cond):
            m = np.isin(wc, codes)
            keep[m] = cond[m]

        rule([50], (st["ndvi_max"] < r["built_up_50"]["ndvi_max_lt"]) & (st["mndwi_max"] < r["built_up_50"]["mndwi_max_lt"]))
        rule([40], (st["ndvi_max"] >= r["cropland_40"]["ndvi_max_ge"]) | (st["ndvi_amp"] >= r["cropland_40"]["ndvi_amp_ge"]))
        rule([10], (st["ndvi_max"] >= r["tree_10"]["ndvi_max_ge"]) & (st["ndvi_min"] >= r["tree_10"]["ndvi_min_ge"]))
        rule(
            [20],
            (st["ndvi_max"] >= r["shrub_20"]["ndvi_max_ge"])
            & (st["ndvi_min"] >= r["shrub_20"]["ndvi_min_ge"])
            & (st["bsi_max"] < r["shrub_20"]["bsi_max_lt"]),
        )
        rule([30], (st["ndvi_max"] >= r["grass_30"]["ndvi_max_ge"]) & (st["ndvi_min"] >= r["grass_30"]["ndvi_min_ge"]))
        rule([95], (st["ndvi_max"] >= r["mangrove_95"]["ndvi_max_ge"]) & (st["ndvi_min"] >= r["mangrove_95"]["ndvi_min_ge"]))
        rule([60, 100], (st["ndvi_max"] < r["bare_60_100"]["ndvi_max_lt"]) & (st["mndwi_max"] < r["bare_60_100"]["mndwi_max_lt"]))
        rule([80, 90], (st["mndwi_max"] > r["water_80_90"]["mndwi_max_gt"]) & (st["ndvi_min"] < r["water_80_90"]["ndvi_min_lt"]))
    return keep


def boundary_pixels(label: np.ndarray) -> np.ndarray:
    """Pixels with an 8-neighbour of a different (non-ignored) class."""
    h, w = label.shape
    pad = np.pad(label, 1, mode="edge")
    out = np.zeros((h, w), bool)
    center_ok = label != C.IGNORE
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy == 0 and dx == 0:
                continue
            nb = pad[1 + dy : 1 + dy + h, 1 + dx : 1 + dx + w]
            out |= (nb != label) & (nb != C.IGNORE) & center_ok
    return out


def make_labels(wc: np.ndarray, dn: np.ndarray, valid: np.ndarray, rules: dict = C.CONSENSUS_RULES):
    """Returns (label_raw, label_clean, stats, retention)."""
    from .worldcover import remap

    label_raw = remap(wc)
    st = spectral_stats(dn, valid)
    keep = consensus_keep(wc, st, rules)
    label_clean = label_raw.copy()
    label_clean[~keep] = C.IGNORE
    if C.ERODE_BOUNDARIES:
        label_clean[boundary_pixels(label_raw)] = C.IGNORE

    retention = {}
    for code in sorted(C.WORLDCOVER_REMAP):
        m = wc == code
        n = int(m.sum())
        if n == 0:
            continue
        retention[str(code)] = {
            "pixels": n,
            "kept_by_rule": int((keep & m).sum()),
            "kept_final": int(((label_clean != C.IGNORE) & m).sum()),
        }
    return label_raw, label_clean, st, retention
