"""Confusion matrices and the numbers reported in india_benchmark_metrics.json.

Rows are the reference (WorldCover-derived label), columns the prediction.
Producer's accuracy = recall of the reference class; user's accuracy = precision
of the mapped class. Against WorldCover these are *agreement* figures.
"""

from __future__ import annotations

import numpy as np

from . import config as C


def confusion(pred: np.ndarray, ref: np.ndarray, k: int = C.NUM_CLASSES, valid: np.ndarray | None = None) -> np.ndarray:
    m = (ref != C.IGNORE) & (ref < k) & (pred < k)
    if valid is not None:
        m &= valid
    idx = ref[m].astype(np.int64) * k + pred[m].astype(np.int64)
    return np.bincount(idx, minlength=k * k).reshape(k, k)


def confusion_torch(pred, ref, k: int = C.NUM_CLASSES):
    import torch

    m = (ref != C.IGNORE) & (ref < k)
    idx = ref[m] * k + pred[m]
    return torch.bincount(idx, minlength=k * k).reshape(k, k)


def summarize(cm: np.ndarray, names: list[str] = C.CLASS_NAMES) -> dict:
    cm = np.asarray(cm, dtype=np.float64)
    total = cm.sum()
    tp = np.diag(cm)
    ref_tot, pred_tot = cm.sum(1), cm.sum(0)
    with np.errstate(invalid="ignore", divide="ignore"):
        pa = np.where(ref_tot > 0, tp / ref_tot, np.nan)  # producer's (recall)
        ua = np.where(pred_tot > 0, tp / pred_tot, np.nan)  # user's (precision)
        f1 = np.where(pa + ua > 0, 2 * pa * ua / (pa + ua), np.nan)
        iou = np.where(ref_tot + pred_tot - tp > 0, tp / (ref_tot + pred_tot - tp), np.nan)
    present = ref_tot > 0
    oa = float(tp.sum() / total) if total else float("nan")
    pe = float((ref_tot * pred_tot).sum() / total**2) if total else float("nan")
    out = {
        "pixels": int(total),
        "overall_accuracy": round(oa, 4),
        "kappa": round((oa - pe) / (1 - pe), 4) if total and pe < 1 else None,
        "mean_iou": round(float(np.nanmean(iou[present])), 4) if present.any() else None,
        "macro_f1": round(float(np.nanmean(f1[present])), 4) if present.any() else None,
        "per_class": {},
        "confusion_matrix": cm.astype(np.int64).tolist(),
    }
    for i, n in enumerate(names):
        out["per_class"][n] = {
            "reference_pixels": int(ref_tot[i]),
            "reference_share": round(float(ref_tot[i] / total), 4) if total else None,
            "producers_accuracy": None if np.isnan(pa[i]) else round(float(pa[i]), 4),
            "users_accuracy": None if np.isnan(ua[i]) else round(float(ua[i]), 4),
            "f1": None if np.isnan(f1[i]) else round(float(f1[i]), 4),
            "iou": None if np.isnan(iou[i]) else round(float(iou[i]), 4),
        }
    return out


def miou(cm) -> float:
    s = summarize(np.asarray(cm))
    return float(s["mean_iou"]) if s["mean_iou"] is not None else 0.0


def write_cm_csv(path: str, cm, names: list[str] = C.CLASS_NAMES, unit: str = "pixels", scale: float = 1.0) -> None:
    cm = np.asarray(cm, dtype=np.float64) * scale
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# rows = reference (WorldCover-derived), columns = prediction, unit = {unit}\n")
        f.write("reference \\ predicted," + ",".join(names) + "\n")
        for i, n in enumerate(names):
            f.write(n + "," + ",".join(f"{v:.4f}" if scale != 1.0 else str(int(v)) for v in cm[i]) + "\n")
