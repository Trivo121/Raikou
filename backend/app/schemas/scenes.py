from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


SceneName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=160),
]


class SceneStatus(str, Enum):
    DRAFT = "draft"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    QUEUED = "queued"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DELETING = "deleting"
    ARCHIVED = "archived"


class SceneCreate(BaseModel):
    name: SceneName
    # V1 accepts metadata captured at upload time without baking a specific SAR
    # vendor schema into the public API.
    metadata: dict[str, Any] = Field(default_factory=dict)


class SceneUpdate(BaseModel):
    name: SceneName | None = None
    status: SceneStatus | None = None
    metadata: dict[str, Any] | None = None

    @model_validator(mode="after")
    def contains_a_change(self) -> "SceneUpdate":
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update.")
        return self


class SceneRead(BaseModel):
    model_config = ConfigDict(extra="ignore", from_attributes=True)

    id: UUID
    project_id: UUID
    owner_id: UUID
    name: str
    status: SceneStatus
    metadata: dict[str, Any] = Field(default_factory=dict)
    sensor: str | None = None
    acquisition_time: datetime | None = None
    polarizations: list[str] = Field(default_factory=list)
    source_artifact_id: UUID | None = None
    failure_code: str | None = None
    failure_detail: str | None = None
    created_at: datetime
    updated_at: datetime
