"""Request/response models for optical (Sentinel-2 land-cover) runs.

As with SAR acquisitions, the browser never sees provider credentials or
upstream URLs. The AOI is four validated floats; product ids are re-fetched
from the provider before anything durable is written.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.acquisitions import BoundingBoxModel


class OpticalRunMode(str, Enum):
    """One clear date, or up to three seasons of one hydrological year."""

    SINGLE_DATE = "single_date"
    SEASONAL_STACK = "seasonal_stack"


class OpticalSeasonRead(BaseModel):
    name: str
    start_month_day: str
    end_month_day: str


class OpticalModelRead(BaseModel):
    name: str | None = None
    version: str | None = None
    classes: list[dict[str, Any]] = Field(default_factory=list)
    single_date: dict[str, Any] | None = None
    seasonal_stack: dict[str, Any] | None = None
    reference: str | None = None
    temperature: float | None = None


class OpticalBudgetRead(BaseModel):
    user_month_cap: float
    user_used: float
    account_month_cap: float
    account_used: float


class OpticalProvidersRead(BaseModel):
    enabled: bool
    # 'disabled' | 'not_configured' | 'schema_not_applied' | 'model_unavailable'; null when enabled.
    reason: str | None = None
    max_side_km_single: float
    max_side_km_stack: float
    min_side_px: int
    default_max_cloud: float
    max_search_days: int
    max_results: int
    processing_units_per_10km_date: float
    seasons: list[OpticalSeasonRead] = Field(default_factory=list)
    budget: OpticalBudgetRead | None = None
    model: OpticalModelRead | None = None


class OpticalSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    west: float = Field(ge=-180.0, le=180.0)
    south: float = Field(ge=-90.0, le=90.0)
    east: float = Field(ge=-180.0, le=180.0)
    north: float = Field(ge=-90.0, le=90.0)
    start_date: date
    end_date: date
    max_cloud: float | None = Field(default=None, ge=0.0, le=100.0)
    limit: int | None = Field(default=None, ge=1, le=200)


class OpticalProductRead(BaseModel):
    product_id: str
    name: str
    sensing_start: datetime | None = None
    cloud_cover: float | None = None
    tile_id: str | None = None
    platform: str | None = None
    relative_orbit: int | None = None
    processing_baseline: str | None = None
    online: bool = True
    covers_aoi: bool | None = None
    footprint: dict[str, Any] | None = None


class OpticalGridRead(BaseModel):
    epsg: int
    width: int
    height: int
    width_km: float
    height_km: float
    corners_lonlat: list[list[float]]
    processing_units_per_date: float


class OpticalSearchResponse(BaseModel):
    items: list[OpticalProductRead] = Field(default_factory=list)
    # The grid each mode would use for this box, or null with the reason in notes.
    grid_single_date: OpticalGridRead | None = None
    grid_seasonal_stack: OpticalGridRead | None = None
    notes: list[str] = Field(default_factory=list)


class OpticalRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    # Omit to use (and on first use, create) the caller's own "Optical land cover" project.
    project_id: UUID | None = None
    aoi: BoundingBoxModel
    mode: OpticalRunMode = OpticalRunMode.SINGLE_DATE
    product_ids: list[str] = Field(min_length=1, max_length=3)
    client_request_id: str | None = Field(default=None, min_length=8, max_length=128)

    @model_validator(mode="after")
    def dates_match_mode(self) -> "OpticalRunCreate":
        ids = [p.strip() for p in self.product_ids]
        if len(set(ids)) != len(ids):
            raise ValueError("Pick each date only once.")
        if self.mode is OpticalRunMode.SINGLE_DATE and len(ids) != 1:
            raise ValueError("A single-date run takes exactly one product.")
        self.product_ids = ids
        return self


class OpticalRunRead(BaseModel):
    scene_id: UUID
    project_id: UUID
    name: str
    status: str
    mode: OpticalRunMode | None = None
    created_at: datetime | None = None
    dates: list[str] = Field(default_factory=list)
    grid: dict[str, Any] | None = None
    corners_lonlat: list[list[float]] | None = None
    processing_units: float | None = None
    estimated_processing_units: float | None = None
    acquisition_status: str | None = None
    job_id: UUID | None = None
    job_status: str | None = None
    job_stage: str | None = None
    job_progress: int | None = None
    failure_code: str | None = None
    failure_detail: str | None = None


class OpticalRunCreateResponse(BaseModel):
    run: OpticalRunRead
    processing_job_id: UUID | None = None
    replayed: bool = False


class OpticalSimilarScope(str, Enum):
    """``run``: a heatmap over this run. ``library``: nearest 640 m windows in the caller's other runs."""

    RUN = "run"
    LIBRARY = "library"


class OpticalSimilarRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lon: float = Field(ge=-180.0, le=180.0)
    lat: float = Field(ge=-90.0, le=90.0)
    scope: OpticalSimilarScope = OpticalSimilarScope.RUN
    limit: int | None = Field(default=None, ge=1, le=100)


class OpticalSeedRead(BaseModel):
    lon: float
    lat: float
    token_row: int
    token_col: int
    token_m: float
    window_row: int | None = None
    window_col: int | None = None


class OpticalHeatmapRead(BaseModel):
    # A PNG the size of the token grid (one pixel per 40 m token); place it on
    # the map with corners_lonlat (top-left, top-right, bottom-right, bottom-left).
    data_url: str
    width: int
    height: int
    corners_lonlat: list[list[float]]
    value_range: list[float]
    colormap: str


class OpticalSimilarAreaRead(BaseModel):
    threshold: float
    area_sqkm: float
    share_pct: float


class OpticalWindowMatchRead(BaseModel):
    scene_id: UUID
    scene_name: str | None = None
    score: float | None = None
    bbox_lonlat: list[float]
    center_lonlat: list[float]
    dates: list[str] = Field(default_factory=list)
    dominant_class: str | None = None
    class_shares: dict[str, float] = Field(default_factory=dict)


class OpticalSimilarResponse(BaseModel):
    scope: OpticalSimilarScope
    label: str
    meaning: str
    seed: OpticalSeedRead
    image: OpticalHeatmapRead | None = None
    areas: list[OpticalSimilarAreaRead] = Field(default_factory=list)
    seed_window: OpticalWindowMatchRead | None = None
    matches: list[OpticalWindowMatchRead] = Field(default_factory=list)
    basis: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class OpticalRunDetail(OpticalRunRead):
    # logical name -> artifact id, for previews (POST /artifacts/{id}/preview)
    # and downloads (POST /artifacts/{id}/download).
    artifacts: dict[str, str] = Field(default_factory=dict)
    record: dict[str, Any] | None = None
