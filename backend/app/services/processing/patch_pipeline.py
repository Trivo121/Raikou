import uuid
import logging
from dataclasses import dataclass, field
from typing import Iterator, Optional
import base64
import os
import tempfile
import io
from PIL import Image

import numpy as np
import rasterio
from rasterio.windows import Window

logger = logging.getLogger("patch_pipeline")

# --------------------------------------------------------------------------
# Config — Feature 3
# --------------------------------------------------------------------------

PATCH_SIZE = 224          # ViT-L-14 input size; no resize step, cut directly.
STRIDE = 112              # 50% overlap.
NODATA_DISCARD_FRACTION = 0.40   # >40% zero pixels -> discard.

# Config — Feature 2 dB clipping range.
DB_FLOOR = -25.0
DB_CEILING = 5.0


# --------------------------------------------------------------------------
# Output contract
# --------------------------------------------------------------------------

@dataclass
class ProcessedPatch:
    patch_id: str
    array: np.ndarray            # 224x224x3 uint8, ready for SARCLIP
    row_start: int                # pixel coords in parent scene
    col_start: int
    session_id: str
    scene_metadata: dict          # passthrough from Feature 1, unmodified


@dataclass
class PatchPlan:
    """What Feature 3 calculates before starting, so the user can be
    told the expected patch count up front."""
    steps_x: int
    steps_y: int
    estimated_total_patches: int
    patch_size: int = PATCH_SIZE
    stride: int = STRIDE


class ShapeMismatchError(Exception):
    """Raised if a processed patch is not 224x224x3 before hand-off."""


# --------------------------------------------------------------------------
# Planning — "what to calculate before starting"
# --------------------------------------------------------------------------

def plan_patches(width: int, height: int) -> PatchPlan:
    steps_x = max(0, (width - PATCH_SIZE) // STRIDE + 1) if width >= PATCH_SIZE else 0
    steps_y = max(0, (height - PATCH_SIZE) // STRIDE + 1) if height >= PATCH_SIZE else 0
    return PatchPlan(
        steps_x=steps_x,
        steps_y=steps_y,
        estimated_total_patches=steps_x * steps_y,
    )


def _window_origins(width: int, height: int) -> Iterator[tuple[int, int]]:
    """
    Yields (row_start, col_start) for every candidate window, discarding
    incomplete edge patches rather than padding them.
    """
    plan = plan_patches(width, height)
    for step_y in range(plan.steps_y):
        row_start = step_y * STRIDE
        if row_start + PATCH_SIZE > height:
            continue  # incomplete edge patch — discard, don't pad
        for step_x in range(plan.steps_x):
            col_start = step_x * STRIDE
            if col_start + PATCH_SIZE > width:
                continue
            yield row_start, col_start


# --------------------------------------------------------------------------
# Feature 2 — Preprocessing chain, applied to one raw window at a time
# --------------------------------------------------------------------------

def _is_mostly_nodata(raw_patch: np.ndarray) -> bool:
    """Step 1 — no-data filtering. Checked before any arithmetic."""
    zero_fraction = float(np.count_nonzero(raw_patch == 0)) / raw_patch.size
    return zero_fraction > NODATA_DISCARD_FRACTION


def _to_db(raw_band: np.ndarray) -> np.ndarray:
    """Step 2 — dB conversion: 10 * log10(value + 1)."""
    return 10.0 * np.log10(raw_band.astype(np.float64) + 1.0)


def _normalize_to_uint8(db_band: np.ndarray) -> np.ndarray:
    """Step 3 — clip to [-25, +5] dB, then linearly scale to 0-255 uint8."""
    clipped = np.clip(db_band, DB_FLOOR, DB_CEILING)
    scaled = (clipped - DB_FLOOR) / (DB_CEILING - DB_FLOOR) * 255.0
    return scaled.astype(np.uint8)

def _build_channels(raw_patch: np.ndarray) -> np.ndarray:
    """
    Step 4 — channel replication / polarimetric combination.

    raw_patch shape is (bands, H, W) as returned by rasterio.

    - 1 band  -> replicate single dB-normalized channel x3.
    - 2 bands -> VV, VH, VV-VH combination (computed in dB space, then
                 each channel independently normalized to 0-255).
    """
    band_count = raw_patch.shape[0]

    if band_count == 1:
        db = _to_db(raw_patch[0])
        norm = _normalize_to_uint8(db)
        return np.stack([norm, norm, norm], axis=-1)

    if band_count == 2:
        vv_db = _to_db(raw_patch[0])
        vh_db = _to_db(raw_patch[1])
        ratio_db = vv_db - vh_db  # meaningful polarimetric contrast

        vv_norm = _normalize_to_uint8(vv_db)
        vh_norm = _normalize_to_uint8(vh_db)
        # VV-VH difference has a different natural range than single-band
        # backscatter; clip/scale it against the same DB span for
        # consistency rather than inventing a separate range for MVP.
        ratio_norm = _normalize_to_uint8(ratio_db)

        return np.stack([vv_norm, vh_norm, ratio_norm], axis=-1)

    raise ShapeMismatchError(
        f"Expected 1 or 2 bands per Feature 1's contract, got {band_count}."
    )


def preprocess_patch(raw_patch: np.ndarray) -> Optional[np.ndarray]:
    """
    Full Feature 2 chain for one raw window. Returns a 224x224x3 uint8
    array, or None if the patch was discarded for no-data.
    """
    if _is_mostly_nodata(raw_patch):
        return None

    rgb = _build_channels(raw_patch)

    # Step 5 — resize confirmation. Should already be exact since we cut
    # at PATCH_SIZE directly; this is a hard guard, not a fallback resize.
    if rgb.shape != (PATCH_SIZE, PATCH_SIZE, 3):
        raise ShapeMismatchError(
            f"Processed patch has shape {rgb.shape}, expected "
            f"({PATCH_SIZE}, {PATCH_SIZE}, 3). Something went wrong in "
            f"the windowing step."
        )

    return rgb


# --------------------------------------------------------------------------
# Feature 3 — Windowed extraction, fused with Feature 2 per window
# --------------------------------------------------------------------------

def extract_and_preprocess_patches(
    file_path: str,
    session_id: str,
    scene_metadata: dict,
) -> Iterator[ProcessedPatch]:
    """
    The single fused loop: read one window from disk -> preprocess it
    immediately -> yield it -> read the next window. Raw patches are
    never accumulated; only one window's worth of raw pixels is ever
    in memory at a time.
    """
    with rasterio.open(file_path) as dataset:
        width, height = dataset.width, dataset.height

        for row_start, col_start in _window_origins(width, height):
            window = Window(col_start, row_start, PATCH_SIZE, PATCH_SIZE)

            # Windowed read — only these pixels enter RAM, for all bands.
            raw_patch = dataset.read(window=window)  # shape (bands, H, W)

            processed = preprocess_patch(raw_patch)
            if processed is None:
                continue  # discarded for no-data, move to next window

            yield ProcessedPatch(
                patch_id=uuid.uuid4().hex,
                array=processed,
                row_start=row_start,
                col_start=col_start,
                session_id=session_id,
                scene_metadata=scene_metadata,
            )


# --------------------------------------------------------------------------
# Convenience: expected patch count for the "tell the user upfront" UX
# --------------------------------------------------------------------------

def estimate_patch_count(width: int, height: int) -> PatchPlan:
    """Thin wrapper kept separate from plan_patches for API clarity —
    call this from the ingestion response / a progress endpoint before
    encoding starts, so the user knows what they're in for."""
    return plan_patches(width, height)


# --------------------------------------------------------------------------
# Multi-Modal Orchestrator Helpers
# --------------------------------------------------------------------------

def get_base64_patches(session_id: str, coordinates: list[tuple[int, int]]) -> list[str]:
    """
    Takes top-K patch coordinates (row, col), reads them from stacked.vrt, and returns
    base64 encoded JPEG strings in memory for the VLM.
    """
    temp_dir = tempfile.gettempdir()
    session_dir = os.path.join(temp_dir, f"raikou_session_{session_id}")
    vrt_path = os.path.join(session_dir, "stacked.vrt")
    
    if not os.path.exists(vrt_path):
        logger.error(f"VRT file not found: {vrt_path}")
        return []

    base64_images = []
    
    try:
        with rasterio.open(vrt_path) as dataset:
            for row, col in coordinates:
                window = Window(col, row, PATCH_SIZE, PATCH_SIZE)
                raw_patch = dataset.read(window=window)
                processed = preprocess_patch(raw_patch)
                if processed is not None:
                    image = Image.fromarray(processed)
                    buf = io.BytesIO()
                    image.save(buf, format="JPEG")
                    b64_str = base64.b64encode(buf.getvalue()).decode('utf-8')
                    base64_images.append(b64_str)
    except Exception as e:
        logger.error(f"Failed to extract base64 patches: {e}")
        
    return base64_images
