"""Notebook 1 pipeline: one tile in, one compressed .npz + .json + quicklook out.

Resumable: a tile whose .json says status=done is skipped. Each tile is written to
local disk first and then copied to Drive, so a disconnect never leaves half a file.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import numpy as np

from . import config as C
from . import labels as L
from . import s2_pc
from . import worldcover as WC


def tile_json_path(paths: C.Paths, tile_id: str) -> str:
    return os.path.join(paths.tiles, f"{tile_id}.json")


def tile_npz_path(paths: C.Paths, tile_id: str) -> str:
    return os.path.join(paths.tiles, f"{tile_id}.npz")


def is_done(paths: C.Paths, tile_id: str) -> bool:
    jp, zp = tile_json_path(paths, tile_id), tile_npz_path(paths, tile_id)
    if not (os.path.exists(jp) and os.path.exists(zp)):
        return False
    try:
        with open(jp, encoding="utf-8") as f:
            return json.load(f).get("status") == "done"
    except (OSError, json.JSONDecodeError):
        return False


def process_tile(tile, catalog, paths: C.Paths, hydro_year: int = C.TRAIN_HYDRO_YEAR) -> dict:
    windows = C.season_windows(hydro_year)
    T, H, W = len(windows), tile.height, tile.width
    dn = np.zeros((T, len(C.S2_BANDS), H, W), np.uint16)
    valid = np.zeros((T, H, W), bool)
    steps = []
    for t, win in enumerate(windows):
        acq = s2_pc.pick_acquisition(catalog, tile, win["start"], win["end"])
        step = {"season": win["name"], "window": [win["start"], win["end"]], "nominal": win["nominal"]}
        if acq is None or acq.get("aoi_valid", 0.0) < C.MIN_TIMESTEP_VALID:
            step.update({"status": "missing", "acquisition": s2_pc.acquisition_meta(acq), "date": win["nominal"]})
            steps.append(step)
            continue
        got = s2_pc.load_acquisition(acq, tile)
        dn[t], valid[t] = got["dn"], got["valid"]
        step.update(
            {
                "status": "ok" if got["valid_fraction"] >= C.MIN_TIMESTEP_VALID else "mostly_invalid",
                "date": acq["date"],
                "acquisition": s2_pc.acquisition_meta(acq),
                "baseline": got["baseline"],
                "offset_applied": got["offset_applied"],
                "valid_fraction": round(got["valid_fraction"], 4),
            }
        )
        steps.append(step)

    wc = WC.read_worldcover(tile)
    label_raw, label_clean, st, retention = L.make_labels(wc, dn, valid)

    meta = {
        "tile_id": tile.tile_id,
        "zone": tile.zone,
        "anchor": tile.anchor,
        "split": tile.split,
        "epsg": tile.epsg,
        "transform": list(tile.transform)[:6],
        "bounds": list(tile.bounds),
        "center_latlon": [tile.lat, tile.lon],
        "hydro_year": hydro_year,
        "timesteps": steps,
        "worldcover_composition": WC.composition(wc),
        "retention": retention,
        "label_raw_valid_fraction": round(float((label_raw != C.IGNORE).mean()), 4),
        "label_clean_valid_fraction": round(float((label_clean != C.IGNORE).mean()), 4),
        "band_order": C.S2_BANDS,
        "settings": C.ALL_SETTINGS,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "done",
    }

    with tempfile.TemporaryDirectory() as tmp:
        local_npz = os.path.join(tmp, f"{tile.tile_id}.npz")
        np.savez_compressed(
            local_npz,
            dn=dn,
            valid=valid.astype(np.uint8),
            wc=wc,
            label_raw=label_raw,
            label_clean=label_clean,
        )
        ql = os.path.join(tmp, f"{tile.tile_id}.png")
        save_quicklook(ql, dn, valid, label_raw, label_clean, title=tile.tile_id)
        os.makedirs(paths.tiles, exist_ok=True)
        os.makedirs(paths.quicklooks, exist_ok=True)
        shutil.copyfile(local_npz, tile_npz_path(paths, tile.tile_id) + ".part")
        os.replace(tile_npz_path(paths, tile.tile_id) + ".part", tile_npz_path(paths, tile.tile_id))
        shutil.copyfile(ql, os.path.join(paths.quicklooks, f"{tile.tile_id}.png"))
    with open(tile_json_path(paths, tile.tile_id), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1)
    return meta


def build_all(tiles, paths: C.Paths, hydro_year: int = C.TRAIN_HYDRO_YEAR, workers: int = 3, progress=None) -> dict:
    """Process every tile not yet done. Returns {tile_id: 'done'|'skipped'|error text}."""
    catalog = s2_pc.open_catalog()
    todo = [t for t in tiles if not is_done(paths, t.tile_id)]
    results = {t.tile_id: "skipped" for t in tiles if t not in todo}
    bar = progress(total=len(todo)) if progress else None
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(process_tile, t, catalog, paths, hydro_year): t for t in todo}
        for fut in as_completed(futs):
            t = futs[fut]
            try:
                fut.result()
                results[t.tile_id] = "done"
            except Exception as e:  # keep going; the failed tile is retried on the next run
                results[t.tile_id] = f"error: {e!r}"
                with open(os.path.join(paths.tiles, f"{t.tile_id}.error.txt"), "w", encoding="utf-8") as f:
                    f.write(traceback.format_exc())
            if bar:
                bar.update(1)
    if bar:
        bar.close()
    return results


# --------------------------------------------------------------------------- previews


def class_rgb(label: np.ndarray) -> np.ndarray:
    lut = np.zeros((256, 3), np.uint8)
    for i, c in enumerate(C.CLASS_COLORS):
        lut[i] = c
    return lut[label]


def true_color(dn_t: np.ndarray, valid_t: np.ndarray | None = None, max_refl: float = 0.30) -> np.ndarray:
    """(12, H, W) harmonized DN -> (H, W, 3) uint8, gamma-stretched."""
    b = C.BAND_INDEX
    rgb = np.stack([dn_t[b["B04"]], dn_t[b["B03"]], dn_t[b["B02"]]], -1).astype(np.float32) / 10000.0
    rgb = np.clip(rgb / max_refl, 0, 1) ** (1 / 1.8)
    out = (rgb * 255).astype(np.uint8)
    if valid_t is not None:
        out[~valid_t] = (out[~valid_t] * 0.35).astype(np.uint8)
    return out


def save_quicklook(path, dn, valid, label_raw, label_clean, title: str = "", size: int = 400) -> None:
    from PIL import Image, ImageDraw

    t = int(np.argmax(valid.reshape(valid.shape[0], -1).mean(1)))
    panels = [true_color(dn[t], valid[t]), class_rgb(label_raw), class_rgb(label_clean)]
    ims = [Image.fromarray(p).resize((size, size), Image.NEAREST) for p in panels]
    canvas = Image.new("RGB", (size * 3 + 20, size + 28), (255, 255, 255))
    for i, im in enumerate(ims):
        canvas.paste(im, (i * (size + 10), 28))
    d = ImageDraw.Draw(canvas)
    d.text((4, 6), f"{title}   |   true colour (t{t})   |   WorldCover remapped   |   cleaned labels (black = ignored)", fill=(0, 0, 0))
    canvas.save(path)


# --------------------------------------------------------------------------- reports


def worldcover_preview(tiles, workers: int = 8) -> list[dict]:
    """WorldCover shares per tile before any Sentinel-2 is fetched (a couple of minutes)."""
    rows = []

    def one(t):
        wc = WC.read_worldcover(t)
        lbl = WC.remap(wc)
        row = {"tile_id": t.tile_id, "zone": t.zone, "split": t.split}
        for i, n in enumerate(C.CLASS_NAMES):
            row[n] = round(float((lbl == i).mean()), 3)
        row["ignored"] = round(float((lbl == C.IGNORE).mean()), 3)
        flags = []
        if row["Water bodies & wetlands"] > 0.7:
            flags.append("mostly water")
        if max(row[n] for n in C.CLASS_NAMES) > 0.95:
            flags.append("single class")
        if row["ignored"] > 0.2:
            flags.append("much nodata/snow")
        row["flags"] = ", ".join(flags)
        return row

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(one, tiles):
            rows.append(r)
    return rows


def load_all_meta(paths: C.Paths, tiles) -> list[dict]:
    out = []
    for t in tiles:
        p = tile_json_path(paths, t.tile_id)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                out.append(json.load(f))
    return out


def retention_table(metas: list[dict]) -> list[dict]:
    """Share of each WorldCover class kept after the consensus rule, by zone."""
    agg: dict = {}
    for m in metas:
        for code, r in m["retention"].items():
            a = agg.setdefault((m["zone"], code), {"pixels": 0, "kept_by_rule": 0, "kept_final": 0})
            for k in a:
                a[k] += r[k]
    rows = []
    for (zone, code), a in sorted(agg.items()):
        rows.append(
            {
                "zone": zone,
                "worldcover_code": int(code),
                "pixels": a["pixels"],
                "kept_by_rule_%": round(100.0 * a["kept_by_rule"] / max(a["pixels"], 1), 1),
                "kept_final_%": round(100.0 * a["kept_final"] / max(a["pixels"], 1), 1),
                "warn": "rule removes >70%" if a["pixels"] > 50_000 and a["kept_by_rule"] < 0.3 * a["pixels"] else "",
            }
        )
    return rows


def timestep_table(metas: list[dict]) -> list[dict]:
    rows = []
    for m in metas:
        row = {"tile_id": m["tile_id"], "split": m["split"]}
        for s in m["timesteps"]:
            row[s["season"]] = f'{s.get("date", "")} ({s.get("valid_fraction", 0):.2f})' if s["status"] != "missing" else "missing"
        rows.append(row)
    return rows
