"""Read models for durable scene artifacts."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SceneArtifactKind(str, Enum):
    SOURCE_ARCHIVE = "source_archive"
    SOURCE_RASTER = "source_raster"
    METADATA = "metadata"
    VRT = "vrt"
    OVERVIEW = "overview"
    THUMBNAIL = "thumbnail"
    PATCH_PREVIEW = "patch_preview"
    EVIDENCE = "evidence"
    EMBEDDING_MANIFEST = "embedding_manifest"
    SCENE_RECORD = "scene_record"
    OTHER = "other"


class SceneArtifactStatus(str, Enum):
    PENDING = "pending"
    AVAILABLE = "available"
    FAILED = "failed"
    DELETED = "deleted"


class SceneArtifactRead(BaseModel):
    """Safe artifact metadata returned to browser clients.

    Storage bucket names and object keys are deliberately internal. A client
    that needs a renderable image must make an explicit, authorized preview
    request; it never receives a permanent object URL or storage location.
    """

    model_config = ConfigDict(extra="ignore")

    id: UUID
    owner_id: UUID
    project_id: UUID
    scene_id: UUID
    kind: SceneArtifactKind
    status: SceneArtifactStatus
    content_type: str | None = None
    size_bytes: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
