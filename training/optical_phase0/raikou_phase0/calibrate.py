"""Temperature scaling on validation tiles.

Fitted against label_raw (unfiltered WorldCover-derived labels), so a calibrated
score of 80% means: on held-out Indian tiles, about 80% of pixels given that score
agreed with the WorldCover-2021-derived class. It is not a probability of being
right on the ground.
"""

from __future__ import annotations

import numpy as np

from . import config as C


def sample_pixels(logits: np.ndarray, label: np.ndarray, has_data: np.ndarray, n_max: int, rng) -> tuple[np.ndarray, np.ndarray]:
    ok = (label != C.IGNORE) & has_data
    idx = np.flatnonzero(ok)
    if idx.size > n_max:
        idx = rng.choice(idx, n_max, replace=False)
    k = logits.shape[0]
    return logits.reshape(k, -1)[:, idx].T.astype(np.float32), label.reshape(-1)[idx].astype(np.int64)


def fit_temperature(logits: np.ndarray, labels: np.ndarray) -> float:
    import torch
    import torch.nn.functional as F

    x = torch.from_numpy(logits)
    y = torch.from_numpy(labels)
    log_t = torch.zeros(1, requires_grad=True)
    opt = torch.optim.LBFGS([log_t], lr=0.1, max_iter=200, line_search_fn="strong_wolfe")

    def closure():
        opt.zero_grad()
        loss = F.cross_entropy(x / log_t.exp(), y)
        loss.backward()
        return loss

    opt.step(closure)
    return float(log_t.exp().item())


def reliability(probs: np.ndarray, labels: np.ndarray, n_bins: int = 15) -> dict:
    conf = probs.max(1)
    pred = probs.argmax(1)
    correct = pred == labels
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    bins, ece = [], 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (conf > lo) & (conf <= hi)
        if not m.any():
            continue
        acc, cf, n = float(correct[m].mean()), float(conf[m].mean()), int(m.sum())
        ece += abs(acc - cf) * n / len(conf)
        bins.append({"lo": round(lo, 3), "hi": round(hi, 3), "confidence": round(cf, 4), "accuracy": round(acc, 4), "count": n})
    return {"ece": round(float(ece), 4), "bins": bins}


def softmax_rows(logits: np.ndarray, t: float = 1.0) -> np.ndarray:
    z = logits / t
    z = z - z.max(1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(1, keepdims=True)


def reliability_png(path: str, before: dict, after: dict, temperature: float) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(5, 5))
    ax.plot([0, 1], [0, 1], color="0.6", lw=1, ls="--")
    for rel, label, color in ((before, f"raw (ECE {before['ece']:.3f})", "#d97706"), (after, f"T={temperature:.2f} (ECE {after['ece']:.3f})", "#2563eb")):
        ax.plot([b["confidence"] for b in rel["bins"]], [b["accuracy"] for b in rel["bins"]], marker="o", color=color, label=label)
    ax.set_xlabel("model score (max softmax)")
    ax.set_ylabel("agreement with WorldCover-derived label")
    ax.set_title("Calibration on validation tiles")
    ax.legend(loc="upper left")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
