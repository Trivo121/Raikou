"""Stable Qdrant payload contract for tenant-scoped SAR patch vectors."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class QdrantPatchPayload(BaseModel):
    """Metadata persisted beside every V1 SAR patch embedding.

    These identifiers are duplicated in Qdrant deliberately.  They let every
    vector search carry the same ownership boundaries as PostgreSQL, while the
    source artifact and model fields preserve evidence provenance.
    """

    model_config = ConfigDict(extra="forbid")

    owner_id: UUID
    project_id: UUID
    scene_id: UUID
    source_artifact_id: UUID

    row_start: int = Field(ge=0)
    row_end: int = Field(ge=0)
    col_start: int = Field(ge=0)
    col_end: int = Field(ge=0)
    patch_size: int = Field(gt=0)

    model_name: str = Field(min_length=1, max_length=128)
    model_version: str = Field(min_length=1, max_length=128)

    # Optional scene metadata may aid display but is not used as an authority
    # boundary.  PostgreSQL remains the canonical source of truth.
    sensor: str | None = None
    acquisition_date: str | None = None
    polarization: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_bounds(self) -> "QdrantPatchPayload":
        if self.row_end <= self.row_start or self.col_end <= self.col_start:
            raise ValueError("Patch end bounds must be greater than start bounds.")
        return self

    def as_qdrant_payload(self) -> dict[str, Any]:
        """Return JSON-compatible payload values accepted by Qdrant."""
        return self.model_dump(mode="json", exclude_none=True)


TENANT_QDRANT_PAYLOAD_FIELDS = ("owner_id", "project_id", "scene_id")


class OpticalWindowPayload(BaseModel):
    """Metadata beside every optical window vector (collection ``optical_patches``).

    Same tenant boundary as the SAR payload. The window's place on the run's
    grid, its lon/lat box and what the land-cover map says inside it travel
    with the vector, so a cross-run match can be shown without another query.
    """

    model_config = ConfigDict(extra="forbid")

    owner_id: UUID
    project_id: UUID
    scene_id: UUID

    window_row: int = Field(ge=0)
    window_col: int = Field(ge=0)
    row_start: int = Field(ge=0)
    row_end: int = Field(ge=0)
    col_start: int = Field(ge=0)
    col_end: int = Field(ge=0)
    window_px: int = Field(gt=0)
    epsg: int = Field(gt=0)
    bbox_lonlat: list[float] = Field(min_length=4, max_length=4)
    center_lonlat: list[float] = Field(min_length=2, max_length=2)
    valid_tokens: int = Field(ge=1)

    dates: list[str] = Field(default_factory=list)
    class_shares: dict[str, float] = Field(default_factory=dict)
    dominant_class: str | None = None

    model_name: str = Field(min_length=1, max_length=128)
    model_version: str = Field(min_length=1, max_length=128)
    embedding: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_bounds(self) -> "OpticalWindowPayload":
        if self.row_end <= self.row_start or self.col_end <= self.col_start:
            raise ValueError("Window end bounds must be greater than start bounds.")
        return self

    def as_qdrant_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)
