"""M4 read APIs for the authenticated, server-backed project workspace."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from starlette.concurrency import run_in_threadpool

from app.api.deps import (
    CurrentUser,
    get_current_user,
    resolve_owned_artifact,
    resolve_owned_patch,
    resolve_owned_project,
    resolve_owned_scene,
)
from app.api.routes.projects import _execute, _first_row, _project_response
from app.core.config import settings
from app.schemas.artifacts import SceneArtifactRead
from app.schemas.jobs import ProcessingJobRead
from app.schemas.scenes import SceneRead
from app.schemas.workspace import (
    ArtifactPreviewGrant,
    EvidenceAvailability,
    EvidenceKind,
    EvidenceSectionRead,
    EvidenceSourceRead,
    PatchBoundsRead,
    PatchDetailRead,
    ProjectLifecycleCounts,
    ProjectWorkspaceRead,
    SceneEvidenceRecordRead,
    SceneEvidenceResponse,
    ScenePatchSummary,
    SceneWorkspaceDetail,
    SceneWorkspaceItem,
)
from app.services.cache.evidence import invalidate_project_evidence_cache
from app.services.database import get_supabase
from app.services.storage.object_store import ObjectNotFoundError, ObjectStorageError, get_object_storage

router = APIRouter()
logger = logging.getLogger(__name__)

_ACTIVE_PROCESS_STATUSES = {"queued", "validating", "processing", "running"}
_PREVIEWABLE_KINDS = {"overview", "thumbnail", "patch_preview"}
_MAX_EVIDENCE_ITEMS = 100
_MAX_LIMITATIONS = 24


def _rows(response: Any) -> list[dict[str, Any]]:
    data = getattr(response, "data", None)
    return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []


def _evidence_availability(row: dict[str, Any] | None) -> EvidenceAvailability:
    if row is None:
        return EvidenceAvailability.MISSING
    try:
        return EvidenceAvailability(str(row.get("status") or "missing"))
    except ValueError:
        return EvidenceAvailability.UNAVAILABLE


def _artifact(row: dict[str, Any] | None) -> SceneArtifactRead | None:
    return SceneArtifactRead.model_validate(row) if row is not None else None


def _job(row: dict[str, Any] | None) -> ProcessingJobRead | None:
    return ProcessingJobRead.model_validate(row) if row is not None else None


def _is_process_job(row: dict[str, Any]) -> bool:
    return str(row.get("kind") or "process_scene") == "process_scene"


def _job_maps(rows: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return latest and active process jobs per scene without N+1 reads."""
    latest: dict[str, dict[str, Any]] = {}
    active: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not _is_process_job(row):
            continue
        scene_id = str(row.get("scene_id") or "")
        if not scene_id:
            continue
        # Queries are descending by creation time, so the first row is latest.
        latest.setdefault(scene_id, row)
        if str(row.get("status") or "") in _ACTIVE_PROCESS_STATUSES:
            active.setdefault(scene_id, row)
    return latest, active


def _bounded_json(value: Any, *, depth: int = 0) -> Any:
    """Keep user-provided JSON useful without returning an unbounded blob."""
    if depth >= 4:
        return "[truncated]"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, list):
        return [_bounded_json(item, depth=depth + 1) for item in value[:50]]
    if isinstance(value, dict):
        return {
            str(key)[:100]: _bounded_json(item, depth=depth + 1)
            for key, item in list(value.items())[:50]
        }
    return str(value)[:500]


def _bounded_strings(value: Any, *, limit: int = _MAX_LIMITATIONS) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item[:500] for item in value if isinstance(item, str) and item.strip()][:limit]


def _metadata_section(scene: dict[str, Any], overview_id: UUID | None) -> EvidenceSectionRead:
    metadata = scene.get("metadata") if isinstance(scene.get("metadata"), dict) else {}
    values: dict[str, Any] = {
        "sensor": scene.get("sensor"),
        "acquisition_time": scene.get("acquisition_time"),
        "polarizations": list(scene.get("polarizations") or []),
        "accepted_metadata": _bounded_json(metadata),
    }
    return EvidenceSectionRead(
        kind=EvidenceKind.METADATA,
        title="Acquisition and source metadata",
        values=values,
        provenance={"source": "scene metadata and derived raster metadata"},
        source=EvidenceSourceRead(scene_id=scene["id"], artifact_id=overview_id),
    )


def _safe_detector_fact(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    label = value.get("label")
    confidence = value.get("confidence")
    bbox = value.get("bounding_box_px")
    if not isinstance(label, str) or not isinstance(confidence, (int, float)) or not isinstance(bbox, dict):
        return None
    bounds = {key: bbox.get(key) for key in ("x_min", "y_min", "x_max", "y_max")}
    if not all(isinstance(item, (int, float)) for item in bounds.values()):
        return None
    return {
        "id": str(value.get("id") or "")[:100] or None,
        "label": label[:160],
        "confidence": max(0.0, min(1.0, float(confidence))),
        "bounding_box_px": bounds,
        "centroid_px": _bounded_json(value.get("centroid_px")),
        "location": _bounded_json(value.get("location")),
    }


async def _load_overview(scene: dict[str, Any], current_user: CurrentUser) -> dict[str, Any] | None:
    response = await _execute(
        lambda: get_supabase()
        .table("scene_artifacts")
        .select("*")
        .eq("scene_id", str(scene["id"]))
        .eq("project_id", str(scene["project_id"]))
        .eq("owner_id", current_user.id)
        .eq("kind", "overview")
        .eq("status", "available")
        .order("created_at", desc=True)
        .limit(1)
        .execute(),
        "Artifact data is temporarily unavailable",
    )
    return _first_row(response)


async def _current_evidence(scene: dict[str, Any], current_user: CurrentUser) -> dict[str, Any] | None:
    response = await _execute(
        lambda: get_supabase()
        .table("scene_evidence_records")
        .select("*")
        .eq("scene_id", str(scene["id"]))
        .eq("project_id", str(scene["project_id"]))
        .eq("owner_id", current_user.id)
        .eq("is_current", True)
        .order("updated_at", desc=True)
        .limit(1)
        .execute(),
        "Evidence data is temporarily unavailable",
    )
    return _first_row(response)


@router.get("/projects/{project_id}/workspace", response_model=ProjectWorkspaceRead)
async def get_project_workspace(
    project_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> ProjectWorkspaceRead:
    """Return one bounded, no-N+1 project workspace summary."""
    project = await resolve_owned_project(project_id, current_user)
    scenes_response = await _execute(
        lambda: get_supabase()
        .table("scenes")
        .select("*")
        .eq("project_id", str(project_id))
        .eq("owner_id", current_user.id)
        .order("created_at", desc=True)
        .execute(),
        "Scene data is temporarily unavailable",
    )
    scenes = _rows(scenes_response)
    scene_ids = [str(scene["id"]) for scene in scenes]
    counts = ProjectLifecycleCounts(total=len(scenes))
    for scene in scenes:
        scene_status = str(scene.get("status") or "")
        if hasattr(counts, scene_status):
            setattr(counts, scene_status, getattr(counts, scene_status) + 1)

    if not scene_ids:
        return ProjectWorkspaceRead(
            project=_project_response(project, scene_count=0), counts=counts, scenes=[]
        )

    jobs_response = await _execute(
        lambda: get_supabase()
        .table("processing_jobs")
        .select("*")
        .eq("project_id", str(project_id))
        .eq("owner_id", current_user.id)
        .in_("scene_id", scene_ids)
        .order("created_at", desc=True)
        .execute(),
        "Job data is temporarily unavailable",
    )
    overview_response = await _execute(
        lambda: get_supabase()
        .table("scene_artifacts")
        .select("*")
        .eq("project_id", str(project_id))
        .eq("owner_id", current_user.id)
        .eq("kind", "overview")
        .eq("status", "available")
        .in_("scene_id", scene_ids)
        .order("created_at", desc=True)
        .execute(),
        "Artifact data is temporarily unavailable",
    )
    evidence_response = await _execute(
        lambda: get_supabase()
        .table("scene_evidence_records")
        .select("scene_id,status,updated_at")
        .eq("project_id", str(project_id))
        .eq("owner_id", current_user.id)
        .eq("is_current", True)
        .in_("scene_id", scene_ids)
        .order("updated_at", desc=True)
        .execute(),
        "Evidence data is temporarily unavailable",
    )

    latest_jobs, active_jobs = _job_maps(_rows(jobs_response))
    overviews: dict[str, dict[str, Any]] = {}
    for artifact in _rows(overview_response):
        overviews.setdefault(str(artifact["scene_id"]), artifact)
    evidence: dict[str, dict[str, Any]] = {}
    for record in _rows(evidence_response):
        evidence.setdefault(str(record["scene_id"]), record)

    return ProjectWorkspaceRead(
        project=_project_response(project, scene_count=len(scenes)),
        counts=counts,
        scenes=[
            SceneWorkspaceItem(
                scene=SceneRead.model_validate(scene),
                active_job=_job(active_jobs.get(str(scene["id"]))),
                latest_job=_job(latest_jobs.get(str(scene["id"]))),
                overview=_artifact(overviews.get(str(scene["id"]))),
                evidence_status=_evidence_availability(evidence.get(str(scene["id"]))),
            )
            for scene in scenes
        ],
    )


@router.get("/scenes/{scene_id}/workspace", response_model=SceneWorkspaceDetail)
async def get_scene_workspace(
    scene_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> SceneWorkspaceDetail:
    """Return all durable state required by one selected scene panel."""
    scene = await resolve_owned_scene(scene_id, current_user)
    artifacts_response = await _execute(
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
    jobs_response = await _execute(
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
    patches_response = await _execute(
        lambda: get_supabase()
        .table("patches")
        .select("id,status,row_start,row_end,col_start,col_end,patch_size,model_name,model_version,preview_artifact_id")
        .eq("scene_id", str(scene_id))
        .eq("project_id", str(scene["project_id"]))
        .eq("owner_id", current_user.id)
        .neq("status", "deleted")
        .execute(),
        "Patch data is temporarily unavailable",
    )
    evidence = await _current_evidence(scene, current_user)
    artifacts = _rows(artifacts_response)
    overview = next(
        (item for item in artifacts if item.get("kind") == "overview" and item.get("status") == "available"),
        None,
    )
    latest_jobs, active_jobs = _job_maps(_rows(jobs_response))
    scene_key = str(scene_id)
    patches = _rows(patches_response)
    preview_ids = [str(patch["preview_artifact_id"]) for patch in patches if patch.get("preview_artifact_id")]
    preview_by_id: dict[str, dict[str, Any]] = {}
    if preview_ids:
        preview_response = await _execute(
            lambda: get_supabase()
            .table("scene_artifacts")
            .select("*")
            .eq("scene_id", str(scene_id))
            .eq("project_id", str(scene["project_id"]))
            .eq("owner_id", current_user.id)
            .eq("kind", "patch_preview")
            .eq("status", "available")
            .in_("id", preview_ids)
            .execute(),
            "Artifact data is temporarily unavailable",
        )
        preview_by_id = {str(item["id"]): item for item in _rows(preview_response)}
    return SceneWorkspaceDetail(
        scene=SceneRead.model_validate(scene),
        active_job=_job(active_jobs.get(scene_key)),
        latest_job=_job(latest_jobs.get(scene_key)),
        artifacts=[_artifact(item) for item in artifacts if _artifact(item) is not None],
        overview=_artifact(overview),
        evidence_status=_evidence_availability(evidence),
        evidence_record_id=evidence.get("id") if evidence else None,
        patch_count=len(patches),
        preview_patch_count=len(preview_by_id),
        patches=[
            ScenePatchSummary(
                id=patch["id"],
                status=str(patch["status"]),
                bounds=PatchBoundsRead(
                    row_start=patch["row_start"], row_end=patch["row_end"],
                    col_start=patch["col_start"], col_end=patch["col_end"],
                ),
                patch_size=patch["patch_size"],
                model_name=patch.get("model_name"),
                model_version=patch.get("model_version"),
                preview_artifact=_artifact(preview_by_id.get(str(patch.get("preview_artifact_id")))),
            )
            for patch in patches
            if str(patch.get("preview_artifact_id")) in preview_by_id
        ],
    )


@router.get("/scenes/{scene_id}/evidence-record", response_model=SceneEvidenceResponse)
async def get_scene_evidence_record(
    scene_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> SceneEvidenceResponse:
    """Return an explicit, provenance-aware evidence projection for one scene."""
    scene = await resolve_owned_scene(scene_id, current_user)
    evidence = await _current_evidence(scene, current_user)
    availability = _evidence_availability(evidence)
    if evidence is None:
        return SceneEvidenceResponse(scene_id=scene_id, status=availability)

    overview = await _load_overview(scene, current_user)
    overview_id = UUID(str(overview["id"])) if overview and overview.get("id") else None
    sections = [_metadata_section(scene, overview_id)]
    metadata = evidence.get("metadata") if isinstance(evidence.get("metadata"), dict) else {}
    record_artifact_id = metadata.get("record_artifact_id")
    if not isinstance(record_artifact_id, str):
        return SceneEvidenceResponse(scene_id=scene_id, status=EvidenceAvailability.UNAVAILABLE)

    record_response = await _execute(
        lambda: get_supabase()
        .table("scene_artifacts")
        .select("*")
        .eq("id", record_artifact_id)
        .eq("scene_id", str(scene_id))
        .eq("project_id", str(scene["project_id"]))
        .eq("owner_id", current_user.id)
        .eq("kind", "scene_record")
        .eq("status", "available")
        .limit(1)
        .execute(),
        "Evidence data is temporarily unavailable",
    )
    record_artifact = _first_row(record_response)
    if record_artifact is None:
        return SceneEvidenceResponse(scene_id=scene_id, status=EvidenceAvailability.UNAVAILABLE)
    size_bytes = record_artifact.get("size_bytes")
    if not isinstance(size_bytes, int) or size_bytes <= 0 or size_bytes > settings.M4_MAX_EVIDENCE_RECORD_BYTES:
        return SceneEvidenceResponse(scene_id=scene_id, status=EvidenceAvailability.UNAVAILABLE)

    try:
        raw = await run_in_threadpool(
            lambda: get_object_storage().read_range(str(record_artifact["storage_key"]), 0, size_bytes - 1)
        )
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ObjectStorageError, RuntimeError, ValueError):
        logger.info("Unable to read a scene evidence record", exc_info=True)
        return SceneEvidenceResponse(scene_id=scene_id, status=EvidenceAvailability.UNAVAILABLE)
    if not isinstance(record, dict):
        return SceneEvidenceResponse(scene_id=scene_id, status=EvidenceAvailability.UNAVAILABLE)

    land_water = record.get("context", {}).get("land_water") if isinstance(record.get("context"), dict) else None
    if isinstance(land_water, dict):
        values = {
            key: _bounded_json(land_water.get(key))
            for key in (
                "label", "method", "water_fraction_estimate", "land_fraction_estimate",
                "backscatter_threshold_db", "separability_score", "is_calibrated_confidence", "review_required", "reason",
            )
            if key in land_water
        }
        sections.append(EvidenceSectionRead(
            kind=EvidenceKind.LAND_WATER_ESTIMATE,
            title="Land/water context estimate",
            values=values,
            provenance={"method": "low_backscatter_otsu_heuristic", "calibrated_confidence": False},
            limitations=["This is a backscatter heuristic, not calibrated semantic-segmentation evidence."],
            source=EvidenceSourceRead(scene_id=scene["id"], artifact_id=overview_id),
        ))

    caption = record.get("model_generated_caption")
    caption_text = caption.get("text") if isinstance(caption, dict) else evidence.get("summary")
    if isinstance(caption_text, str) and caption_text.strip():
        sections.append(EvidenceSectionRead(
            kind=EvidenceKind.MODEL_OBSERVATION,
            title="Model observation",
            values={"text": caption_text[:4000], "verified_object_source": False},
            provenance={
                "model_name": caption.get("model_name") if isinstance(caption, dict) else None,
                "generated_at": caption.get("generated_at") if isinstance(caption, dict) else None,
            },
            limitations=["This generated observation is not validated detector evidence and does not create object detections."],
            source=EvidenceSourceRead(scene_id=scene["id"], artifact_id=overview_id),
        ))

    facts = [fact for item in (evidence.get("facts") or []) if (fact := _safe_detector_fact(item)) is not None]
    detector = record.get("detector") if isinstance(record.get("detector"), dict) else {}
    detector_artifact_id = metadata.get("detector_sidecar_artifact_id")
    if facts or detector.get("status"):
        sections.append(EvidenceSectionRead(
            kind=EvidenceKind.VALIDATED_DETECTOR_EVIDENCE,
            title="Validated detector evidence",
            values={"objects": facts[:_MAX_EVIDENCE_ITEMS], "object_count": len(facts)},
            provenance={
                "detector_status": _bounded_json(detector.get("status")),
                "detector_name": _bounded_json(detector.get("name")),
                "detector_version": _bounded_json(detector.get("version")),
                "confidence_semantics": _bounded_json(detector.get("confidence_semantics")),
                "sidecar_artifact_id": detector_artifact_id,
            },
            limitations=_bounded_strings(record.get("limitations")),
            source=EvidenceSourceRead(scene_id=scene["id"], artifact_id=overview_id),
        ))

    record_read = SceneEvidenceRecordRead(
        id=evidence["id"],
        scene_id=scene_id,
        status=availability,
        record_version=evidence["record_version"],
        model_name=evidence.get("model_name"),
        model_version=evidence.get("model_version"),
        generated_at=evidence.get("updated_at") or evidence.get("created_at"),
        sections=sections,
        limitations=_bounded_strings(record.get("limitations")),
    )
    return SceneEvidenceResponse(scene_id=scene_id, status=availability, record=record_read)


@router.post("/scenes/{scene_id}/reprocess", response_model=ProcessingJobRead, status_code=status.HTTP_202_ACCEPTED)
async def reprocess_scene(
    scene_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> ProcessingJobRead:
    """Create one durable M3 rebuild job from an owned scene's retained source."""
    scene = await resolve_owned_scene(scene_id, current_user)
    response = await _execute(
        lambda: get_supabase()
        .rpc("m4_request_scene_reprocess", {"p_owner_id": current_user.id, "p_scene_id": str(scene_id)})
        .execute(),
        "Scene retry is temporarily unavailable",
    )
    result = _first_row(response)
    if result is None:
        raise HTTPException(status_code=404, detail="Scene not found")
    if not result.get("accepted"):
        reason = str(result.get("reason") or "not_retryable")
        details = {
            "active_job": "This scene already has active processing.",
            "no_source": "This scene has no retained source artifact to process.",
            "deleting": "This scene is being deleted and cannot be retried.",
            "not_reprocessable": "This scene cannot be rebuilt in its current state.",
        }
        raise HTTPException(status_code=409, detail=details.get(reason, "This scene cannot be retried now."))
    job_id = result.get("job_id")
    if not job_id:
        raise HTTPException(status_code=503, detail="Scene retry did not create a durable job")
    # A retry supersedes prior patch/evidence assumptions before the worker
    # starts replacing artifacts and vectors.
    await invalidate_project_evidence_cache(
        owner_id=current_user.id,
        project_id=str(scene["project_id"]),
    )
    job_response = await _execute(
        lambda: get_supabase()
        .table("processing_jobs")
        .select("*")
        .eq("id", str(job_id))
        .eq("scene_id", str(scene_id))
        .eq("owner_id", current_user.id)
        .limit(1)
        .execute(),
        "Job data is temporarily unavailable",
    )
    job = _first_row(job_response)
    if job is None:
        raise HTTPException(status_code=503, detail="Scene retry job is temporarily unavailable")
    return ProcessingJobRead.model_validate(job)


@router.post("/artifacts/{artifact_id}/preview", response_model=ArtifactPreviewGrant)
async def create_artifact_preview(
    artifact_id: UUID,
    response: Response,
    current_user: CurrentUser = Depends(get_current_user),
) -> ArtifactPreviewGrant:
    """Issue a short-lived inline preview only after full ownership checks."""
    artifact = await resolve_owned_artifact(artifact_id, current_user)
    if artifact.get("status") != "available" or artifact.get("kind") not in _PREVIEWABLE_KINDS:
        raise HTTPException(status_code=409, detail="This artifact is not available for preview")
    content_type = str(artifact.get("content_type") or "")
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=409, detail="This artifact does not have an image preview")
    try:
        configured_bucket = settings.require_object_storage()[1]
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Object previews are temporarily unavailable") from None
    if str(artifact.get("storage_bucket")) != configured_bucket:
        logger.warning("Refusing preview for an artifact in an unexpected bucket")
        raise HTTPException(status_code=409, detail="This artifact is not available for preview")
    try:
        storage = get_object_storage()
        # Fail before issuing a grant when cleanup or an external lifecycle
        # rule removed the artifact but its row has not yet been reconciled.
        await run_in_threadpool(storage.head_object, str(artifact["storage_key"]))
        url = await run_in_threadpool(
            storage.presign_download,
            str(artifact["storage_key"]),
            settings.ARTIFACT_PREVIEW_TTL_SECONDS,
        )
    except ObjectNotFoundError:
        raise HTTPException(status_code=404, detail="Artifact preview is no longer available") from None
    except (ObjectStorageError, RuntimeError, ValueError):
        logger.info("Unable to create an artifact preview grant", exc_info=True)
        raise HTTPException(status_code=503, detail="Artifact previews are temporarily unavailable") from None
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return ArtifactPreviewGrant(
        artifact_id=artifact_id,
        url=url,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=settings.ARTIFACT_PREVIEW_TTL_SECONDS),
        content_type=content_type,
    )


@router.get("/patches/{patch_id}", response_model=PatchDetailRead)
async def get_patch_detail(
    patch_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> PatchDetailRead:
    """Return one owned patch and its safe preview metadata, never its S3 key."""
    patch = await resolve_owned_patch(patch_id, current_user)
    preview = None
    preview_artifact_id = patch.get("preview_artifact_id")
    if preview_artifact_id is not None:
        preview_response = await _execute(
            lambda: get_supabase()
            .table("scene_artifacts")
            .select("*")
            .eq("id", str(preview_artifact_id))
            .eq("scene_id", str(patch["scene_id"]))
            .eq("project_id", str(patch["project_id"]))
            .eq("owner_id", current_user.id)
            .eq("kind", "patch_preview")
            .eq("status", "available")
            .limit(1)
            .execute(),
            "Artifact data is temporarily unavailable",
        )
        preview = _first_row(preview_response)
    return PatchDetailRead(
        id=patch["id"],
        project_id=patch["project_id"],
        scene_id=patch["scene_id"],
        status=str(patch["status"]),
        bounds=PatchBoundsRead(
            row_start=patch["row_start"], row_end=patch["row_end"],
            col_start=patch["col_start"], col_end=patch["col_end"],
        ),
        patch_size=patch["patch_size"],
        quality=_bounded_json(patch.get("quality") if isinstance(patch.get("quality"), dict) else {}),
        model_name=patch.get("model_name"),
        model_version=patch.get("model_version"),
        source_artifact_id=patch.get("source_artifact_id"),
        preview_artifact=_artifact(preview),
        created_at=patch["created_at"],
        updated_at=patch["updated_at"],
    )
