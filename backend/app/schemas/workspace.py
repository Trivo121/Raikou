"""M4 browser-safe read models for the authenticated project workspace."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.artifacts import SceneArtifactRead
from app.schemas.jobs import ProcessingJobRead
from app.schemas.projects import ProjectRead
from app.schemas.scenes import SceneRead


class EvidenceAvailability(str, Enum):
    MISSING = "missing"
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"
    SUPERSEDED = "superseded"
    UNAVAILABLE = "unavailable"


class EvidenceKind(str, Enum):
    METADATA = "metadata"
    LAND_WATER_ESTIMATE = "land_water_estimate"
    MODEL_OBSERVATION = "model_observation"
    VALIDATED_DETECTOR_EVIDENCE = "validated_detector_evidence"


class EvidenceSourceRead(BaseModel):
    """A source opened only through an authorized artifact-preview request."""

    scene_id: UUID
    artifact_id: UUID | None = None
    patch_id: UUID | None = None
    bounds: dict[str, int] | None = None


class EvidenceSectionRead(BaseModel):
    kind: EvidenceKind
    title: str
    values: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)
    source: EvidenceSourceRead | None = None


class SceneEvidenceRecordRead(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: UUID
    scene_id: UUID
    status: EvidenceAvailability
    record_version: int
    model_name: str | None = None
    model_version: str | None = None
    generated_at: datetime
    sections: list[EvidenceSectionRead] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class SceneEvidenceResponse(BaseModel):
    scene_id: UUID
    status: EvidenceAvailability
    record: SceneEvidenceRecordRead | None = None


class PatchBoundsRead(BaseModel):
    row_start: int
    row_end: int
    col_start: int
    col_end: int


class SceneWorkspaceItem(BaseModel):
    scene: SceneRead
    active_job: ProcessingJobRead | None = None
    latest_job: ProcessingJobRead | None = None
    overview: SceneArtifactRead | None = None
    evidence_status: EvidenceAvailability = EvidenceAvailability.MISSING


class ScenePatchSummary(BaseModel):
    id: UUID
    status: str
    bounds: PatchBoundsRead
    patch_size: int
    model_name: str | None = None
    model_version: str | None = None
    preview_artifact: SceneArtifactRead | None = None


class ProjectLifecycleCounts(BaseModel):
    total: int = 0
    draft: int = 0
    uploading: int = 0
    queued: int = 0
    processing: int = 0
    ready: int = 0
    failed: int = 0
    cancelled: int = 0
    deleting: int = 0
    archived: int = 0


class ProjectWorkspaceRead(BaseModel):
    project: ProjectRead
    counts: ProjectLifecycleCounts
    scenes: list[SceneWorkspaceItem] = Field(default_factory=list)


class SceneWorkspaceDetail(BaseModel):
    scene: SceneRead
    active_job: ProcessingJobRead | None = None
    latest_job: ProcessingJobRead | None = None
    artifacts: list[SceneArtifactRead] = Field(default_factory=list)
    overview: SceneArtifactRead | None = None
    evidence_status: EvidenceAvailability = EvidenceAvailability.MISSING
    evidence_record_id: UUID | None = None
    patch_count: int = 0
    preview_patch_count: int = 0
    patches: list[ScenePatchSummary] = Field(default_factory=list)


class ArtifactPreviewGrant(BaseModel):
    artifact_id: UUID
    url: str
    expires_at: datetime
    content_type: str


class PatchDetailRead(BaseModel):
    id: UUID
    project_id: UUID
    scene_id: UUID
    status: str
    bounds: PatchBoundsRead
    patch_size: int
    quality: dict[str, Any] = Field(default_factory=dict)
    model_name: str | None = None
    model_version: str | None = None
    source_artifact_id: UUID | None = None
    preview_artifact: SceneArtifactRead | None = None
    created_at: datetime
    updated_at: datetime
