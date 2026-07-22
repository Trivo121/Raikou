from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


ProjectName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=160),
]


class ProjectBase(BaseModel):
    name: ProjectName
    description: str | None = Field(default=None, max_length=5_000)


class ProjectCreate(ProjectBase):
    """Payload for creating a user-owned project."""


class ProjectUpdate(BaseModel):
    """Partial project update; ``description: null`` clears a description."""

    name: ProjectName | None = None
    description: str | None = Field(default=None, max_length=5_000)

    @model_validator(mode="after")
    def contains_a_change(self) -> "ProjectUpdate":
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update.")
        return self


class ProjectRead(ProjectBase):
    model_config = ConfigDict(extra="ignore", from_attributes=True)

    id: UUID
    owner_id: UUID
    description: str = ""
    created_at: datetime
    updated_at: datetime
    scene_count: int = 0
