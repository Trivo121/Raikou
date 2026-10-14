"""Notebook 3: whole-tile evaluation exactly as production will run it.

T1 = the single clearest date of the tile (what a one-image /optical run does).
T3 = every season with enough clear pixels.
Scores are computed against label_raw (primary, unfiltered) and label_clean
(secondary, the training-style labels), with and without the 4 px sieve.
"""

from __future__ import annotations

import time

import numpy as np

from . import calibrate as CAL
from . import config as C
from . import metrics as M
from .data import LocalTile
from .infer import predict_tile


def timesteps_for(tile: LocalTile, mode: str) -> list[int]:
    fr = [s.get("valid_fraction", 0.0) if s["status"] != "missing" else 0.0 for s in tile.meta["timesteps"]]
    best = int(np.argmax(fr))
    if mode == "T1":
        return [best]
    use = [i for i, f in enumerate(fr) if f >= C.MIN_TIMESTEP_VALID]
    return use or [best]


def sieve_classes(cls: np.ndarray, size: int) -> np.ndarray:
    from rasterio.features import sieve

    exp = np.where(cls == C.IGNORE, 0, cls.astype(np.int16) + 1).astype(np.uint8)
    out = sieve(exp, size=size, connectivity=8, mask=exp > 0)
    return np.where(out == 0, C.IGNORE, out.astype(np.int16) - 1).astype(np.uint8)


def benchmark(model, tile_ids, paths: C.Paths, amp: str = "fp32", sieve_px: int = 4, progress=None) -> dict:
    import torch

    k = C.NUM_CLASSES
    cms = {m: {lab: {s: np.zeros((k, k), np.int64) for s in ("sieved", "unsieved")} for lab in ("raw", "clean")} for m in ("T1", "T3")}
    per_tile, timing = [], []
    torch.cuda.reset_peak_memory_stats()
    it = progress(tile_ids) if progress else tile_ids
    for tid in it:
        lt = LocalTile(paths.local, tid)
        dn, valid = np.asarray(lt.arr("dn")), np.asarray(lt.arr("valid")).astype(bool)
        raw, clean = np.asarray(lt.arr("label_raw")), np.asarray(lt.arr("label_clean"))
        row = {"tile_id": tid, "zone": lt.meta["zone"]}
        for mode in ("T1", "T3"):
            use = timesteps_for(lt, mode)
            torch.cuda.synchronize()
            t0 = time.time()
            res = predict_tile(model, dn, valid, lt.dates, use_timesteps=use, amp=amp, want_features=False)
            torch.cuda.synchronize()
            timing.append({"tile_id": tid, "mode": mode, "seconds": round(time.time() - t0, 2), "timesteps": len(use)})
            pred = res["logits"].argmax(0).astype(np.uint8)
            pred[~res["has_data"]] = C.IGNORE
            pred_s = sieve_classes(pred, sieve_px)
            for lab, ref in (("raw", raw), ("clean", clean)):
                cms[mode][lab]["unsieved"] += M.confusion(pred, ref)
                cms[mode][lab]["sieved"] += M.confusion(pred_s, ref)
            row[f"{mode}_oa_raw_sieved"] = M.summarize(M.confusion(pred_s, raw))["overall_accuracy"]
        per_tile.append(row)
    out = {
        "amp": amp,
        "sieve_px": sieve_px,
        "modes": {m: {lab: {s: M.summarize(cm) for s, cm in d.items()} for lab, d in dd.items()} for m, dd in cms.items()},
        "per_tile": per_tile,
        "timing": timing,
        "peak_vram_gb": round(torch.cuda.max_memory_allocated() / 2**30, 2),
    }
    out["summary"] = {m: out["modes"][m]["raw"]["sieved"] for m in ("T1", "T3")}
    return out


def calibration_set(model, tile_ids, paths: C.Paths, amp: str = "fp32", n_per_tile_mode: int = 20000, seed: int = 0, progress=None):
    rng = np.random.default_rng(seed)
    xs, ys = [], []
    it = progress(tile_ids) if progress else tile_ids
    for tid in it:
        lt = LocalTile(paths.local, tid)
        dn, valid = np.asarray(lt.arr("dn")), np.asarray(lt.arr("valid")).astype(bool)
        raw = np.asarray(lt.arr("label_raw"))
        for mode in ("T1", "T3"):
            res = predict_tile(model, dn, valid, lt.dates, use_timesteps=timesteps_for(lt, mode), amp=amp, want_features=False)
            x, y = CAL.sample_pixels(res["logits"], raw, res["has_data"], n_per_tile_mode, rng)
            xs.append(x)
            ys.append(y)
    return np.concatenate(xs), np.concatenate(ys)


def calibrate(model, val_ids, paths: C.Paths, amp: str = "fp32", progress=None) -> dict:
    x, y = calibration_set(model, val_ids, paths, amp=amp, progress=progress)
    t = CAL.fit_temperature(x, y)
    before = CAL.reliability(CAL.softmax_rows(x, 1.0), y)
    after = CAL.reliability(CAL.softmax_rows(x, t), y)
    return {
        "temperature": round(t, 4),
        "fit_on": f"{len(val_ids)} validation tiles, label_raw, T1 + T3 pooled, {len(y)} pixels",
        "meaning": "calibrated probability that a pixel agrees with the WorldCover-2021-derived class on held-out Indian tiles",
        "ece_before": before["ece"],
        "ece_after": after["ece"],
        "bins_before": before["bins"],
        "bins_after": after["bins"],
    }
