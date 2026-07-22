"""Authenticated durable processing-job read endpoints."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import (
    CurrentUser,
    get_current_user,
    resolve_owned_processing_job,
    resolve_owned_scene,
)
from app.api.routes.projects import _execute
from app.schemas.jobs import ProcessingJobEventPage, ProcessingJobEventRead, ProcessingJobRead
from app.services.cache.evidence import invalidate_project_evidence_cache
from app.services.database import get_supabase

router = APIRouter()
logger = logging.getLogger(__name__)


def _event_read(row: dict[str, object]) -> ProcessingJobEventRead:
    """Expose only a safe, bounded message from opaque worker event detail."""
    detail = row.get("detail")
    message = detail.get("message") if isinstance(detail, dict) else None
    if not isinstance(message, str):
        message = None
    elif len(message) > 500:
        message = message[:500]
    return ProcessingJobEventRead.model_validate({**row, "message": message})


async def _nudge_queued_job_dispatch(
    job: dict[str, object],
    current_user: CurrentUser,
) -> None:
    """M3 owns publication through its always-on PostgreSQL outbox dispatcher.

    This no-op is retained for a narrow compatibility window so existing
    clients can poll jobs without FastAPI becoming a queue publisher again.
    """
    return None


@router.get("/scenes/{scene_id}", response_model=list[ProcessingJobRead])
async def list_scene_jobs(
    scene_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> list[ProcessingJobRead]:
    """List a caller's durable job history for one owned scene."""
    scene = await resolve_owned_scene(scene_id, current_user)
    response = await _execute(
        lambda: get_supabase()
        .table("processing_jobs")
        .select("*")
        .eq("scene_id", str(scene_id))
        .eq("project_id", str(scene["project_id"]))
        .eq("owner_id", current_user.id)
        .order("created_at", desc=True)
        .execute(),
        "Job data is temporarily unavailable",
    )
    rows = getattr(response, "data", None) or []
    for row in rows:
        if isinstance(row, dict):
            await _nudge_queued_job_dispatch(row, current_user)

    # A nudge can exhaust the outbox and atomically mark a queued job failed,
    # so re-read before returning a list the client may cache.
    refreshed = await _execute(
        lambda: get_supabase()
        .table("processing_jobs")
        .select("*")
        .eq("scene_id", str(scene_id))
        .eq("project_id", str(scene["project_id"]))
        .eq("owner_id", current_user.id)
        .order("created_at", desc=True)
        .execute(),
        "Job data is temporarily unavailable",
    )
    return [ProcessingJobRead.model_validate(row) for row in (getattr(refreshed, "data", None) or [])]


@router.get("/{job_id}", response_model=ProcessingJobRead)
async def get_processing_job(
    job_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> ProcessingJobRead:
    """Return a job after resolving ownership before exposing its status."""
    job = await resolve_owned_processing_job(job_id, current_user)
    await _nudge_queued_job_dispatch(job, current_user)
    refreshed = await resolve_owned_processing_job(job_id, current_user)
    return ProcessingJobRead.model_validate(refreshed)


@router.get("/{job_id}/events", response_model=ProcessingJobEventPage)
async def list_processing_job_events(
    job_id: UUID,
    before_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=100),
    current_user: CurrentUser = Depends(get_current_user),
) -> ProcessingJobEventPage:
    """Return an owned job's durable timeline without leaking worker detail."""
    job = await resolve_owned_processing_job(job_id, current_user)

    def operation():
        query = (
            get_supabase()
            .table("processing_job_events")
            .select("*")
            .eq("processing_job_id", str(job_id))
            .eq("scene_id", str(job["scene_id"]))
            .eq("project_id", str(job["project_id"]))
            .eq("owner_id", current_user.id)
            .order("id", desc=True)
            .limit(limit + 1)
        )
        if before_id is not None:
            query = query.lt("id", before_id)
        return query.execute()

    response = await _execute(operation, "Job history is temporarily unavailable")
    rows = [row for row in (getattr(response, "data", None) or []) if isinstance(row, dict)]
    has_more = len(rows) > limit
    visible = rows[:limit]
    return ProcessingJobEventPage(
        items=[_event_read(row) for row in visible],
        next_before_id=int(visible[-1]["id"]) if has_more and visible else None,
    )


@router.post("/{job_id}/cancel", response_model=ProcessingJobRead, status_code=status.HTTP_202_ACCEPTED)
async def cancel_processing_job(
    job_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> ProcessingJobRead:
    """Durably request cancellation; M3 workers perform scoped cleanup."""
    job = await resolve_owned_processing_job(job_id, current_user)
    response = await _execute(
        lambda: get_supabase()
        .rpc("m3_request_job_cancellation", {"p_owner_id": current_user.id, "p_job_id": str(job_id)})
        .execute(),
        "Job cancellation is temporarily unavailable",
    )
    result = getattr(response, "data", None)
    if result is None:
        raise HTTPException(status_code=404, detail="Processing job not found")
    # A cancellation immediately makes the scene's prior retrieval context
    # stale even though the worker performs the durable cleanup afterward.
    await invalidate_project_evidence_cache(
        owner_id=current_user.id,
        project_id=str(job["project_id"]),
    )
    refreshed = await resolve_owned_processing_job(job_id, current_user)
    return ProcessingJobRead.model_validate(refreshed)
