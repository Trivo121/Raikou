"""M5 public contracts for tenant-scoped retrieval and grounded chat."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.artifacts import SceneArtifactRead
from app.schemas.workspace import PatchBoundsRead


def _clean_query(value: str) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError("Query cannot be empty")
    return normalized


class EvidenceMetadataFilters(BaseModel):
    """Optional filters resolved against owned PostgreSQL scene metadata."""

    model_config = ConfigDict(extra="forbid")

    sensor: str | None = Field(default=None, max_length=128)
    polarization: str | None = Field(default=None, max_length=32)
    acquisition_from: datetime | None = None
    acquisition_to: datetime | None = None
    ready_only: bool = True

    @field_validator("sensor", "polarization", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object) -> object:
        if isinstance(value, str):
            value = " ".join(value.split())
            return value or None
        return value

    def normalized(self) -> dict[str, Any]:
        return {
            "sensor": self.sensor.casefold() if self.sensor else None,
            "polarization": self.polarization.upper() if self.polarization else None,
            "acquisition_from": self.acquisition_from.isoformat() if self.acquisition_from else None,
            "acquisition_to": self.acquisition_to.isoformat() if self.acquisition_to else None,
            "ready_only": self.ready_only,
        }


class EvidenceSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    scene_id: UUID | None = None
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=8, ge=1, le=25)
    filters: EvidenceMetadataFilters = Field(default_factory=EvidenceMetadataFilters)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        return _clean_query(value)


class EvidenceCitation(BaseModel):
    """A durable explanation of exactly what was given to the model/client."""

    source_type: Literal["patch", "overview", "metadata", "validated_detector_evidence", "model_observation"]
    source_id: UUID | str
    scene_id: UUID
    artifact_id: UUID | None = None
    patch_id: UUID | None = None
    bounds: PatchBoundsRead | None = None
    retrieval_score: float | None = None
    why_provided: str = Field(min_length=1, max_length=500)
    provenance: dict[str, Any] = Field(default_factory=dict)


class EvidenceSearchCard(BaseModel):
    patch_id: UUID
    scene_id: UUID
    scene_name: str
    bounds: PatchBoundsRead
    retrieval_score: float
    source_artifact_id: UUID | None = None
    preview_artifact: SceneArtifactRead | None = None
    model_name: str | None = None
    model_version: str | None = None
    citation: EvidenceCitation


class EvidenceSearchResponse(BaseModel):
    project_id: UUID
    scene_id: UUID | None = None
    query: str
    filters: EvidenceMetadataFilters
    cards: list[EvidenceSearchCard] = Field(default_factory=list)
    retrieval_state: Literal["results", "weak", "empty"]
    message: str


class ConversationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    scene_id: UUID | None = None
    title: str | None = Field(default=None, max_length=160)

    @field_validator("title", mode="before")
    @classmethod
    def normalize_title(cls, value: object) -> object:
        if isinstance(value, str):
            value = " ".join(value.split())
            return value or None
        return value


class ConversationRead(BaseModel):
    id: UUID
    project_id: UUID
    scene_id: UUID | None = None
    title: str
    status: Literal["active", "archived"]
    created_at: datetime
    updated_at: datetime


class ConversationMessageRead(BaseModel):
    id: UUID
    conversation_id: UUID
    project_id: UUID
    scene_id: UUID | None = None
    role: Literal["system", "user", "assistant"]
    content: str
    mode: str | None = None
    status: Literal["pending", "streaming", "complete", "failed", "cancelled"]
    citations: list[EvidenceCitation] = Field(default_factory=list)
    created_at: datetime


class ConversationMessagePage(BaseModel):
    items: list[ConversationMessageRead] = Field(default_factory=list)


class ChatStreamRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=1000)
    scene_id: UUID | None = None
    limit: int = Field(default=6, ge=1, le=12)
    filters: EvidenceMetadataFilters = Field(default_factory=EvidenceMetadataFilters)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        return _clean_query(value)
