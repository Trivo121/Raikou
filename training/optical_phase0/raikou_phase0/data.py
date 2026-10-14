"""Training/validation data on the Colab VM's local disk.

Tiles are copied from Drive once per session and unpacked to uncompressed .npy
files that are memory-mapped, so DataLoader workers read crops at disk speed.
Training draws fresh random 128 px crops each epoch (more variety than fixed
overlapping chips, and 100 files instead of thousands).
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import date

import numpy as np

from . import config as C

ARRAYS = ("dn", "valid", "label_raw", "label_clean", "wc")


def stage_tiles(tile_ids, paths: C.Paths, progress=None) -> None:
    """Drive .npz -> local .npy (memory-mappable). Idempotent."""
    os.makedirs(paths.local, exist_ok=True)
    it = progress(tile_ids) if progress else tile_ids
    for tid in it:
        dst = os.path.join(paths.local, tid)
        if os.path.exists(os.path.join(dst, "_ok")):
            continue
        os.makedirs(dst, exist_ok=True)
        tmp = os.path.join(paths.local, f"{tid}.npz")
        shutil.copyfile(os.path.join(paths.tiles, f"{tid}.npz"), tmp)
        with np.load(tmp) as z:
            for k in ARRAYS:
                np.save(os.path.join(dst, f"{k}.npy"), z[k])
        shutil.copyfile(os.path.join(paths.tiles, f"{tid}.json"), os.path.join(dst, "meta.json"))
        os.remove(tmp)
        open(os.path.join(dst, "_ok"), "w").close()


def _date_triplet(iso: str) -> tuple[int, int, int]:
    d = date.fromisoformat(iso[:10])
    return (d.day, d.month - 1, d.year)  # OlmoEarth: day 1-31, month 0-11, year


class LocalTile:
    def __init__(self, root: str, tile_id: str):
        self.tile_id = tile_id
        self.dir = os.path.join(root, tile_id)
        with open(os.path.join(self.dir, "meta.json"), encoding="utf-8") as f:
            self.meta = json.load(f)
        self.dates = np.array([_date_triplet(s["date"]) for s in self.meta["timesteps"]], np.int64)
        self._arr: dict = {}

    def arr(self, key: str) -> np.ndarray:
        if key not in self._arr:  # opened lazily so each DataLoader worker maps its own copy
            self._arr[key] = np.load(os.path.join(self.dir, f"{key}.npy"), mmap_mode="r")
        return self._arr[key]

    @property
    def shape(self) -> tuple[int, int]:
        return tuple(self.arr("label_raw").shape)

    def __getstate__(self):
        d = self.__dict__.copy()
        d["_arr"] = {}
        return d


def window_starts(n: int, win: int, stride: int) -> list[int]:
    """Window origins covering [0, n) with the last window flush to the edge."""
    if n <= win:
        return [0]
    starts = list(range(0, n - win + 1, stride))
    if starts[-1] != n - win:
        starts.append(n - win)
    return starts


class TrainCrops:
    """Random crops with rotation/flip and +/-jitter brightness/contrast per timestep."""

    def __init__(self, tiles: list[LocalTile], cfg: C.TrainConfig):
        self.tiles, self.cfg = tiles, cfg
        self.epoch = 0

    def set_epoch(self, epoch: int) -> None:
        self.epoch = epoch

    def __len__(self) -> int:
        return len(self.tiles) * self.cfg.crops_per_tile

    def __getitem__(self, i: int) -> dict:
        import torch

        cfg, c = self.cfg, self.cfg.crop
        rng = np.random.default_rng([cfg.seed, self.epoch, i])
        tile = self.tiles[i % len(self.tiles)]
        lc = tile.arr("label_clean")
        h, w = lc.shape
        for _ in range(25):
            y, x = int(rng.integers(0, h - c + 1)), int(rng.integers(0, w - c + 1))
            lbl = np.asarray(lc[y : y + c, x : x + c])
            if (lbl == C.IGNORE).mean() > cfg.max_ignore_frac:
                continue
            val = np.asarray(tile.arr("valid")[:, y : y + c, x : x + c]).astype(bool)
            if (val.mean((1, 2)) >= cfg.min_crop_timestep_valid).any():
                break
        else:
            val = np.asarray(tile.arr("valid")[:, y : y + c, x : x + c]).astype(bool)
        dn = np.asarray(tile.arr("dn")[:, :, y : y + c, x : x + c]).astype(np.float32)
        lbl = lbl.copy()

        k = int(rng.integers(0, 4))
        if k:
            dn, val, lbl = np.rot90(dn, k, (2, 3)), np.rot90(val, k, (1, 2)), np.rot90(lbl, k, (0, 1))
        if rng.random() < 0.5:
            dn, val, lbl = dn[..., ::-1], val[..., ::-1], lbl[..., ::-1]
        if cfg.jitter > 0:
            T = dn.shape[0]
            gain = 1.0 + rng.uniform(-cfg.jitter, cfg.jitter, (T, 1, 1, 1)).astype(np.float32)
            contrast = 1.0 + rng.uniform(-cfg.jitter, cfg.jitter, (T, 1, 1, 1)).astype(np.float32)
            mean = dn.mean((2, 3), keepdims=True)
            dn = np.clip((dn - mean) * contrast + mean * gain, 0.0, 65535.0)

        vfrac = val.mean((1, 2))
        avail = vfrac >= cfg.min_crop_timestep_valid
        if not avail.any():
            if vfrac.max() > 0:
                avail[int(np.argmax(vfrac))] = True
            else:  # nothing clear at all: keep shapes, contribute no loss
                lbl = np.full_like(lbl, C.IGNORE)
                val[0] = True
                avail[0] = True
        return {
            "dn": torch.from_numpy(np.ascontiguousarray(dn)),
            "valid": torch.from_numpy(np.ascontiguousarray(val)),
            "avail": torch.from_numpy(avail),
            "vfrac": torch.from_numpy(vfrac.astype(np.float32)),
            "label": torch.from_numpy(np.ascontiguousarray(lbl).astype(np.int64)),
            "dates": torch.from_numpy(tile.dates.copy()),
        }


class ValChips:
    """Fixed, non-overlapping 128 px chips (last row/column flush to the edge)."""

    def __init__(self, tiles: list[LocalTile], crop: int = 128, min_timestep_valid: float = 0.5):
        self.tiles, self.crop, self.min_tv = tiles, crop, min_timestep_valid
        self.index = []
        for ti, t in enumerate(tiles):
            h, w = t.shape
            for y in window_starts(h, crop, crop):
                for x in window_starts(w, crop, crop):
                    self.index.append((ti, y, x))

    def __len__(self) -> int:
        return len(self.index)

    def __getitem__(self, i: int) -> dict:
        import torch

        ti, y, x = self.index[i]
        t, c = self.tiles[ti], self.crop
        val = np.asarray(t.arr("valid")[:, y : y + c, x : x + c]).astype(bool)
        vfrac = val.mean((1, 2))
        avail = vfrac >= self.min_tv
        if not avail.any() and vfrac.max() > 0:
            avail[int(np.argmax(vfrac))] = True
        return {
            "dn": torch.from_numpy(np.asarray(t.arr("dn")[:, :, y : y + c, x : x + c]).astype(np.float32)),
            "valid": torch.from_numpy(val),
            "avail": torch.from_numpy(avail),
            "vfrac": torch.from_numpy(vfrac.astype(np.float32)),
            "label_clean": torch.from_numpy(np.asarray(t.arr("label_clean")[y : y + c, x : x + c]).astype(np.int64)),
            "label_raw": torch.from_numpy(np.asarray(t.arr("label_raw")[y : y + c, x : x + c]).astype(np.int64)),
            "dates": torch.from_numpy(t.dates.copy()),
        }


def choose_timesteps(avail, vfrac, t_target: int, generator=None):
    """Batch T-dropout: keep `t_target` random available timesteps per sample.

    avail: (B, T) bool. Returns (B, T) bool with at least one True per sample.
    """
    import torch

    B, T = avail.shape
    scores = torch.rand(B, T, generator=generator, device=avail.device)
    scores = scores.masked_fill(~avail, -1.0)
    top = scores.topk(min(t_target, T), dim=1).indices
    sel = torch.zeros_like(avail)
    sel.scatter_(1, top, True)
    sel &= avail
    none = ~sel.any(1)
    if none.any():
        best = vfrac.argmax(1)
        sel[none, best[none]] = True
    return sel


def clearest_one(avail, vfrac):
    """T=1 evaluation: the single clearest available timestep per sample."""
    import torch

    v = vfrac.masked_fill(~avail, -1.0)
    sel = torch.zeros_like(avail)
    sel[torch.arange(avail.shape[0], device=avail.device), v.argmax(1)] = True
    return sel
