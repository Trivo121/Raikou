"""Request/response models for provider-fetched scene sources.

No provider credential, token, or upstream URL appears in any model here: the
browser learns only that the feature is available and what products matched.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AcquisitionProvider(str, Enum):
    COPERNICUS = "copernicus"


class AcquisitionMode(str, Enum):
    """What shape of data to fetch for the drawn area.

    ``aoi_subset`` renders just the box, orthorectified and already calibrated.
    ``full_frame`` downloads the whole SAFE product the box falls inside.
    """

    AOI_SUBSET = "aoi_subset"
    FULL_FRAME = "full_frame"


class BoundingBoxModel(BaseModel):
    """The area to render, as four validated floats.

    Same shape as the search box and validated the same way, but kept separate
    on purpose: the search box is where a user looked, this is what they want
    cut out, and conflating them would let a pan between the two silently move
    the delivered area.
    """

    model_config = ConfigDict(extra="forbid")

    west: float = Field(ge=-180.0, le=180.0)
    south: float = Field(ge=-90.0, le=90.0)
    east: float = Field(ge=-180.0, le=180.0)
    north: float = Field(ge=-90.0, le=90.0)

    @model_validator(mode="after")
    def ordered(self) -> "BoundingBoxModel":
        if self.west >= self.east:
            raise ValueError("west must be less than east; an antimeridian box is not supported.")
        if self.south >= self.north:
            raise ValueError("south must be less than north.")
        return self


class SceneAcquisitionStatus(str, Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AcquisitionProviderRead(BaseModel):
    """Feature availability plus the limits the UI must enforce locally."""

    enabled: bool
    # 'not_configured' | 'schema_not_applied' | 'disabled'; null when enabled.
    reason: str | None = None
    max_results: int
    max_search_days: int
    max_aoi_sq_km: float
    # Stated so the UI can be explicit that the AOI selects a scene rather
    # than clipping one. Sentinel-1 GRD ships as whole ~250x170 km frames.
    product_type: str
    polarisation_channels: str
    aoi_is_crop: bool = False


class AcquisitionProvidersRead(BaseModel):
    copernicus: AcquisitionProviderRead


class AcquisitionSearchRequest(BaseModel):
    """A bbox, not a free polygon.

    This is the endpoint's key security property: four validated floats are
    formatted into the upstream filter, so no user-supplied string ever enters
    the filter expression, and the vertex count is four by construction.
    """

    model_config = ConfigDict(extra="forbid")

    west: float = Field(ge=-180.0, le=180.0)
    south: float = Field(ge=-90.0, le=90.0)
    east: float = Field(ge=-180.0, le=180.0)
    north: float = Field(ge=-90.0, le=90.0)
    start_date: date
    end_date: date
    limit: int | None = Field(default=None, ge=1, le=200)


class AcquisitionProductRead(BaseModel):
    """One catalogue result.

    ``online is False`` means the product sits in the long-term archive; it is
    returned so the user can see the scene exists, and rendered unselectable.
    """

    product_id: str
    name: str
    product_type: str
    polarisation_channels: str
    sensing_start: datetime | None = None
    online: bool
    size_bytes: int | None = None
    footprint: dict[str, Any] | None = None


class AcquisitionSearchResponse(BaseModel):
    items: list[AcquisitionProductRead] = Field(default_factory=list)
    # True when the frame covers the drawn area rather than matching it.
    aoi_is_crop: bool = False


class AcquisitionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scene_id: UUID
    product_id: str = Field(min_length=1, max_length=128)
    # Echoed back for a stale-UI check; the server trusts only what it
    # re-fetches from the provider.
    product_name: str = Field(min_length=1, max_length=512)
    client_request_id: str | None = Field(default=None, min_length=8, max_length=128)
    # Subset by default: it is what almost everyone wants, and it is 40x
    # smaller and map-projected. A whole frame stays available for work that
    # needs the full swath or the ~33k patches it tiles into.
    mode: AcquisitionMode = AcquisitionMode.AOI_SUBSET
    # Required for a subset and ignored otherwise. Not defaulted to the search
    # box: the user may have panned since, and silently fetching somewhere
    # other than what is on screen is worse than refusing.
    aoi: BoundingBoxModel | None = None

    @model_validator(mode="after")
    def subset_needs_its_box(self) -> "AcquisitionCreateRequest":
        if self.mode is AcquisitionMode.AOI_SUBSET and self.aoi is None:
            raise ValueError("An area-of-interest subset needs the area to render.")
        return self


class SceneAcquisitionRead(BaseModel):
    model_config = ConfigDict(extra="ignore", from_attributes=True)

    id: UUID
    owner_id: UUID
    project_id: UUID
    scene_id: UUID
    provider: AcquisitionProvider
    status: SceneAcquisitionStatus
    product_id: str
    product_name: str
    product_type: str
    polarisation_channels: str
    sensing_start: datetime | None = None
    online: bool
    expected_size_bytes: int
    downloaded_size_bytes: int | None = None
    footprint: dict[str, Any] | None = None
    artifact_id: UUID | None = None
    downloaded_at: datetime | None = None
    failure_code: str | None = None
    failure_detail: str | None = None
    created_at: datetime
    updated_at: datetime


class SceneAcquisitionCreateResponse(BaseModel):
    acquisition: SceneAcquisitionRead
    processing_job_id: UUID | None = None
    # True when a retried request replayed an existing acquisition rather than
    # starting a second download.
    replayed: bool = False
