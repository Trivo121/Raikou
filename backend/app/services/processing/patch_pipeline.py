import uuid
import logging
from dataclasses import dataclass, field
from typing import Iterator, Optional
import base64
import os
import tempfile
import io
from PIL import Image

import json
import numpy as np
import rasterio
from rasterio.windows import Window
from rasterio.enums import Resampling

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

# Config - Phase 1 Scene Overview
OVERVIEW_TARGET_SIZE = 1024
GRID_SPLIT_THRESHOLD = 8192


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

def generate_overview(vrt_path: str, session_id: str):
    """
    Generates token-bounded low-resolution overviews for the entire scene using 
    decimated reads, preserving the exact same dB conversion logic as patches.
    """
    session_dir = os.path.dirname(vrt_path)
    metadata_path = os.path.join(session_dir, "metadata.json")
    
    try:
        with rasterio.open(vrt_path) as dataset:
            width, height = dataset.width, dataset.height
            bands = dataset.count
            
            use_grid = width > GRID_SPLIT_THRESHOLD or height > GRID_SPLIT_THRESHOLD
            
            overviews = {}
            
            if use_grid:
                half_w, half_h = width // 2, height // 2
                sections = [
                    ("NW", Window(0, 0, half_w, half_h)),
                    ("NE", Window(half_w, 0, width - half_w, half_h)),
                    ("SW", Window(0, half_h, half_w, height - half_h)),
                    ("SE", Window(half_w, half_h, width - half_w, height - half_h))
                ]
            else:
                sections = [("single", Window(0, 0, width, height))]
                
            for label, window in sections:
                # Decimated read: request subset, but force output to fixed target size.
                out_shape = (bands, OVERVIEW_TARGET_SIZE, OVERVIEW_TARGET_SIZE)
                
                raw_overview = dataset.read(
                    window=window,
                    out_shape=out_shape,
                    resampling=Resampling.average
                )
                
                # Use the exact same dB mapping as micro-patches
                rgb_overview = _build_channels(raw_patch=raw_overview)
                
                filename = f"overview_{label}.jpg"
                out_path = os.path.join(session_dir, filename)
                
                image = Image.fromarray(rgb_overview)
                image.save(out_path, format="JPEG")
                
                overviews[filename] = {
                    "type": "grid" if use_grid else "single",
                    "label": label,
                    "row_range": [window.row_off, window.row_off + window.height],
                    "col_range": [window.col_off, window.col_off + window.width]
                }
                
        # Update metadata.json safely
        metadata = {}
        if os.path.exists(metadata_path):
            with open(metadata_path, 'r') as f:
                try:
                    metadata = json.load(f)
                except Exception:
                    pass
            
        metadata["overviews"] = overviews
        
        with open(metadata_path, 'w') as f:
            json.dump(metadata, f, indent=2)
            
    except Exception as e:
        logger.error(f"Failed to generate scene overview: {e}", exc_info=True)


def get_base64_patches(session_id: str, coordinates: list[tuple[int, int]]) -> list[dict]:
    """
    Takes top-K patch coordinates (row, col), reads them from stacked.vrt, and returns
    a list of dicts with row, col, and the base64 encoded JPEG string.
    """
    temp_dir = tempfile.gettempdir()
    session_dir = os.path.join(temp_dir, f"raikou_session_{session_id}")
    vrt_path = os.path.join(session_dir, "stacked.vrt")
    
    if not os.path.exists(vrt_path):
        logger.error(f"VRT file not found: {vrt_path}")
        return []

    extracted_patches = []
    
    try:
        with rasterio.open(vrt_path) as dataset:
            for row, col in coordinates:
                try:
                    window = Window(col, row, PATCH_SIZE, PATCH_SIZE)
                    raw_patch = dataset.read(window=window)
                    processed = preprocess_patch(raw_patch)
                    if processed is not None:
                        image = Image.fromarray(processed)
                        buf = io.BytesIO()
                        image.save(buf, format="JPEG")
                        b64_str = base64.b64encode(buf.getvalue()).decode('utf-8')
                        extracted_patches.append({
                            "row": row,
                            "col": col,
                            "base64": b64_str
                        })
                except Exception as e:
                    logger.error(f"Failed to extract patch at row {row}, col {col}: {e}")
    except Exception as e:
        logger.error(f"Failed to open VRT for patch extraction: {e}")
        
    return extracted_patches


def get_spatial_label(row_start: int, col_start: int, patch_size: int, scene_width: int, scene_height: int) -> str:
    """
    Computes a plain-language spatial label for a given patch coordinate.
    Mirrors the grid-split logic of generate_overview exactly.
    """
    center_row = row_start + patch_size // 2
    center_col = col_start + patch_size // 2
    
    use_grid = scene_width > GRID_SPLIT_THRESHOLD or scene_height > GRID_SPLIT_THRESHOLD
    
    if use_grid:
        half_w, half_h = scene_width // 2, scene_height // 2
        if center_row < half_h and center_col < half_w:
            quadrant = "northwest"
        elif center_row < half_h and center_col >= half_w:
            quadrant = "northeast"
        elif center_row >= half_h and center_col < half_w:
            quadrant = "southwest"
        else:
            quadrant = "southeast"
        
        return f"{quadrant} quadrant, rows {row_start}-{row_start+patch_size}, columns {col_start}-{col_start+patch_size}"
    else:
        return f"full scene, rows {row_start}-{row_start+patch_size}, columns {col_start}-{col_start+patch_size}"
