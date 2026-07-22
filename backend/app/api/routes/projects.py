"""Authenticated project and project-scoped scene endpoints."""

from __future__ import annotations

import logging
from typing import Any, Callable
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from starlette.concurrency import run_in_threadpool

from app.api.deps import CurrentUser, get_current_user, resolve_owned_project
from app.schemas.projects import ProjectCreate, ProjectRead, ProjectUpdate
from app.schemas.scenes import SceneCreate, SceneRead
from app.services.database import get_supabase

router = APIRouter()
logger = logging.getLogger(__name__)


async def _execute(operation: Callable[[], Any], unavailable_detail: str) -> Any:
    try:
        return await run_in_threadpool(operation)
    except Exception:
        logger.exception("Supabase operation failed")
        raise HTTPException(status_code=503, detail=unavailable_detail) from None


def _first_row(response: Any) -> dict[str, Any] | None:
    data = getattr(response, "data", None)
    if isinstance(data, list):
        return data[0] if data else None
    if isinstance(data, dict):
        return data
    return None


def _scene_count_from_relation(row: dict[str, Any]) -> int:
    relation = row.get("scenes")
    if isinstance(relation, list) and relation:
        count = relation[0].get("count") if isinstance(relation[0], dict) else None
        if isinstance(count, int):
            return count
    return 0


def _project_response(row: dict[str, Any], scene_count: int | None = None) -> ProjectRead:
    data = dict(row)
    resolved_count = _scene_count_from_relation(data) if scene_count is None else scene_count
    data.pop("scenes", None)
    data["scene_count"] = resolved_count
    return ProjectRead.model_validate(data)


async def _count_project_scenes(project_id: UUID | str, owner_id: str) -> int:
    response = await _execute(
        lambda: get_supabase()
        .table("scenes")
        .select("id", count="exact")
        .eq("project_id", str(project_id))
        .eq("owner_id", owner_id)
        .execute(),
        "Scene data is temporarily unavailable",
    )
    count = getattr(response, "count", None)
    if isinstance(count, int):
        return count
    data = getattr(response, "data", None)
    return len(data) if isinstance(data, list) else 0


@router.get("", response_model=list[ProjectRead])
async def list_projects(current_user: CurrentUser = Depends(get_current_user)) -> list[ProjectRead]:
    """List only projects owned by the verified user."""
    response = await _execute(
        lambda: get_supabase()
        .table("projects")
        .select("*,scenes(count)")
        .eq("owner_id", current_user.id)
        .order("created_at", desc=True)
        .execute(),
        "Project data is temporarily unavailable",
    )
    rows = getattr(response, "data", None) or []
    return [_project_response(row) for row in rows]


@router.post("", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreate,
    current_user: CurrentUser = Depends(get_current_user),
) -> ProjectRead:
    """Create a project owned by the caller; owner IDs are never client input."""
    # The database uses an empty string rather than NULL for descriptions.
    # Omitting a missing value lets its default apply.
    insert_data = payload.model_dump(exclude_none=True)
    insert_data["owner_id"] = current_user.id
    response = await _execute(
        lambda: get_supabase().table("projects").insert(insert_data).execute(),
        "Project data is temporarily unavailable",
    )
    project = _first_row(response)
    if project is None:
        raise HTTPException(status_code=503, detail="Project creation did not return a record")
    return _project_response(project, scene_count=0)


@router.get("/{project_id}", response_model=ProjectRead)
async def get_project(
    project_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> ProjectRead:
    project = await resolve_owned_project(project_id, current_user)
    scene_count = await _count_project_scenes(project_id, current_user.id)
    return _project_response(project, scene_count=scene_count)


@router.patch("/{project_id}", response_model=ProjectRead)
async def update_project(
    project_id: UUID,
    payload: ProjectUpdate,
    current_user: CurrentUser = Depends(get_current_user),
) -> ProjectRead:
    await resolve_owned_project(project_id, current_user)
    update_data = payload.model_dump(exclude_unset=True)
    if "description" in update_data and update_data["description"] is None:
        update_data["description"] = ""
    response = await _execute(
        lambda: get_supabase()
        .table("projects")
        .update(update_data)
        .eq("id", str(project_id))
        .eq("owner_id", current_user.id)
        .execute(),
        "Project data is temporarily unavailable",
    )
    project = _first_row(response)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    scene_count = await _count_project_scenes(project_id, current_user.id)
    return _project_response(project, scene_count=scene_count)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> Response:
    await resolve_owned_project(project_id, current_user)
    response = await _execute(
        lambda: get_supabase()
        .rpc(
            "delete_project_if_idle",
            {"p_owner_id": current_user.id, "p_project_id": str(project_id)},
        )
        .execute(),
        "Project data is temporarily unavailable",
    )
    result = _first_row(response)
    if result is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if not result.get("deleted"):
        reason = result.get("reason")
        detail_by_reason = {
            "upload_in_progress": "This project has an active upload and cannot be deleted.",
            "upload_cleanup_pending": "This project has temporary upload storage awaiting cleanup and cannot be deleted yet.",
            "job_in_progress": "This project has an active processing job and cannot be deleted.",
            "artifacts_require_cleanup": "This project has durable object-storage artifacts and cannot be deleted until cleanup is available.",
        }
        detail = detail_by_reason.get(reason, "This project cannot be deleted in its current state.")
        raise HTTPException(status_code=409, detail=detail)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{project_id}/scenes", response_model=list[SceneRead])
async def list_project_scenes(
    project_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> list[SceneRead]:
    await resolve_owned_project(project_id, current_user)
    response = await _execute(
        lambda: get_supabase()
        .table("scenes")
        .select("*")
        .eq("project_id", str(project_id))
        .eq("owner_id", current_user.id)
        .order("created_at", desc=True)
        .execute(),
        "Scene data is temporarily unavailable",
    )
    rows = getattr(response, "data", None) or []
    return [SceneRead.model_validate(row) for row in rows]


@router.post(
    "/{project_id}/scenes",
    response_model=SceneRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_project_scene(
    project_id: UUID,
    payload: SceneCreate,
    current_user: CurrentUser = Depends(get_current_user),
) -> SceneRead:
    await resolve_owned_project(project_id, current_user)
    insert_data = payload.model_dump(mode="json")
    insert_data.update(
        {
            "owner_id": current_user.id,
            "project_id": str(project_id),
            "status": "draft",
        }
    )
    response = await _execute(
        lambda: get_supabase().table("scenes").insert(insert_data).execute(),
        "Scene data is temporarily unavailable",
    )
    scene = _first_row(response)
    if scene is None:
        raise HTTPException(status_code=503, detail="Scene creation did not return a record")
    return SceneRead.model_validate(scene)
