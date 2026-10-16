"""Spectral index medians per date and per mapped class. Numpy only.

Three normalised differences on harmonised Sentinel-2 L2A DN (10000 x
reflectance, so the ratios are reflectance ratios):

* NDVI  = (B08 - B04) / (B08 + B04): green vegetation;
* MNDWI = (B03 - B11) / (B03 + B11): open water;
* NDBI  = (B11 - B08) / (B11 + B08): built-up and bare surfaces.

Each value is the median over the pixels that are clear on that date (SCL)
and mapped to that class. They describe the image on that date; they are not
field measurements and not a second classifier.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import numpy as np

# Band positions in the stored 12-band stack (process_api.S2_BANDS order).
BAND_INDEX = {"B02": 0, "B03": 1, "B04": 2, "B08": 3, "B11": 8}
INDICES = {"ndvi": ("B08", "B04"), "mndwi": ("B03", "B11"), "ndbi": ("B11", "B08")}
FIELDS = ["date", "used_by_model", "class_id", "class_name", "clear_pixels", "clear_area_sqkm", *(f"{k}_median" for k in INDICES)]
PIXEL_KM2 = (10.0 * 10.0) / 1e6


def _median_index(a: np.ndarray, b: np.ndarray) -> float | None:
    a = a.astype(np.float32)
    b = b.astype(np.float32)
    den = a + b
    ok = den > 0
    if not ok.any():
        return None
    return round(float(np.median((a[ok] - b[ok]) / den[ok])), 4)


def class_index_medians(
    dn: np.ndarray,
    valid: np.ndarray,
    class_map: np.ndarray,
    class_names: list[str],
    dates: list[str],
    used: list[int],
) -> list[dict[str, Any]]:
    """One row per (date, class); ``class_map`` uses 0 for no data and 1..K for classes."""
    rows: list[dict[str, Any]] = []
    for t in range(dn.shape[0]):
        for class_id, class_name in enumerate(class_names, start=1):
            mask = valid[t] & (class_map == class_id)
            n = int(mask.sum())
            row: dict[str, Any] = {
                "date": dates[t],
                "used_by_model": t in used,
                "class_id": class_id,
                "class_name": class_name,
                "clear_pixels": n,
                "clear_area_sqkm": round(n * PIXEL_KM2, 4),
            }
            for name, (num, sub) in INDICES.items():
                row[f"{name}_median"] = (
                    _median_index(dn[t, BAND_INDEX[num]][mask], dn[t, BAND_INDEX[sub]][mask]) if n else None
                )
            rows.append(row)
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        handle.write(
            "# Medians over pixels clear on that date (Sentinel-2 SCL) and mapped to that class. "
            "NDVI=(B08-B04)/(B08+B04), MNDWI=(B03-B11)/(B03+B11), NDBI=(B11-B08)/(B11+B08), harmonised L2A.\n"
        )
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in FIELDS})
