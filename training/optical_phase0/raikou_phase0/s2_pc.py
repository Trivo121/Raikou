"""Sentinel-2 L2A from Microsoft Planetary Computer (free, no Copernicus quota).

Per tile and season window:
  1. search PC STAC for granules over the tile (granule cloud below MAX_SCENE_CLOUD),
  2. group granules into acquisitions (same day, orbit and platform),
  3. read the SCL over the tile at 60 m for the best few and keep the clearest,
  4. read the 12 bands + SCL for that acquisition on the exact 10 m tile grid,
  5. remove the baseline >= 04.00 offset so DNs are comparable across years.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict

import numpy as np

from . import config as C

# PC items carry no raster:bands metadata, so odc-stac is told dtype and nodata.
STAC_CFG = {
    C.PC_COLLECTION: {
        "assets": {
            "*": {"data_type": "uint16", "nodata": 0, "unit": "1"},
            "SCL": {"data_type": "uint8", "nodata": 0, "unit": "1"},
        }
    },
    "*": {"warnings": "ignore"},
}


def open_catalog():
    """Unsigned catalog: asset URLs are signed at read time (patch_url), so long runs
    never trip over expired SAS tokens and nothing is signed twice."""
    import pystac_client

    return pystac_client.Client.open(C.PC_STAC_URL)


def processing_baseline(item) -> str:
    pb = item.properties.get("s2:processing_baseline")
    if pb:
        return str(pb)
    m = re.search(r"_N(\d{2})(\d{2})_", item.properties.get("s2:product_uri", item.id))
    return f"{m.group(1)}.{m.group(2)}" if m else "00.00"


def needs_offset(baseline: str) -> bool:
    try:
        return float(baseline) >= 4.0
    except ValueError:
        return False


def harmonize(dn: np.ndarray, baseline: str) -> tuple[np.ndarray, bool]:
    """Subtract BOA_OFFSET for baseline >= 04.00. 0 stays 0 (nodata); validity comes from SCL."""
    if not needs_offset(baseline):
        return dn, False
    x = dn.astype(np.int32) - C.BOA_OFFSET
    np.clip(x, 0, None, out=x)
    x[dn == 0] = 0
    return x.astype(np.uint16), True


def _retry(fn, attempts: int = 4, base_delay: float = 5.0):
    last = None
    for k in range(attempts):
        try:
            return fn()
        except Exception as e:  # network hiccups, expired SAS tokens, 5xx
            last = e
            time.sleep(base_delay * (2**k))
    raise last


def search_acquisitions(catalog, tile, start: str, end: str, max_scene_cloud: float = C.MAX_SCENE_CLOUD) -> list[dict]:
    """Granules over the tile grouped into acquisitions, clearest metadata first."""

    def _search():
        s = catalog.search(
            collections=[C.PC_COLLECTION],
            bbox=list(tile.bbox_lonlat()),
            datetime=f"{start}/{end}",
            query={"eo:cloud_cover": {"lt": max_scene_cloud}},
        )
        return list(s.items())

    items = _retry(_search)
    groups: dict = defaultdict(list)
    for it in items:
        key = (
            it.datetime.date().isoformat(),
            it.properties.get("sat:relative_orbit"),
            it.properties.get("platform"),
        )
        groups[key].append(it)
    acqs = []
    for (day, orbit, platform), its in groups.items():
        baselines = sorted({processing_baseline(i) for i in its})
        acqs.append(
            {
                "date": day,
                "datetime": min(i.datetime for i in its).isoformat(),
                "relative_orbit": orbit,
                "platform": platform,
                "items": its,
                "item_ids": [i.id for i in its],
                "mgrs_tiles": sorted({i.properties.get("s2:mgrs_tile", "") for i in its}),
                "baselines": baselines,
                "granule_cloud_mean": float(np.mean([i.properties.get("eo:cloud_cover", 100.0) for i in its])),
            }
        )
    acqs.sort(key=lambda a: a["granule_cloud_mean"])
    return acqs


def _load(items, geobox, bands: list[str], pool: int = 8):
    import odc.stac
    import planetary_computer

    return odc.stac.load(
        items,
        bands=bands,
        geobox=geobox,
        groupby="solar_day",
        resampling="nearest",  # same as the Copernicus Process API default
        stac_cfg=STAC_CFG,
        patch_url=planetary_computer.sign,
        pool=pool,
        fail_on_error=True,
    )


def _first_time(da) -> np.ndarray:
    a = np.asarray(da.values)
    return a[0] if a.ndim == 3 else a


def aoi_valid_fraction(acq: dict, tile) -> float:
    """Share of the tile that is clear in this acquisition, from SCL at 60 m."""
    ds = _retry(lambda: _load(acq["items"], tile.geobox(res=60), ["SCL"], pool=4))
    scl = _first_time(ds["SCL"])
    return float((~np.isin(scl, C.SCL_INVALID)).mean())


def pick_acquisition(catalog, tile, start: str, end: str) -> dict | None:
    acqs = search_acquisitions(catalog, tile, start, end)
    best = None
    for acq in acqs[: C.CANDIDATES_TO_CHECK]:
        if len(acq["baselines"]) > 1:
            continue  # mixed baselines inside one acquisition: skip, cannot harmonize once
        acq["aoi_valid"] = aoi_valid_fraction(acq, tile)
        if best is None or acq["aoi_valid"] > best["aoi_valid"]:
            best = acq
        if acq["aoi_valid"] >= C.GOOD_ENOUGH_VALID:
            break
    if best is not None:
        best["n_candidates"] = len(acqs)
    return best


def load_acquisition(acq: dict, tile) -> dict:
    """12 bands (harmonized DN, uint16) + SCL + validity on the tile grid."""
    ds = _retry(lambda: _load(acq["items"], tile.geobox(), C.S2_BANDS + ["SCL"]))
    raw = np.stack([_first_time(ds[b]) for b in C.S2_BANDS]).astype(np.uint16)
    scl = _first_time(ds["SCL"]).astype(np.uint8)
    if raw.shape[1:] != (tile.height, tile.width):
        raise RuntimeError(f"{tile.tile_id}: loaded {raw.shape}, expected {(tile.height, tile.width)}")
    baseline = acq["baselines"][0]
    dn, offset_applied = harmonize(raw, baseline)
    valid = (~np.isin(scl, C.SCL_INVALID)) & (raw[0] > 0)
    return {
        "dn": dn,
        "scl": scl,
        "valid": valid,
        "baseline": baseline,
        "offset_applied": offset_applied,
        "valid_fraction": float(valid.mean()),
    }


def acquisition_meta(acq: dict | None) -> dict | None:
    if acq is None:
        return None
    return {k: v for k, v in acq.items() if k != "items"}
