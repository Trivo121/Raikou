"""Reusable authentication and ownership dependencies for FastAPI routes."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool
from supabase import Client

from app.services.database import get_supabase

logger = logging.getLogger(__name__)
_bearer_scheme = HTTPBearer(auto_error=False)


class CurrentUser(BaseModel):
    """The minimal verified identity available to every protected endpoint."""

    model_config = ConfigDict(frozen=True)

    id: str
    email: str | None = None
    role: str | None = None


def _unauthorized(detail: str = "Invalid or expired bearer token") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),
) -> CurrentUser:
    """Verify the supplied Supabase access token once for the current request."""
    cached = getattr(request.state, "current_user", None)
    if isinstance(cached, CurrentUser):
        return cached

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized("Missing bearer token")

    try:
        response = await run_in_threadpool(
            lambda: get_supabase().auth.get_user(credentials.credentials)
        )
        user = getattr(response, "user", None)
        user_id = getattr(user, "id", None)
        if user is None or user_id is None:
            raise ValueError("Supabase did not return a user")

        app_metadata = getattr(user, "app_metadata", None) or {}
        current_user = CurrentUser(
            id=str(user_id),
            email=getattr(user, "email", None),
            role=app_metadata.get("role") if isinstance(app_metadata, dict) else None,
        )
    except RuntimeError:
        logger.error("Supabase authentication is not configured", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is temporarily unavailable",
        ) from None
    except HTTPException:
        raise
    except Exception:
        # Do not disclose token/provider details to clients.  The exception is
        # retained in server logs for operational diagnosis.
        logger.info("Supabase token verification failed", exc_info=True)
        raise _unauthorized() from None

    request.state.current_user = current_user
    return current_user


def _first_row(response: Any) -> dict[str, Any] | None:
    data = getattr(response, "data", None)
    if isinstance(data, list):
        return data[0] if data else None
    if isinstance(data, dict):
        return data
    return None


async def resolve_owned_project(
    project_id: UUID | str,
    current_user: CurrentUser,
    *,
    supabase: Client | None = None,
) -> dict[str, Any]:
    """Resolve a project only when it belongs to the verified user."""
    try:
        client = supabase or get_supabase()
        response = await run_in_threadpool(
            lambda: client.table("projects")
            .select("*")
            .eq("id", str(project_id))
            .eq("owner_id", current_user.id)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.exception("Failed to resolve project ownership")
        raise HTTPException(status_code=503, detail="Project data is temporarily unavailable") from None

    project = _first_row(response)
    if project is None:
        # Deliberately return 404 for absent and unowned resources; this avoids
        # confirming whether another tenant owns a particular UUID.
        raise HTTPException(status_code=404, detail="Project not found")
    return project


async def resolve_owned_scene(
    scene_id: UUID | str,
    current_user: CurrentUser,
    *,
    supabase: Client | None = None,
) -> dict[str, Any]:
    """Resolve a scene only when it belongs to the verified user."""
    try:
        client = supabase or get_supabase()
        response = await run_in_threadpool(
            lambda: client.table("scenes")
            .select("*")
            .eq("id", str(scene_id))
            .eq("owner_id", current_user.id)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.exception("Failed to resolve scene ownership")
        raise HTTPException(status_code=503, detail="Scene data is temporarily unavailable") from None

    scene = _first_row(response)
    if scene is None:
        raise HTTPException(status_code=404, detail="Scene not found")
    return scene


async def resolve_owned_conversation(
    conversation_id: UUID | str,
    current_user: CurrentUser,
    *,
    supabase: Client | None = None,
) -> dict[str, Any]:
    """Resolve a conversation only when it belongs to the verified user."""
    client = supabase or get_supabase()
    try:
        response = await run_in_threadpool(
            lambda: client.table("conversations")
            .select("*")
            .eq("id", str(conversation_id))
            .eq("owner_id", current_user.id)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.exception("Failed to resolve conversation ownership")
        raise HTTPException(status_code=503, detail="Conversation data is temporarily unavailable") from None

    conversation = _first_row(response)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


async def resolve_owned_scene_in_project(
    scene_id: UUID | str,
    project_id: UUID | str,
    current_user: CurrentUser,
    *,
    supabase: Client | None = None,
) -> dict[str, Any]:
    """Resolve a scene and require that it belongs to the requested project."""
    scene = await resolve_owned_scene(scene_id, current_user, supabase=supabase)
    if str(scene.get("project_id")) != str(project_id):
        raise HTTPException(status_code=404, detail="Scene not found")
    return scene


async def resolve_owned_processing_job(
    job_id: UUID | str,
    current_user: CurrentUser,
    *,
    supabase: Client | None = None,
) -> dict[str, Any]:
    """Resolve a durable processing job only for its verified owner."""
    try:
        client = supabase or get_supabase()
        response = await run_in_threadpool(
            lambda: client.table("processing_jobs")
            .select("*")
            .eq("id", str(job_id))
            .eq("owner_id", current_user.id)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.exception("Failed to resolve processing job ownership")
        raise HTTPException(status_code=503, detail="Job data is temporarily unavailable") from None

    job = _first_row(response)
    if job is None:
        raise HTTPException(status_code=404, detail="Processing job not found")
    return job


async def resolve_owned_artifact(
    artifact_id: UUID | str,
    current_user: CurrentUser,
    *,
    supabase: Client | None = None,
) -> dict[str, Any]:
    """Resolve an artifact only within the caller's private scene scope."""
    try:
        client = supabase or get_supabase()
        response = await run_in_threadpool(
            lambda: client.table("scene_artifacts")
            .select("*")
            .eq("id", str(artifact_id))
            .eq("owner_id", current_user.id)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.exception("Failed to resolve artifact ownership")
        raise HTTPException(status_code=503, detail="Artifact data is temporarily unavailable") from None

    artifact = _first_row(response)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    # The foreign key proves the normal relationship, but resolving the scene
    # here also prevents a signed URL for a row whose parent is being removed.
    await resolve_owned_scene(artifact["scene_id"], current_user, supabase=client)
    return artifact


async def resolve_owned_patch(
    patch_id: UUID | str,
    current_user: CurrentUser,
    *,
    supabase: Client | None = None,
) -> dict[str, Any]:
    """Resolve a patch only within the caller's private scene scope."""
    try:
        client = supabase or get_supabase()
        response = await run_in_threadpool(
            lambda: client.table("patches")
            .select("*")
            .eq("id", str(patch_id))
            .eq("owner_id", current_user.id)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.exception("Failed to resolve patch ownership")
        raise HTTPException(status_code=503, detail="Patch data is temporarily unavailable") from None

    patch = _first_row(response)
    if patch is None:
        raise HTTPException(status_code=404, detail="Patch not found")
    scene = await resolve_owned_scene(patch["scene_id"], current_user, supabase=client)
    if str(scene.get("project_id")) != str(patch.get("project_id")):
        # Never trust a malformed cross-scope row even when service-role
        # access bypasses database RLS.
        raise HTTPException(status_code=404, detail="Patch not found")
    return patch


async def resolve_owned_upload_plan(
    upload_plan_id: UUID | str,
    current_user: CurrentUser,
    *,
    supabase: Client | None = None,
) -> dict[str, Any]:
    """Resolve a direct-upload plan only for its verified owner."""
    try:
        client = supabase or get_supabase()
        response = await run_in_threadpool(
            lambda: client.table("upload_plans")
            .select("*")
            .eq("id", str(upload_plan_id))
            .eq("owner_id", current_user.id)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.exception("Failed to resolve upload plan ownership")
        raise HTTPException(status_code=503, detail="Upload plan data is temporarily unavailable") from None

    upload_plan = _first_row(response)
    if upload_plan is None:
        raise HTTPException(status_code=404, detail="Upload plan not found")
    return upload_plan
