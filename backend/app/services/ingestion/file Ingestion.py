"""
file_ingestion.py

Feature 1 — Gatekeeper & Metadata Extractor.

Responsibilities (and ONLY these):
  1. Accept a single GeoTIFF upload (no batches, no zips, no SAFE dirs).
  2. Validate that it is a real, processed GRD product (not SLC/raw).
  3. Extract scene-level metadata once, so nothing downstream re-reads the file.
  4. Create a session object that Features 2+ reference by session_id.

No pixel processing happens here. No full-array reads happen here.
Everything uses rasterio header-only access + small windowed reads.
"""

from __future__ import annotations

import os
import uuid
import shutil
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import rasterio
from rasterio.windows import Window
from fastapi import APIRouter, UploadFile, File, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("file_ingestion")

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

UPLOAD_DIR = Path(os.environ.get("INGEST_UPLOAD_DIR", "./data/uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Not a hard reject — just tells the user "this will take a while".
LARGE_FILE_WARNING_MB = 500

EXPECTED_DTYPE = "uint16"

# --------------------------------------------------------------------------
# Errors — always converted to a clean human-readable message, never a raw
# traceback, before they reach the client.
# --------------------------------------------------------------------------

class GRDValidationError(Exception):
    """Raised when a file fails GRD validation. Message is user-facing."""


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

class SceneMetadata(BaseModel):
    filename: str
    bbox_lat_lon: dict = Field(
        ..., description="min_lon, min_lat, max_lon, max_lat"
    )
    width: int
    height: int
    band_count: int
    crs_epsg: Optional[int]
    pixel_resolution_deg: dict = Field(..., description="x_res, y_res")

    # Not extractable from a bare GeoTIFF — user fills these in manually.
    polarization: Optional[str] = None
    orbit_direction: Optional[str] = None
    incidence_angle_deg: Optional[float] = None


class ManualMetadataUpdate(BaseModel):
    polarization: Optional[str] = None
    orbit_direction: Optional[str] = None
    incidence_angle_deg: Optional[float] = None


class SessionData(BaseModel):
    session_id: str
    file_path: str
    created_at: datetime
    metadata: SceneMetadata
    missing_fields: list[str]


class IngestResponse(BaseModel):
    session_id: str
    metadata: SceneMetadata
    missing_fields: list[str]
    size_warning: Optional[str] = None


# --------------------------------------------------------------------------
# In-memory session store (MVP only — swap for Redis/DB later).
# Feature 10 (session cleanup) will reference this same dict.
# --------------------------------------------------------------------------

SESSIONS: dict[str, SessionData] = {}


# --------------------------------------------------------------------------
# Sub-component 2 — GRD Validation
# --------------------------------------------------------------------------

def validate_grd(dataset: "rasterio.io.DatasetReader") -> None:
    """
    Confirms the opened dataset looks like a processed GRD product.
    Raises GRDValidationError with a human-readable message on failure.

    Deliberately does NOT attempt to:
      - identify the satellite constellation
      - validate georeferencing beyond "a CRS exists"
    """
    # 1. Bit depth check — GRD is uint16. SLC is complex float32.
    dtypes = set(dataset.dtypes)
    if dtypes != {EXPECTED_DTYPE}:
        raise GRDValidationError(
            f"This file has pixel type '{', '.join(dtypes)}', but GRD data "
            f"must be 16-bit unsigned integer. This looks like SLC, raw, or "
            f"otherwise unprocessed SAR data — not GRD."
        )

    # 2. CRS check — a properly processed GRD product is georeferenced.
    if dataset.crs is None:
        raise GRDValidationError(
            "No coordinate reference system (CRS) found in this file. "
            "Processed GRD products are always georeferenced, so this file "
            "is likely not a valid GRD product."
        )

    # 3. Band check.
    if dataset.count < 1:
        raise GRDValidationError("This file has no raster bands.")

    # 4. Non-zero pixel check — sample a small window instead of the full
    #    array, so we never pull the whole scene into memory just to
    #    validate it.
    sample = _read_sample_window(dataset)
    if sample is None or not sample.any():
        raise GRDValidationError(
            "This file appears to contain only zero-value pixels in the "
            "sampled region. It may be empty, corrupted, or a no-data tile."
        )


def _read_sample_window(dataset: "rasterio.io.DatasetReader", size: int = 512):
    """Reads a small window from band 1 only — never the full array."""
    w = min(size, dataset.width)
    h = min(size, dataset.height)
    if w == 0 or h == 0:
        return None
    # Sample from the centre of the image rather than the edge, which is
    # more likely to be genuine no-data padding.
    col_off = max(0, (dataset.width - w) // 2)
    row_off = max(0, (dataset.height - h) // 2)
    window = Window(col_off, row_off, w, h)
    return dataset.read(1, window=window)


# --------------------------------------------------------------------------
# Sub-component 3 — Metadata Extraction
# --------------------------------------------------------------------------

def extract_metadata(dataset: "rasterio.io.DatasetReader", filename: str) -> SceneMetadata:
    """
    Extracts everything that CAN be read from a generic GeoTIFF.
    Fields that live only in the SAFE archive's XML manifest (polarization,
    orbit direction, incidence angle) are left None for the user to fill in.
    """
    bounds = dataset.bounds  # already in dataset CRS
    epsg = dataset.crs.to_epsg() if dataset.crs else None

    return SceneMetadata(
        filename=filename,
        bbox_lat_lon={
            "min_lon": bounds.left,
            "min_lat": bounds.bottom,
            "max_lon": bounds.right,
            "max_lat": bounds.top,
        },
        width=dataset.width,
        height=dataset.height,
        band_count=dataset.count,
        crs_epsg=epsg,
        pixel_resolution_deg={
            "x_res": abs(dataset.transform.a),
            "y_res": abs(dataset.transform.e),
        },
    )


def _missing_fields(metadata: SceneMetadata) -> list[str]:
    fields = []
    if metadata.polarization is None:
        fields.append("polarization")
    if metadata.orbit_direction is None:
        fields.append("orbit_direction")
    if metadata.incidence_angle_deg is None:
        fields.append("incidence_angle_deg")
    return fields


# --------------------------------------------------------------------------
# Sub-component 1 — File Acceptance helpers
# --------------------------------------------------------------------------

def _size_warning(file_path: Path) -> Optional[str]:
    size_mb = file_path.stat().st_size / (1024 * 1024)
    if size_mb > LARGE_FILE_WARNING_MB:
        return (
            f"This file is {size_mb:.0f} MB. Files this large can take "
            f"several minutes to process — hang tight once encoding starts."
        )
    return None


def _save_upload(upload: UploadFile) -> Path:
    session_id = uuid.uuid4().hex
    suffix = Path(upload.filename).suffix or ".tif"
    dest = UPLOAD_DIR / f"{session_id}{suffix}"
    with dest.open("wb") as out_file:
        shutil.copyfileobj(upload.file, out_file)
    return dest


# --------------------------------------------------------------------------
# Sub-component 4 — Session Initialisation
# --------------------------------------------------------------------------

def _create_session(file_path: Path, metadata: SceneMetadata) -> SessionData:
    session = SessionData(
        session_id=uuid.uuid4().hex,
        file_path=str(file_path),
        created_at=datetime.now(timezone.utc),
        metadata=metadata,
        missing_fields=_missing_fields(metadata),
    )
    SESSIONS[session.session_id] = session
    return session


# --------------------------------------------------------------------------
# FastAPI router
# --------------------------------------------------------------------------

router = APIRouter(prefix="/ingest", tags=["ingestion"])


@router.post("", response_model=IngestResponse)
async def ingest_file(file: UploadFile = File(...)) -> IngestResponse:
    """
    Feature 1 entry point.

    Accepts exactly one GeoTIFF, validates it, extracts metadata, and
    returns a session_id plus a list of fields the user needs to fill
    in manually (polarization / orbit_direction / incidence_angle_deg).
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file was uploaded.")

    if Path(file.filename).suffix.lower() not in (".tif", ".tiff"):
        raise HTTPException(
            status_code=400,
            detail="Only single GeoTIFF (.tif/.tiff) files are supported "
                   "for MVP. SAFE archives, zips, and multi-file uploads "
                   "are not accepted.",
        )

    saved_path = _save_upload(file)
    warning = _size_warning(saved_path)

    try:
        # rasterio.open only reads headers/metadata here — pixel data
        # stays on disk until Feature 3 does windowed reads.
        with rasterio.open(saved_path) as dataset:
            try:
                validate_grd(dataset)
            except GRDValidationError as e:
                saved_path.unlink(missing_ok=True)
                raise HTTPException(status_code=422, detail=str(e))

            metadata = extract_metadata(dataset, filename=file.filename)

    except rasterio.errors.RasterioIOError:
        saved_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=422,
            detail="This file could not be opened as a valid GeoTIFF. "
                   "It may be corrupted or not actually a GeoTIFF.",
        )

    session = _create_session(saved_path, metadata)

    return IngestResponse(
        session_id=session.session_id,
        metadata=session.metadata,
        missing_fields=session.missing_fields,
        size_warning=warning,
    )


@router.patch("/{session_id}/metadata", response_model=SceneMetadata)
async def update_metadata(session_id: str, update: ManualMetadataUpdate) -> SceneMetadata:
    """
    Lets the user fill in fields that can't be extracted from a bare
    GeoTIFF (polarization, orbit direction, incidence angle).
    """
    session = SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")

    update_data = update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(session.metadata, field, value)

    session.missing_fields = _missing_fields(session.metadata)
    return session.metadata


@router.get("/{session_id}", response_model=SessionData)
async def get_session(session_id: str) -> SessionData:
    session = SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    return session