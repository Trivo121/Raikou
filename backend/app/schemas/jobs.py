"""Typed durable processing-job read models for M2 polling."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ProcessingJobStatus(str, Enum):
    QUEUED = "queued"
    VALIDATING = "validating"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProcessingJobKind(str, Enum):
    PROCESS_SCENE = "process_scene"
    CLEANUP_SCENE = "cleanup_scene"


class ProcessingJobStage(str, Enum):
    VALIDATE_UPLOAD = "validate_upload"
    EXTRACT_METADATA = "extract_metadata"
    BUILD_VRT = "build_vrt"
    BUILD_OVERVIEW = "build_overview"
    TILE_PATCHES = "tile_patches"
    EMBED_PATCHES = "embed_patches"
    INDEX_VECTORS = "index_vectors"
    BUILD_EVIDENCE = "build_evidence"
    FINALIZE = "finalize"
    CLEANUP = "cleanup"


class ProcessingJobRead(BaseModel):
    model_config = ConfigDict(extra="ignore", from_attributes=True)

    id: UUID
    # Present for M2-created jobs, allowing clients to reconcile an ambiguous
    # upload completion with its exact immutable upload plan rather than a
    # historic job on the same scene.
    upload_plan_id: UUID | None = None
    owner_id: UUID
    project_id: UUID
    scene_id: UUID
    kind: ProcessingJobKind = ProcessingJobKind.PROCESS_SCENE
    stage: ProcessingJobStage
    status: ProcessingJobStatus
    progress: int = Field(ge=0, le=100)
    attempt: int = Field(ge=0)
    max_attempts: int = Field(ge=1)
    worker_job_id: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ProcessingJobEventRead(BaseModel):
    """A browser-safe projection of one durable worker event."""

    model_config = ConfigDict(extra="ignore")

    id: int
    processing_job_id: UUID
    status: ProcessingJobStatus
    stage: ProcessingJobStage
    progress: int = Field(ge=0, le=100)
    attempt: int = Field(ge=0)
    event_type: str
    error_code: str | None = None
    message: str | None = None
    created_at: datetime


class ProcessingJobEventPage(BaseModel):
    items: list[ProcessingJobEventRead] = Field(default_factory=list)
    next_before_id: int | None = None
