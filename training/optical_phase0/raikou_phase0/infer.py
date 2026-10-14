"""Whole-tile inference (128 px windows, 50% overlap, Gaussian blending) and the
contract the Raikou backend will call later:

    model = load_model(weights_path, card_path, device)
    probs, embeddings = predict(model, stack_dn, valid_mask, dates)
        stack_dn   (T, 12, H, W) uint16 harmonized DN, bands in config.S2_BANDS order
        valid_mask (T, H, W) bool
        dates      T entries: date / datetime / "YYYY-MM-DD" / (day, month0, year)
        probs      (K, H, W) float16, temperature-scaled softmax
        embeddings (H/P, W/P, D) float16, from the fine-tuned encoder
"""

from __future__ import annotations

import json
import os
from contextlib import nullcontext
from datetime import date, datetime

import numpy as np
import torch

from . import config as C
from . import model as Mdl
from .data import window_starts


def gaussian_window(win: int, sigma_scale: float = 1.0 / 8.0) -> np.ndarray:
    ax = np.arange(win, dtype=np.float64) - (win - 1) / 2.0
    g = np.exp(-(ax**2) / (2.0 * (win * sigma_scale) ** 2))
    w = np.outer(g, g)
    return np.maximum(w / w.max(), 1e-3).astype(np.float32)


def dates_array(dates) -> np.ndarray:
    out = []
    for d in dates:
        if isinstance(d, str):
            d = date.fromisoformat(d[:10])
        if isinstance(d, datetime):
            d = d.date()
        if isinstance(d, date):
            out.append((d.day, d.month - 1, d.year))
        else:
            day, month0, year = (int(v) for v in d)
            out.append((day, month0, year))
    return np.asarray(out, np.int64)


def _amp(amp: str):
    dtype = {"fp16": torch.float16, "bf16": torch.bfloat16}.get(amp)
    return (lambda: torch.autocast("cuda", dtype=dtype)) if dtype is not None else nullcontext


@torch.no_grad()
def predict_tile(
    model: Mdl.LulcNet,
    dn: np.ndarray,
    valid: np.ndarray,
    dates,
    use_timesteps: list[int] | None = None,
    window: int = 128,
    stride: int = 64,
    batch: int = 8,
    amp: str = "fp32",
    device: str = "cuda",
    min_timestep_valid: float = 0.5,
    want_features: bool = True,
) -> dict:
    """Blended logits (K, H, W) float32, has_data (H, W) and optional features (D, H/P, W/P)."""
    model.eval()
    use = list(range(dn.shape[0])) if use_timesteps is None else list(use_timesteps)
    T, _, H, W = dn[use].shape
    P = model.P
    if H % P or W % P or window % P or stride % P:
        raise ValueError("tile size, window and stride must be multiples of the patch size")
    K, D = model.head.out_channels, model.hparams["embed_dim"]
    g = torch.from_numpy(gaussian_window(window)).to(device)
    gt = torch.from_numpy(gaussian_window(window // P)).to(device)
    acc = torch.zeros(K, H, W, device=device)
    wsum = torch.zeros(H, W, device=device)
    facc = torch.zeros(D, H // P, W // P, device=device) if want_features else None
    fsum = torch.zeros(H // P, W // P, device=device) if want_features else None

    dn_t = torch.from_numpy(np.ascontiguousarray(dn[use]).astype(np.float32))
    val_t = torch.from_numpy(np.ascontiguousarray(valid[use]).astype(bool))
    dts = torch.from_numpy(dates_array(dates)[use]).long()
    coords = [(y, x) for y in window_starts(H, window, stride) for x in window_starts(W, window, stride)]
    coords = [(y, x) for y, x in coords if bool(val_t[:, y : y + window, x : x + window].any())]
    autocast = _amp(amp)

    for i in range(0, len(coords), batch):
        chunk = coords[i : i + batch]
        dnb = torch.stack([dn_t[:, :, y : y + window, x : x + window] for y, x in chunk]).to(device)
        vb = torch.stack([val_t[:, y : y + window, x : x + window] for y, x in chunk]).to(device)
        vfrac = vb.float().mean((2, 3))
        avail = vfrac >= min_timestep_valid
        none = torch.nonzero(~avail.any(1)).squeeze(1)
        if none.numel():
            avail[none, vfrac[none].argmax(1)] = True
        pv = vb & avail[:, :, None, None]
        db = dts.unsqueeze(0).expand(len(chunk), -1, -1).to(device)
        with autocast():
            logits, feat = model(dnb, pv, db, return_features=True)
        logits, feat = logits.float(), feat.float()
        for j, (y, x) in enumerate(chunk):
            acc[:, y : y + window, x : x + window] += logits[j] * g
            wsum[y : y + window, x : x + window] += g
            if want_features:
                ty, tx, tw = y // P, x // P, window // P
                facc[:, ty : ty + tw, tx : tx + tw] += feat[j] * gt
                fsum[ty : ty + tw, tx : tx + tw] += gt

    logits = (acc / wsum.clamp(min=1e-6)).cpu().numpy()
    out = {"logits": logits, "has_data": valid[use].any(0), "timesteps_used": use}
    if want_features:
        out["features"] = (facc / fsum.clamp(min=1e-6)).cpu().numpy().astype(np.float16)
    return out


def softmax(logits: np.ndarray, temperature: float = 1.0, axis: int = 0) -> np.ndarray:
    z = logits / float(temperature)
    z = z - z.max(axis=axis, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=axis, keepdims=True)


def load_model(weights_path: str, card_path: str, device: str = "cuda") -> Mdl.LulcNet:
    with open(card_path, encoding="utf-8") as f:
        card = json.load(f)
    base_dir = os.path.join(os.path.dirname(os.path.abspath(card_path)), card["architecture"]["base_config_dir"])
    enc = Mdl.load_base_encoder(base_dir, load_weights=False)  # architecture only; no download
    model = Mdl.build_model(enc, hparams=card["architecture"]["hparams"])
    Mdl.load_weights(model, weights_path)
    model.card = card
    return model.eval().to(device)


def predict(model, stack_dn, valid_mask, dates, temperature: float | None = None, amp: str = "fp16", device: str = "cuda"):
    out = predict_tile(model, stack_dn, valid_mask, dates, amp=amp, device=device)
    if temperature is None:
        temperature = getattr(model, "card", {}).get("calibration", {}).get("temperature", 1.0)
    probs = softmax(out["logits"], temperature).astype(np.float16)
    probs[:, ~out["has_data"]] = 0
    return probs, np.transpose(out["features"], (1, 2, 0))
