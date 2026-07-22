"""Authenticated direct scene CRUD endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import CurrentUser, get_current_user, resolve_owned_scene
from app.api.routes.projects import _execute, _first_row
from app.schemas.artifacts import SceneArtifactRead
from app.schemas.scenes import SceneRead, SceneUpdate
from app.services.cache.evidence import invalidate_project_evidence_cache
from app.services.database import get_supabase

router = APIRouter()


@router.get("/{scene_id}", response_model=SceneRead)
async def get_scene(
    scene_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> SceneRead:
    scene = await resolve_owned_scene(scene_id, current_user)
    return SceneRead.model_validate(scene)


@router.patch("/{scene_id}", response_model=SceneRead)
async def update_scene(
    scene_id: UUID,
    payload: SceneUpdate,
    current_user: CurrentUser = Depends(get_current_user),
) -> SceneRead:
    existing_scene = await resolve_owned_scene(scene_id, current_user)
    update_data = payload.model_dump(mode="json", exclude_unset=True)
    if "status" in update_data:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Scene lifecycle is managed by durable uploads and M3 workers.",
        )
    if "metadata" in update_data and update_data["metadata"] is None:
        # The durable schema owns an object, never NULL. Treat an explicit
        # null PATCH as a request to clear metadata.
        update_data["metadata"] = {}
    response = await _execute(
        lambda: get_supabase()
        .table("scenes")
        .update(update_data)
        .eq("id", str(scene_id))
        .eq("owner_id", current_user.id)
        .execute(),
        "Scene data is temporarily unavailable",
    )
    scene = _first_row(response)
    if scene is None:
        raise HTTPException(status_code=404, detail="Scene not found")
    # Metadata may participate in M5 filters/context, so stale project-wide
    # retrieval/context values must be removed before the next search.
    await invalidate_project_evidence_cache(
        owner_id=current_user.id,
        project_id=str(existing_scene["project_id"]),
    )
    return SceneRead.model_validate(scene)


@router.delete("/{scene_id}", status_code=status.HTTP_202_ACCEPTED)
async def delete_scene(
    scene_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> dict[str, str]:
    """Request one durable M3 cleanup workflow for an owned scene."""
    scene = await resolve_owned_scene(scene_id, current_user)
    response = await _execute(
        lambda: get_supabase()
        .rpc(
            "m3_request_scene_cleanup",
            {"p_owner_id": current_user.id, "p_scene_id": str(scene_id)},
        )
        .execute(),
        "Scene data is temporarily unavailable",
    )
    result = _first_row(response)
    if result is None:
        raise HTTPException(status_code=404, detail="Scene not found")
    cleanup_job_id = result.get("cleanup_job_id")
    if not cleanup_job_id:
        raise HTTPException(status_code=409, detail="Scene cleanup could not be scheduled")
    await invalidate_project_evidence_cache(
        owner_id=current_user.id,
        project_id=str(scene["project_id"]),
    )
    return {"scene_id": str(scene_id), "cleanup_job_id": str(cleanup_job_id), "status": "deleting"}


@router.get("/{scene_id}/artifacts", response_model=list[SceneArtifactRead])
async def list_scene_artifacts(
    scene_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> list[SceneArtifactRead]:
    """List durable artifact metadata only after resolving scene ownership."""
    scene = await resolve_owned_scene(scene_id, current_user)
    response = await _execute(
        lambda: get_supabase()
        .table("scene_artifacts")
        .select("*")
        .eq("scene_id", str(scene_id))
        .eq("project_id", str(scene["project_id"]))
        .eq("owner_id", current_user.id)
        .order("created_at", desc=False)
        .execute(),
        "Artifact data is temporarily unavailable",
    )
    rows = getattr(response, "data", None) or []
    return [SceneArtifactRead.model_validate(row) for row in rows]
