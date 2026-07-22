from __future__ import annotations

import time
import logging
import contextlib
from dataclasses import dataclass
from typing import Iterator, Optional, Union

import torch
from PIL import Image
import open_clip

from app.core.config import settings
from app.services.processing.patch_pipeline import ProcessedPatch, plan_patches

logger = logging.getLogger("sarclip_encoder")

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

MODEL_NAME = "ViT-L-14"
SARCLIP_CHECKPOINT_PATH = settings.SARCLIP_CHECKPOINT_PATH
DEVICE = settings.SARCLIP_DEVICE or ("cuda" if torch.cuda.is_available() else "cpu")
BATCH_SIZE = settings.SARCLIP_BATCH_SIZE

# Expected on a T4 per the design doc: 2-5 min for a full Sentinel-1 scene.
# Not enforced anywhere — just context for why progress signalling matters.


# --------------------------------------------------------------------------
# Output contracts
# --------------------------------------------------------------------------

@dataclass
class EncodedPatch:
    patch_id: str
    embedding: list[float]     # flat, L2-normalized, float32 precision
    row_start: int
    col_start: int
    session_id: str
    scene_metadata: dict


@dataclass
class ProgressUpdate:
    session_id: str
    patches_done: int
    patches_total: int
    percent: float
    throughput_patches_per_sec: float


EncodingEvent = Union[EncodedPatch, ProgressUpdate]


# --------------------------------------------------------------------------
# Sub-component 1 — Model loading (singleton, loaded once at startup)
# --------------------------------------------------------------------------

class SARCLIPEncoder:
    """
    Load exactly once, in your FastAPI startup hook / lifespan handler:

        @app.on_event("startup")
        async def _startup():
            SARCLIPEncoder.load_singleton()

    Every session then calls SARCLIPEncoder.get() to reuse the same
    loaded weights — never instantiate this per-request or per-session.
    """

    _instance: Optional["SARCLIPEncoder"] = None

    def __init__(self):
        logger.info("Loading SARCLIP (%s) onto %s ...", MODEL_NAME, DEVICE)

        # open_clip treats `pretrained` as a local checkpoint path when it
        # isn't a recognized tag name, so this loads your SARCLIP weights
        # directly and returns the matching preprocessing transform
        # (resize/crop to the model's expected input + CLIP's standard
        # normalization) in the same call.
        model, _, preprocess = open_clip.create_model_and_transforms(
            model_name=MODEL_NAME,
            pretrained=SARCLIP_CHECKPOINT_PATH,
        )

        model = model.to(DEVICE)
        model.eval()
        for param in model.parameters():
            param.requires_grad_(False)

        self.model = model
        # Built once here, reused for every patch across every session.
        self.preprocess = preprocess
        
        # Initialize tokenizer for text queries
        self.tokenizer = open_clip.get_tokenizer(MODEL_NAME)

    def encode_text(self, text: str) -> list[float]:
        """Encodes a text string into a L2-normalized 768-d embedding."""
        tokens = self.tokenizer([text]).to(DEVICE)
        
        with torch.no_grad(), _autocast_ctx():
            text_features = self.model.encode_text(tokens)
            
        text_features = text_features.float()
        norms = text_features.norm(dim=-1, keepdim=True).clamp_min(1e-12)
        text_features = (text_features / norms).cpu()
        
        return text_features[0].tolist()

    @classmethod
    def load_singleton(cls) -> "SARCLIPEncoder":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def get(cls) -> "SARCLIPEncoder":
        if cls._instance is None:
            raise RuntimeError(
                "SARCLIPEncoder not initialized. Call "
                "SARCLIPEncoder.load_singleton() at app startup before "
                "encoding anything."
            )
        return cls._instance


def _autocast_ctx():
    """FP16 autocast on CUDA (the real target — T4). On CPU (local dev
    without a GPU) we skip autocast rather than risk half-precision ops
    that aren't implemented for CPU tensors."""
    if DEVICE == "cuda":
        return torch.autocast(device_type="cuda", dtype=torch.float16)
    return contextlib.nullcontext()


# --------------------------------------------------------------------------
# Sub-component 2 — Input preparation
# --------------------------------------------------------------------------

def _prepare_tensor(patch: ProcessedPatch, preprocess) -> torch.Tensor:
    """
    Note on patch size: the doc for this feature describes patches
    arriving at 256x256 and being resized to 224x224 here. Feature 3, as
    specified and built, cuts patches directly at 224x224 and explicitly
    skips a resize step to avoid degrading backscatter texture — so the
    two feature docs disagree with each other on patch size.

    This is written to work correctly either way: OpenCLIP's preprocess
    transform resizes/center-crops to whatever the model expects
    regardless of input size, so a 224x224 input passes through as a
    no-op resize, and a 256x256 input would be resized down correctly.
    Worth reconciling the two docs upstream so it's clear which is
    intentional — right now this function is quietly papering over a
    spec mismatch rather than you having decided one way or the other.
    """
    image = Image.fromarray(patch.array)  # HWC uint8 -> PIL
    return preprocess(image)  # -> float32 CHW tensor, CLIP-normalized


# --------------------------------------------------------------------------
# Sub-component 3 — Batched encoding
# --------------------------------------------------------------------------

def _run_batch(
    encoder: SARCLIPEncoder,
    batch_patches: list[ProcessedPatch],
    batch_tensors: list[torch.Tensor],
) -> list[EncodedPatch]:
    stacked = torch.stack(batch_tensors).to(DEVICE, non_blocking=True)

    with torch.no_grad(), _autocast_ctx():
        features = encoder.model.encode_image(stacked)

    # L2 normalize to unit length — required for Qdrant cosine similarity
    # to produce correct retrieval results. Upcast to float32 first since
    # the forward pass ran in fp16.
    features = features.float()
    norms = features.norm(dim=-1, keepdim=True).clamp_min(1e-12)
    features = (features / norms).cpu()

    return [
        EncodedPatch(
            patch_id=source.patch_id,
            embedding=vector.tolist(),
            row_start=source.row_start,
            col_start=source.col_start,
            session_id=source.session_id,
            scene_metadata=source.scene_metadata,
        )
        for source, vector in zip(batch_patches, features)
    ]


# --------------------------------------------------------------------------
# Sub-component 5 — Progress signalling
# --------------------------------------------------------------------------

def _progress_event(
    session_id: str, patches_done: int, patches_total: int, start_time: float
) -> ProgressUpdate:
    elapsed = max(time.monotonic() - start_time, 1e-6)
    return ProgressUpdate(
        session_id=session_id,
        patches_done=patches_done,
        patches_total=patches_total,
        percent=min(100.0, 100.0 * patches_done / patches_total),
        throughput_patches_per_sec=patches_done / elapsed,
    )


# --------------------------------------------------------------------------
# Sub-component 4 — Output per patch, tying it all together
# --------------------------------------------------------------------------

def encode_patch_stream(
    patches: Iterator[ProcessedPatch],
    session_id: str,
    scene_width: int,
    scene_height: int,
    batch_size: int = BATCH_SIZE,
) -> Iterator[EncodingEvent]:
    """
    Pulls ProcessedPatch objects from Feature 3's stream, encodes them in
    batches, and yields a mix of EncodedPatch (for Feature 5 to upsert)
    and ProgressUpdate (for the frontend) events. Consumers should branch
    on isinstance(event, EncodedPatch) vs isinstance(event, ProgressUpdate).

    `patches_total` is recomputed from the same plan_patches() call
    Feature 3 uses to tell the user the expected count up front, so the
    denominator here matches what they were already shown. Because
    Feature 2 discards no-data patches before they ever reach this
    stream, the true encoded count can end up lower than that estimate —
    percent may not land exactly on 100 in a scene with a lot of no-data.
    That's an acceptable approximation for a progress indicator, not
    meant as exact accounting.
    """
    encoder = SARCLIPEncoder.get()
    plan = plan_patches(scene_width, scene_height)
    patches_total = max(plan.estimated_total_patches, 1)  # guard tiny/edge scenes

    patches_done = 0
    start_time = time.monotonic()
    batch_patches: list[ProcessedPatch] = []
    batch_tensors: list[torch.Tensor] = []

    for patch in patches:
        batch_patches.append(patch)
        batch_tensors.append(_prepare_tensor(patch, encoder.preprocess))

        if len(batch_tensors) < batch_size:
            continue

        yield from _run_batch(encoder, batch_patches, batch_tensors)
        patches_done += len(batch_patches)
        yield _progress_event(session_id, patches_done, patches_total, start_time)

        batch_patches, batch_tensors = [], []

    if batch_tensors:  # trailing partial batch (< batch_size patches left)
        yield from _run_batch(encoder, batch_patches, batch_tensors)
        patches_done += len(batch_patches)
        yield _progress_event(session_id, patches_done, patches_total, start_time)
