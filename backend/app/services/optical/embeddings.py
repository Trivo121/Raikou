"""What the land-cover model sees, kept small enough to query. Numpy and Pillow only.

The fine-tuned OlmoEarth encoder gives one 768-d feature per token (a 4 x 4
pixel, 40 m cell). Two derived products are stored per run:

* **Token embeddings, reduced** to the top singular directions of the
  L2-normalised tokens (uncentred, so the shared direction is kept). The API
  turns them into a "looks similar to the model" heatmap for a clicked spot:
  cosine similarity in that subspace, which tracks the full 768-d cosine as
  closely as the recorded ``energy_captured`` says.
* **Window vectors**: the mean of the normalised tokens in each 16 x 16-token
  (640 m) window, re-normalised, at full width. These go to Qdrant so a spot
  can be compared with windows from the user's other runs.

None of this is a land-cover class, a probability or a measurement; it is how
alike two places look to this model, and the wording everywhere says so.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import numpy as np

LABEL = "Looks similar to the model"
MEANING = (
    "How close each 40 m spot is to the clicked spot in the fine-tuned OlmoEarth encoder's features "
    "(cosine similarity, -1 to 1). High values mean the model sees the two alike. It is not a land-cover "
    "class, a probability or a measurement."
)
# The release's own similarity.png and .qml use magma over -0.2..1.0.
VMIN, VMAX = -0.2, 1.0
MAGMA_STOPS = [(0.0, "#000004"), (0.25, "#51127c"), (0.5, "#b73779"), (0.75, "#fc8961"), (1.0, "#fcfdbf")]
# The release's score .qml uses viridis.
VIRIDIS_STOPS = [(0.0, "#440154"), (0.25, "#3b528b"), (0.5, "#21918c"), (0.75, "#5ec962"), (1.0, "#fde725")]


def token_validity(has_data: np.ndarray, patch: int) -> np.ndarray:
    """(H, W) pixel mask -> (H/P, W/P): a token has data when any of its pixels has."""
    height, width = has_data.shape
    if height % patch or width % patch:
        raise ValueError("The grid must be a whole number of tokens.")
    return has_data.reshape(height // patch, patch, width // patch, patch).any(axis=(1, 3))


def normalize_tokens(features: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """(D, h, w) features -> (h, w, D) float32 unit vectors; tokens without data become zero.

    In place after one contiguous copy: at the largest area (600 x 600 tokens
    of 768) every extra float32 copy is another 1.1 GB.
    """
    tokens = np.ascontiguousarray(np.moveaxis(features, 0, -1), dtype=np.float32)
    norms = np.linalg.norm(tokens, axis=-1)
    tokens /= np.maximum(norms, 1e-6)[..., None]
    tokens[~(valid & (norms > 1e-6))] = 0.0
    return tokens


def reduce_tokens(tokens: np.ndarray, valid: np.ndarray, *, dims: int, sample: int = 20000, seed: int = 0) -> tuple[np.ndarray, dict[str, Any]]:
    """Project unit tokens onto their top ``dims`` singular directions.

    Returns (h, w, k) float16 and the share of the tokens' energy that the
    subspace keeps, so the API can say how faithful the heatmap is.
    """
    height, width, depth = tokens.shape
    flat = tokens.reshape(-1, depth)
    candidates = np.flatnonzero(valid.ravel())
    if candidates.size < 2:
        raise ValueError("Too few tokens with data to build an embedding basis.")
    if candidates.size > sample:
        candidates = np.sort(np.random.default_rng(seed).choice(candidates, sample, replace=False))
    _, _, vt = np.linalg.svd(flat[candidates], full_matrices=False)
    k = int(min(dims, vt.shape[0], depth))
    basis = vt[:k]
    reduced = (flat @ basis.T).reshape(height, width, k)
    reduced[~valid] = 0.0
    kept = float(np.mean(np.sum(reduced[valid] ** 2, axis=-1)))  # tokens are unit vectors
    return reduced.astype(np.float16), {"dims": k, "energy_captured": round(min(1.0, kept), 4)}


def similarity_map(reduced: np.ndarray, valid: np.ndarray, row: int, col: int) -> np.ndarray:
    """Cosine similarity (h, w) of every token to the token at (row, col); NaN where there is no data."""
    y = reduced.astype(np.float32)
    norms = np.linalg.norm(y, axis=-1)
    seed = y[row, col]
    seed_norm = float(norms[row, col])
    if not valid[row, col] or seed_norm <= 1e-6:
        raise ValueError("The seed token has no data.")
    sim = (y @ seed) / (np.maximum(norms, 1e-6) * seed_norm)
    sim = np.clip(sim, -1.0, 1.0)
    sim[~valid | (norms <= 1e-6)] = np.nan
    return sim.astype(np.float32)


def window_slices(height: int, width: int, window: int) -> list[tuple[int, int, int, int, int, int]]:
    """(window_row, window_col, row0, row1, col0, col1) in tokens, edge windows clipped."""
    out = []
    for wr, r0 in enumerate(range(0, height, window)):
        for wc, c0 in enumerate(range(0, width, window)):
            out.append((wr, wc, r0, min(r0 + window, height), c0, min(c0 + window, width)))
    return out


def pool_windows(
    tokens: np.ndarray,
    valid: np.ndarray,
    class_map: np.ndarray,
    *,
    patch: int,
    window: int,
    min_tokens: int,
    num_classes: int,
) -> dict[str, np.ndarray]:
    """Window vectors plus where each window is and what the map says inside it.

    ``class_map`` is the exported raster at pixel resolution: 0 = no data,
    1..num_classes = classes. Windows with fewer than ``min_tokens`` tokens
    with data are left out (too little clear ground to stand for the window).
    """
    height, width, depth = tokens.shape
    vectors, index, bounds, counts, n_valid = [], [], [], [], []
    for wr, wc, r0, r1, c0, c1 in window_slices(height, width, window):
        v = valid[r0:r1, c0:c1]
        n = int(v.sum())
        if n < min_tokens:
            continue
        mean = tokens[r0:r1, c0:c1][v].mean(axis=0)
        norm = float(np.linalg.norm(mean))
        if norm <= 1e-6:
            continue
        cls = class_map[r0 * patch : r1 * patch, c0 * patch : c1 * patch]
        vectors.append(mean / norm)
        index.append((wr, wc))
        bounds.append((r0, r1, c0, c1))
        counts.append(np.bincount(cls.ravel(), minlength=num_classes + 1)[1 : num_classes + 1])
        n_valid.append(n)
    return {
        "vectors": np.asarray(vectors, np.float32).reshape(-1, depth),
        "window_index": np.asarray(index, np.int32).reshape(-1, 2),
        "token_bounds": np.asarray(bounds, np.int32).reshape(-1, 4),
        "class_counts": np.asarray(counts, np.int64).reshape(-1, num_classes),
        "valid_tokens": np.asarray(n_valid, np.int32),
    }


def window_point_id(scene_id: str | UUID, model_version: str, window_row: int, window_col: int) -> str:
    """Deterministic Qdrant id, so a retried or reprocessed run overwrites its own points."""
    return str(uuid5(NAMESPACE_URL, f"raikou:optical:{scene_id}:{model_version}:{int(window_row)}:{int(window_col)}"))


def class_shares(counts: np.ndarray, class_names: list[str]) -> dict[str, float]:
    total = int(np.sum(counts))
    if total <= 0:
        return {}
    return {name: round(100.0 * float(c) / total, 1) for name, c in zip(class_names, counts) if c > 0}


def _hex(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def colorize(values: np.ndarray, vmin: float = VMIN, vmax: float = VMAX, stops: list[tuple[float, str]] = MAGMA_STOPS) -> np.ndarray:
    """(h, w) -> (h, w, 4) RGBA on the release's colour stops (magma by default); NaN becomes transparent."""
    t = np.clip((np.nan_to_num(values, nan=vmin) - vmin) / (vmax - vmin), 0.0, 1.0)
    positions = np.array([p for p, _ in stops])
    colors = np.array([_hex(c) for _, c in stops], np.float64)
    rgba = np.empty(values.shape + (4,), np.uint8)
    for channel in range(3):
        rgba[..., channel] = np.round(np.interp(t, positions, colors[:, channel])).astype(np.uint8)
    rgba[..., 3] = np.where(np.isnan(values), 0, 255).astype(np.uint8)
    return rgba


def png_bytes(rgba: np.ndarray) -> bytes:
    from PIL import Image

    buffer = BytesIO()
    Image.fromarray(np.ascontiguousarray(rgba)).save(buffer, format="PNG", optimize=True)  # (h, w, 4) uint8 -> RGBA
    return buffer.getvalue()
