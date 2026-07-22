"""M2 authenticated, direct-to-storage multipart upload endpoints."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Any, Callable, Iterable
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Response, status
from starlette.concurrency import run_in_threadpool

from app.api.deps import (
    CurrentUser,
    get_current_user,
    resolve_owned_scene_in_project,
    resolve_owned_upload_plan,
)
from app.schemas.uploads import (
    CompleteUploadFile,
    MultipartChecksumMode,
    UploadCompleteRequest,
    UploadCompleteResponse,
    UploadFileKind,
    UploadInitiateRequest,
    UploadPartInstruction,
    UploadPartSignRequest,
    UploadPartSignResponse,
    UploadPlanFileRead,
    UploadPlanRead,
    UploadPlanStatusRead,
)
from app.services.database import get_supabase
from app.services.cache.evidence import invalidate_project_evidence_cache
from app.services.storage.object_store import (
    CompletedMultipartPart,
    ObjectIntegrityError,
    ObjectStorage,
    ObjectStorageError,
    get_object_storage,
)
from app.services.uploads.validation import (
    UploadValidationError,
    ValidatedUploadFile,
    generated_upload_key,
    validate_completed_object,
    validate_upload_descriptors,
)
from app.services.jobs.publisher import publish_processing_job
from app.core.config import settings

router = APIRouter()
logger = logging.getLogger(__name__)

_ACTIVE_PLAN_STATUSES = ("initiated", "uploading", "completing")
_ACTIVE_JOB_STATUSES = ("queued", "running")
_UPLOADABLE_SCENE_STATUSES = ("draft", "failed", "cancelled")


async def _execute(operation: Callable[[], Any], unavailable_detail: str) -> Any:
    try:
        return await run_in_threadpool(operation)
    except HTTPException:
        raise
    except Exception:
        logger.exception("M2 database operation failed")
        raise HTTPException(status_code=503, detail=unavailable_detail) from None


def _first_row(response: Any) -> dict[str, Any] | None:
    data = getattr(response, "data", None)
    if isinstance(data, list):
        return data[0] if data else None
    if isinstance(data, dict):
        return data
    return None


def _rows(response: Any) -> list[dict[str, Any]]:
    data = getattr(response, "data", None)
    return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _completion_reconciliation_error(status_code: int = 503) -> HTTPException:
    """Mark only genuinely ambiguous completion outcomes for client polling."""
    return HTTPException(
        status_code=status_code,
        detail={
            "code": "completion_reconciliation_required",
            "message": "Upload completion is being reconciled. Reload the scene to check its durable job.",
        },
    )


def _parse_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _plan_file_read(row: dict[str, Any]) -> UploadPlanFileRead:
    raw_kind = row["file_kind"]
    kind = "metadata" if raw_kind == "metadata_sidecar" else raw_kind
    return UploadPlanFileRead(
        id=row["id"],
        kind=kind,
        filename=row["original_filename"],
        content_type=row["content_type"],
        size_bytes=row["expected_size_bytes"],
        part_size_bytes=row["part_size_bytes"],
        part_count=row["part_count"],
    )


def _plan_read(plan: dict[str, Any], files: Iterable[dict[str, Any]]) -> UploadPlanRead:
    file_rows = list(files)
    if not file_rows:
        raise HTTPException(status_code=503, detail="Upload plan is missing file instructions")
    first_file = file_rows[0]
    mode = first_file.get("multipart_checksum_mode")
    if any(row.get("multipart_checksum_mode") != mode for row in file_rows):
        raise HTTPException(status_code=503, detail="Upload plan has inconsistent checksum instructions")
    return UploadPlanRead(
        id=plan["id"],
        project_id=plan["project_id"],
        scene_id=plan["scene_id"],
        status=plan["status"],
        expires_at=plan["expires_at"],
        part_size_bytes=first_file["part_size_bytes"],
        multipart_checksum_mode=mode,
        files=[_plan_file_read(row) for row in sorted(file_rows, key=lambda row: row["file_number"])],
    )


def _initiation_request_fingerprint(payload: UploadInitiateRequest) -> str:
    """Return a stable digest for the semantic initiate-request payload.

    The client request UUID identifies one logical action.  This fingerprint
    makes retrying that action safe while preventing the UUID from being
    silently repurposed for another project, scene, or set of files.
    """
    canonical_payload = {
        "project_id": str(payload.project_id),
        "scene_id": str(payload.scene_id),
        "files": [
            {
                "filename": item.filename,
                "content_type": item.content_type,
                "size_bytes": item.size_bytes,
                "checksum_sha256": item.checksum_sha256,
            }
            for item in payload.files
        ],
    }
    encoded = json.dumps(
        canonical_payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def _find_owned_upload_plan_by_client_request_id(
    client_request_id: UUID,
    current_user: CurrentUser,
) -> dict[str, Any] | None:
    """Find a plan through its durable per-owner idempotency key."""
    response = await _execute(
        lambda: get_supabase()
        .table("upload_plans")
        .select("*")
        .eq("owner_id", current_user.id)
        .eq("client_request_id", str(client_request_id))
        .limit(1)
        .execute(),
        "Upload plan data is temporarily unavailable",
    )
    return _first_row(response)


async def _load_upload_plan_read(
    plan: dict[str, Any],
    current_user: CurrentUser,
) -> UploadPlanRead:
    """Load an owned plan together with its complete part instructions.

    The plan and child rows are inserted in successive statements.  A losing
    concurrent idempotency retry can therefore observe the plan in the tiny
    interval before the winning request commits its file batch.  Wait briefly
    for that batch instead of returning a malformed partial plan.
    """
    expected_file_count = int(plan.get("expected_file_count") or 0)
    for attempt in range(4):
        response = await _execute(
            lambda: get_supabase()
            .table("upload_plan_files")
            .select("*")
            .eq("upload_plan_id", str(plan["id"]))
            .eq("owner_id", current_user.id)
            .eq("project_id", str(plan["project_id"]))
            .eq("scene_id", str(plan["scene_id"]))
            .order("file_number")
            .execute(),
            "Upload plan data is temporarily unavailable",
        )
        file_rows = _rows(response)
        if expected_file_count > 0 and len(file_rows) == expected_file_count:
            return _plan_read(plan, file_rows)
        if attempt < 3:
            await asyncio.sleep(0.05)

    raise HTTPException(status_code=503, detail="Upload plan is missing file instructions")


async def _reload_if_expired_active_upload_plan(
    plan: dict[str, Any],
    current_user: CurrentUser,
) -> dict[str, Any]:
    """Persist an expiry transition before a recovery endpoint reports it."""
    if str(plan.get("status")) not in _ACTIVE_PLAN_STATUSES:
        return plan

    expires_at = _parse_timestamp(plan.get("expires_at"))
    if expires_at is None:
        raise HTTPException(status_code=503, detail="Upload plan has an invalid expiry timestamp.")
    if expires_at > _utcnow():
        return plan

    await _release_expired_upload_plan(plan, current_user)
    # The terminal transition is a compare-and-set.  Always reload: another
    # request may have completed/cancelled the plan just before the reaper.
    reloaded = await resolve_owned_upload_plan(plan["id"], current_user)
    if str(reloaded.get("status")) in _ACTIVE_PLAN_STATUSES:
        reloaded_expiry = _parse_timestamp(reloaded.get("expires_at"))
        if reloaded_expiry is None or reloaded_expiry <= _utcnow():
            raise HTTPException(
                status_code=503,
                detail="Upload plan expiry is being reconciled. Retry shortly.",
            )
    return reloaded


async def _replay_initiate_request(
    plan: dict[str, Any],
    request_fingerprint: str,
    current_user: CurrentUser,
) -> UploadPlanRead:
    """Return only an exact, still-active idempotent initiate replay."""
    stored_fingerprint = plan.get("request_fingerprint")
    if not isinstance(stored_fingerprint, str) or stored_fingerprint != request_fingerprint:
        raise HTTPException(
            status_code=409,
            detail="This client request ID was already used for a different upload request.",
        )

    refreshed_plan = await _reload_if_expired_active_upload_plan(plan, current_user)
    if str(refreshed_plan.get("status")) not in _ACTIVE_PLAN_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="This client request ID belongs to a terminal upload plan. Use a new client request ID.",
        )
    return await _load_upload_plan_read(refreshed_plan, current_user)


async def _create_upload_plan_atomically(
    plan_row: dict[str, Any],
    file_rows: list[dict[str, Any]],
    current_user: CurrentUser,
) -> dict[str, Any]:
    """Commit the plan, every file row, and scene state in one transaction.

    The browser may retry an initiate request after losing its response. The
    database must therefore never expose a plan before it has all of the file
    rows needed to sign/complete it; otherwise a retry could adopt a partial
    plan while the original request attempts destructive cleanup.
    """
    response = await _execute(
        lambda: get_supabase()
        .rpc(
            "create_upload_plan_atomically",
            {
                "p_owner_id": current_user.id,
                "p_plan": plan_row,
                "p_files": file_rows,
            },
        )
        .execute(),
        "Upload plan data is temporarily unavailable",
    )
    result = _first_row(response)
    if result is None:
        raise HTTPException(status_code=503, detail="Upload plan data is temporarily unavailable")
    return result


def _validated_file_from_row(row: dict[str, Any]) -> ValidatedUploadFile:
    raw_kind = row["file_kind"]
    return ValidatedUploadFile(
        filename=str(row["original_filename"]),
        kind=UploadFileKind("metadata" if raw_kind == "metadata_sidecar" else raw_kind),
        content_type=str(row["content_type"]),
        size_bytes=int(row["expected_size_bytes"]),
        checksum_sha256=row.get("expected_checksum_sha256"),
    )


def _part_count(size_bytes: int) -> int:
    count = ceil(size_bytes / settings.UPLOAD_MULTIPART_PART_SIZE_BYTES)
    if not 1 <= count <= 10_000:
        raise UploadValidationError("The selected file would exceed the multipart upload part limit.")
    return count


def _upload_plan_ttl_seconds(total_bytes: int) -> int:
    """Estimate a bounded whole-upload lease while keeping part URLs short."""
    transfer_seconds = ceil(total_bytes / settings.UPLOAD_PLAN_MIN_BYTES_PER_SECOND)
    # Include one minimum signing window for selection, hashing, retries, and
    # the final part; cap the lease so abandoned plans are still reclaimable.
    estimated = transfer_seconds + settings.UPLOAD_PLAN_TTL_SECONDS
    return min(
        settings.UPLOAD_PLAN_MAX_TTL_SECONDS,
        max(settings.UPLOAD_PLAN_TTL_SECONDS, estimated),
    )


def _dispatch_retry_at(attempt_count: int) -> datetime:
    """Return a bounded backoff deadline for a failed Redis publication."""
    exponent = max(0, min(16, attempt_count - 1))
    delay_seconds = min(
        settings.JOB_DISPATCH_RETRY_MAX_SECONDS,
        settings.JOB_DISPATCH_RETRY_BASE_SECONDS * (2**exponent),
    )
    return _utcnow() + timedelta(seconds=delay_seconds)


async def _get_storage() -> ObjectStorage:
    try:
        return await run_in_threadpool(get_object_storage)
    except (RuntimeError, ObjectStorageError, ValueError) as exc:
        logger.info("Object storage is unavailable for upload setup", exc_info=True)
        raise HTTPException(status_code=503, detail="Object storage is temporarily unavailable") from exc


async def _optional_storage() -> ObjectStorage | None:
    """Return storage for best-effort cleanup without blocking state recovery."""
    try:
        return await _get_storage()
    except HTTPException:
        return None


async def _abort_remote_uploads(
    storage: ObjectStorage,
    uploads: Iterable[tuple[str, str]],
) -> None:
    """Best-effort cleanup; the plan remains revoked even if this fails."""
    for storage_key, multipart_upload_id in uploads:
        try:
            await run_in_threadpool(
                storage.abort_multipart_upload,
                storage_key,
                multipart_upload_id,
            )
        except Exception:
            logger.warning("Failed to abort multipart upload during cleanup", exc_info=True)


async def _delete_completed_objects(storage: ObjectStorage, storage_keys: Iterable[str]) -> None:
    """Best-effort deletion of objects that failed database finalization."""
    for storage_key in storage_keys:
        try:
            await run_in_threadpool(storage.delete_object, storage_key)
        except Exception:
            logger.warning("Failed to delete completed upload object during cleanup", exc_info=True)


async def _transition_upload_plan_terminal(
    upload_plan_id: UUID,
    current_user: CurrentUser,
    *,
    expected_statuses: list[str],
    target_status: str,
    require_expired: bool,
    failure_code: str | None = None,
    failure_detail: str | None = None,
) -> bool:
    """CAS a terminal transition through the database's atomic state RPC."""
    response = await _execute(
        lambda: get_supabase()
        .rpc(
            "transition_upload_plan_terminal",
            {
                "p_owner_id": current_user.id,
                "p_upload_plan_id": str(upload_plan_id),
                "p_expected_statuses": expected_statuses,
                "p_target_status": target_status,
                "p_require_expired": require_expired,
                "p_failure_code": failure_code,
                "p_failure_detail": failure_detail[:500] if failure_detail else None,
            },
        )
        .execute(),
        "Upload plan data is temporarily unavailable",
    )
    return _first_row(response) is not None


async def _renew_completion_lease(
    upload_plan_id: UUID,
    current_user: CurrentUser,
) -> bool:
    """Extend a live completion lease without changing its exclusive state."""
    response = await _execute(
        lambda: get_supabase()
        .table("upload_plans")
        .update(
            {
                "expires_at": (
                    _utcnow() + timedelta(seconds=settings.UPLOAD_COMPLETION_LEASE_SECONDS)
                ).isoformat()
            }
        )
        .eq("id", str(upload_plan_id))
        .eq("owner_id", current_user.id)
        .eq("status", "completing")
        .execute(),
        "Upload completion data is temporarily unavailable",
    )
    return _first_row(response) is not None


async def _run_with_completion_lease(
    upload_plan_id: UUID,
    current_user: CurrentUser,
    operation: Callable[[], Any],
) -> Any:
    """Run long blocking verification while periodically renewing its lease."""
    task = asyncio.create_task(run_in_threadpool(operation))
    heartbeat_seconds = min(
        60.0,
        max(5.0, settings.UPLOAD_COMPLETION_LEASE_SECONDS / 3),
    )
    try:
        while not task.done():
            completed, _pending = await asyncio.wait({task}, timeout=heartbeat_seconds)
            if completed:
                break
            if not await _renew_completion_lease(upload_plan_id, current_user):
                # The state changed (usually an expiry reaper) while the
                # blocking storage call was running. Wait for it to finish so
                # no threadpool exception is leaked, then refuse DB writes.
                try:
                    await task
                except Exception:
                    pass
                raise HTTPException(
                    status_code=409,
                    detail="The upload completion lease was lost. Start a new upload.",
                )
        return task.result()
    except BaseException:
        # A database heartbeat outage must not abandon a live blocking S3
        # operation with an unobserved exception. In the ordinary error path
        # wait for it; if this request itself is cancelled, retain a callback
        # that consumes its eventual result without cancelling the thread.
        if not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                task.add_done_callback(_consume_background_task_result)
                raise
            except Exception:
                pass
        raise


def _consume_background_task_result(task: asyncio.Task[Any]) -> None:
    """Observe a detached threadpool result after request cancellation."""
    try:
        task.result()
    except BaseException:
        pass


async def _mark_upload_plan_failed(
    upload_plan_id: UUID,
    current_user: CurrentUser,
    *,
    error_code: str,
    error_detail: str,
) -> None:
    """Atomically make a failed upload visible without provider internals."""
    try:
        await _transition_upload_plan_terminal(
            upload_plan_id,
            current_user,
            expected_statuses=["initiated", "completing", "uploading"],
            target_status="failed",
            require_expired=False,
            failure_code=error_code,
            failure_detail=error_detail,
        )
    except HTTPException:
        logger.warning("Failed to record upload plan failure", exc_info=True)


async def _load_plan_storage_rows(
    upload_plan_id: UUID | str,
    current_user: CurrentUser,
) -> list[dict[str, Any]]:
    response = await _execute(
        lambda: get_supabase()
        .table("upload_plan_files")
        .select("storage_key,multipart_upload_id")
        .eq("upload_plan_id", str(upload_plan_id))
        .eq("owner_id", current_user.id)
        .execute(),
        "Upload plan data is temporarily unavailable",
    )
    return _rows(response)


async def _cleanup_terminal_plan_objects(
    upload_plan_id: UUID | str,
    current_user: CurrentUser,
    file_rows: Iterable[dict[str, Any]] | None = None,
) -> None:
    """Best-effort, retryable object cleanup for a non-completed plan."""
    rows = list(file_rows) if file_rows is not None else await _load_plan_storage_rows(
        upload_plan_id,
        current_user,
    )
    storage = await _optional_storage()
    if storage is None:
        return
    # Terminal plans never create durable artifact rows. Deleting the unique
    # plan-prefixed keys is therefore safe and makes both failed completion and
    # cancelled multipart cleanup idempotent.
    await _delete_completed_objects(storage, (str(row["storage_key"]) for row in rows))
    await _abort_remote_uploads(
        storage,
        (
            (str(row["storage_key"]), str(row["multipart_upload_id"]))
            for row in rows
        ),
    )


async def _release_expired_upload_plan(
    plan: dict[str, Any],
    current_user: CurrentUser,
) -> bool:
    """Atomically claim an expired open plan, then release its scene safely.

    ``expires_at`` is the browser signing deadline while a plan is initiated
    and becomes the longer completion lease after the completion CAS.  An
    expired completion lease is conservatively marked failed, because a lost
    request may already have completed objects but never reached the durable
    finalization transaction. Those objects are deleted only after the status
    CAS proves the plan was not finalized.
    """
    old_status = str(plan.get("status"))
    if old_status not in _ACTIVE_PLAN_STATUSES:
        return False

    failed_completion = old_status == "completing"
    next_status = "failed" if failed_completion else "expired"
    failure_code = "completion_lease_expired" if failed_completion else None
    failure_detail = (
        "Upload completion did not finish before its recovery lease expired."
        if failed_completion
        else None
    )
    # Resolve cleanup identifiers before the terminal RPC. If that read is
    # unavailable, no state changes yet and a later request can retry safely.
    file_rows = await _load_plan_storage_rows(plan["id"], current_user)
    transitioned = await _transition_upload_plan_terminal(
        UUID(str(plan["id"])),
        current_user,
        expected_statuses=[old_status],
        target_status=next_status,
        require_expired=True,
        failure_code=failure_code,
        failure_detail=failure_detail,
    )
    if not transitioned:
        return False
    await _cleanup_terminal_plan_objects(plan["id"], current_user, file_rows)
    return True


async def _reclaim_expired_upload_plans(
    scene_id: UUID,
    current_user: CurrentUser,
) -> None:
    """Release every expired open plan before a scene accepts another upload."""
    now = _utcnow()
    response = await _execute(
        lambda: get_supabase()
        .table("upload_plans")
        .select("*")
        .eq("scene_id", str(scene_id))
        .eq("owner_id", current_user.id)
        .in_("status", list(_ACTIVE_PLAN_STATUSES))
        .lte("expires_at", now.isoformat())
        .execute(),
        "Upload plan data is temporarily unavailable",
    )
    for plan in _rows(response):
        await _release_expired_upload_plan(plan, current_user)


async def _retry_terminal_plan_cleanup(
    scene_id: UUID,
    current_user: CurrentUser,
) -> None:
    """Retry cleanup of terminal plans on every safe re-upload attempt."""
    response = await _execute(
        lambda: get_supabase()
        .table("upload_plans")
        .select("id")
        .eq("scene_id", str(scene_id))
        .eq("owner_id", current_user.id)
        .in_("status", ["aborted", "expired", "failed"])
        .execute(),
        "Upload plan data is temporarily unavailable",
    )
    for plan in _rows(response):
        await _cleanup_terminal_plan_objects(plan["id"], current_user)


async def _repair_orphaned_uploading_scene(
    scene: dict[str, Any],
    current_user: CurrentUser,
) -> dict[str, Any]:
    """Repair an old partial terminal transition before rejecting a retry.

    The normal expiry path updates files, plan, and scene together from the
    API's perspective. This defensive repair covers a service outage between
    those writes and also cleans up any pre-M2 stale ``uploading`` scenes.
    """
    if scene.get("status") != "uploading":
        return scene

    now = _utcnow()
    active_response = await _execute(
        lambda: get_supabase()
        .table("upload_plans")
        .select("id")
        .eq("scene_id", str(scene["id"]))
        .eq("owner_id", current_user.id)
        .in_("status", list(_ACTIVE_PLAN_STATUSES))
        .gt("expires_at", now.isoformat())
        .limit(1)
        .execute(),
        "Upload plan data is temporarily unavailable",
    )
    if _first_row(active_response) is not None:
        return scene

    latest_response = await _execute(
        lambda: get_supabase()
        .table("upload_plans")
        .select("status,failure_code,failure_detail")
        .eq("scene_id", str(scene["id"]))
        .eq("project_id", str(scene["project_id"]))
        .eq("owner_id", current_user.id)
        .order("updated_at", desc=True)
        .limit(1)
        .execute(),
        "Upload plan data is temporarily unavailable",
    )
    latest = _first_row(latest_response)
    failed = latest is not None and latest.get("status") == "failed"
    repair = await _execute(
        lambda: get_supabase()
        .table("scenes")
        .update(
            {
                "status": "failed" if failed else "draft",
                "failure_code": latest.get("failure_code") if failed else None,
                "failure_detail": latest.get("failure_detail") if failed else None,
            }
        )
        .eq("id", str(scene["id"]))
        .eq("project_id", str(scene["project_id"]))
        .eq("owner_id", current_user.id)
        .eq("status", "uploading")
        .execute(),
        "Scene data is temporarily unavailable",
    )
    repaired_scene = _first_row(repair)
    if repaired_scene is not None:
        return repaired_scene
    # Another safe terminal transition may already have released this scene
    # between the stale read above and the repair update. Reload instead of
    # returning the old ``uploading`` snapshot and rejecting a valid retry.
    reloaded = await _execute(
        lambda: get_supabase()
        .table("scenes")
        .select("*")
        .eq("id", str(scene["id"]))
        .eq("project_id", str(scene["project_id"]))
        .eq("owner_id", current_user.id)
        .limit(1)
        .execute(),
        "Scene data is temporarily unavailable",
    )
    return _first_row(reloaded) or scene


async def _create_remote_uploads(
    storage: ObjectStorage,
    file_specs: list[dict[str, Any]],
) -> tuple[str, list[tuple[dict[str, Any], str]]]:
    """Create all provider uploads under one stable checksum-mode contract."""
    for _ in range(2):
        expected_mode = storage.multipart_checksum_mode
        created: list[tuple[dict[str, Any], str]] = []
        try:
            for spec in file_specs:
                upload_id = await run_in_threadpool(
                    storage.create_multipart_upload,
                    spec["storage_key"],
                    spec["content_type"],
                    spec["checksum_sha256"],
                    spec["metadata"],
                )
                created.append((spec, upload_id))
        except Exception as exc:
            await _abort_remote_uploads(
                storage,
                ((item["storage_key"], upload_id) for item, upload_id in created),
            )
            if isinstance(exc, (ObjectStorageError, ValueError)):
                raise HTTPException(status_code=503, detail="Object storage could not create the upload plan") from exc
            raise

        # An auto-detected MinIO fallback can change the adapter from native
        # per-part checksums to server verification during creation. Abort and
        # recreate everything once so every file has one unambiguous contract.
        if storage.multipart_checksum_mode == expected_mode:
            return expected_mode, created
        await _abort_remote_uploads(
            storage,
            ((item["storage_key"], upload_id) for item, upload_id in created),
        )

    raise HTTPException(
        status_code=503,
        detail="Object storage checksum capabilities changed while creating the upload plan",
    )


@router.post("/initiate", response_model=UploadPlanRead, status_code=status.HTTP_201_CREATED)
async def initiate_upload(
    payload: UploadInitiateRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> UploadPlanRead:
    """Validate one supported scene input shape and issue a short-lived plan."""
    scene = await resolve_owned_scene_in_project(
        payload.scene_id,
        payload.project_id,
        current_user,
    )
    try:
        validated_files = validate_upload_descriptors(
            payload.files,
            max_archive_bytes=settings.UPLOAD_MAX_ARCHIVE_BYTES,
            max_raster_bytes=settings.UPLOAD_MAX_RASTER_BYTES,
            max_metadata_bytes=settings.UPLOAD_MAX_METADATA_BYTES,
            max_total_bytes=settings.UPLOAD_MAX_TOTAL_BYTES,
        )
    except UploadValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    request_fingerprint = _initiation_request_fingerprint(payload)
    existing_request_plan = await _find_owned_upload_plan_by_client_request_id(
        payload.client_request_id,
        current_user,
    )
    if existing_request_plan is not None:
        return await _replay_initiate_request(
            existing_request_plan,
            request_fingerprint,
            current_user,
        )

    # The partial unique index intentionally counts expired plans until this
    # reaper transitions them. Reclaim first so a normal TTL timeout cannot
    # strand a scene in ``uploading`` or make a later insert fail mysteriously.
    await _reclaim_expired_upload_plans(payload.scene_id, current_user)
    await _retry_terminal_plan_cleanup(payload.scene_id, current_user)
    now = _utcnow()
    active_plan = await _execute(
        lambda: get_supabase()
        .table("upload_plans")
        .select("id")
        .eq("scene_id", str(payload.scene_id))
        .eq("owner_id", current_user.id)
        .in_("status", list(_ACTIVE_PLAN_STATUSES))
        .gt("expires_at", now.isoformat())
        .limit(1)
        .execute(),
        "Upload plan data is temporarily unavailable",
    )
    if _first_row(active_plan) is not None:
        raise HTTPException(
            status_code=409,
            detail="An upload is already in progress for this scene. Cancel it before starting another.",
        )

    scene = await _repair_orphaned_uploading_scene(scene, current_user)
    if scene.get("status") not in _UPLOADABLE_SCENE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="This scene cannot accept a new upload in its current state.",
        )

    active_job = await _execute(
        lambda: get_supabase()
        .table("processing_jobs")
        .select("id")
        .eq("scene_id", str(payload.scene_id))
        .eq("owner_id", current_user.id)
        .in_("status", list(_ACTIVE_JOB_STATUSES))
        .limit(1)
        .execute(),
        "Job data is temporarily unavailable",
    )
    if _first_row(active_job) is not None:
        raise HTTPException(status_code=409, detail="This scene already has an active processing job.")

    storage = await _get_storage()
    # Settings validation is intentionally reached before any provider-side
    # multipart allocation. Signed URL and whole-plan leases are validated
    # independently because large uploads renew short-lived part URLs.
    try:
        _, storage_bucket = settings.require_object_storage()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Object storage is temporarily unavailable") from exc

    plan_id = uuid4()
    expires_at = now + timedelta(
        seconds=_upload_plan_ttl_seconds(sum(item.size_bytes for item in validated_files))
    )
    file_specs: list[dict[str, Any]] = []
    for file_number, validated_file in enumerate(validated_files, start=1):
        upload_file_id = uuid4()
        storage_key = generated_upload_key(
            owner_id=current_user.id,
            project_id=payload.project_id,
            scene_id=payload.scene_id,
            plan_id=plan_id,
            upload_file_id=upload_file_id,
            filename=validated_file.filename,
        )
        file_specs.append(
            {
                "id": upload_file_id,
                "file_number": file_number,
                "validated_file": validated_file,
                "storage_key": storage_key,
                "content_type": validated_file.content_type,
                "checksum_sha256": validated_file.checksum_sha256,
                "part_count": _part_count(validated_file.size_bytes),
                "metadata": {
                    "owner-id": current_user.id,
                    "project-id": str(payload.project_id),
                    "scene-id": str(payload.scene_id),
                    "upload-plan-id": str(plan_id),
                    "upload-file-id": str(upload_file_id),
                    "original-filename": validated_file.filename,
                },
            }
        )

    checksum_mode, created_uploads = await _create_remote_uploads(storage, file_specs)
    plan_row = {
        "id": str(plan_id),
        "owner_id": current_user.id,
        "project_id": str(payload.project_id),
        "scene_id": str(payload.scene_id),
        "status": "initiated",
        "expires_at": expires_at.isoformat(),
        "expected_file_count": len(file_specs),
        "client_request_id": str(payload.client_request_id),
        "request_fingerprint": request_fingerprint,
    }
    file_rows = [
        {
            "id": str(spec["id"]),
            "upload_plan_id": str(plan_id),
            "owner_id": current_user.id,
            "project_id": str(payload.project_id),
            "scene_id": str(payload.scene_id),
            "file_number": spec["file_number"],
            "file_kind": (
                "metadata_sidecar"
                if spec["validated_file"].kind is UploadFileKind.METADATA
                else spec["validated_file"].kind.value
            ),
            "status": "planned",
            "original_filename": spec["validated_file"].filename,
            "content_type": spec["content_type"],
            "storage_bucket": storage_bucket,
            "storage_key": spec["storage_key"],
            "multipart_upload_id": upload_id,
            "multipart_checksum_mode": checksum_mode,
            "expected_size_bytes": spec["validated_file"].size_bytes,
            "expected_checksum_sha256": spec["checksum_sha256"],
            "part_size_bytes": settings.UPLOAD_MULTIPART_PART_SIZE_BYTES,
            "part_count": spec["part_count"],
        }
        for spec, upload_id in created_uploads
    ]

    try:
        creation = await _create_upload_plan_atomically(
            plan_row,
            file_rows,
            current_user,
        )
    except HTTPException:
        # The RPC response itself can be lost after PostgreSQL committed. Read
        # the durable idempotency key before touching remote objects: if this
        # request's plan exists, those multipart IDs are now authoritative and
        # must never be aborted or deleted by recovery cleanup.
        try:
            existing = await _find_owned_upload_plan_by_client_request_id(
                payload.client_request_id,
                current_user,
            )
        except HTTPException:
            # We cannot prove whether the transaction committed. Preserve the
            # provider uploads for TTL/lifecycle cleanup and let the browser
            # recover by the persistent client request ID.
            logger.warning("Unable to reconcile an ambiguous upload-plan creation", exc_info=True)
            raise

        if existing is None:
            # A request/response failure can race with a still-running remote
            # RPC even when this immediate read sees no row. Do not turn that
            # ambiguity into destructive cleanup; the provider lifecycle rule
            # will abort any genuinely unclaimed multipart upload.
            logger.warning("Upload-plan creation outcome is ambiguous; retaining multipart uploads for lifecycle cleanup")
            raise

        if str(existing.get("id")) != str(plan_id):
            await _abort_remote_uploads(
                storage,
                ((spec["storage_key"], upload_id) for spec, upload_id in created_uploads),
            )
        return await _replay_initiate_request(
            existing,
            request_fingerprint,
            current_user,
        )

    outcome = str(creation.get("outcome") or "")
    if outcome == "existing":
        existing = await _find_owned_upload_plan_by_client_request_id(
            payload.client_request_id,
            current_user,
        )
        if existing is None:
            # The response says another transaction owns this request key, but
            # a follow-up read cannot prove which multipart IDs are durable.
            # Leave this local provider upload to its lifecycle rule rather
            # than risk aborting a just-committed plan on a stale response.
            raise HTTPException(status_code=503, detail="Upload plan data is temporarily unavailable")
        if str(existing.get("id")) != str(plan_id):
            await _abort_remote_uploads(
                storage,
                ((spec["storage_key"], upload_id) for spec, upload_id in created_uploads),
            )
        return await _replay_initiate_request(
            existing,
            request_fingerprint,
            current_user,
        )

    if outcome != "created" or str(creation.get("upload_plan_id")) != str(plan_id):
        # This is a confirmed no-create outcome from the atomic RPC, so the
        # multipart IDs were never linked to a durable plan.
        await _abort_remote_uploads(
            storage,
            ((spec["storage_key"], upload_id) for spec, upload_id in created_uploads),
        )
        if outcome == "scene_not_found":
            raise HTTPException(status_code=404, detail="Scene not found")
        if outcome == "active_job":
            raise HTTPException(status_code=409, detail="This scene already has an active processing job.")
        if outcome == "scene_not_uploadable":
            raise HTTPException(status_code=409, detail="This scene cannot accept a new upload in its current state.")
        if outcome == "scene_busy":
            raise HTTPException(
                status_code=409,
                detail="An upload is already in progress for this scene. Cancel it before starting another.",
            )
        raise HTTPException(status_code=503, detail="Upload plan data is temporarily unavailable")

    return UploadPlanRead(
        id=plan_id,
        project_id=payload.project_id,
        scene_id=payload.scene_id,
        status="initiated",
        expires_at=expires_at,
        part_size_bytes=settings.UPLOAD_MULTIPART_PART_SIZE_BYTES,
        multipart_checksum_mode=MultipartChecksumMode(checksum_mode),
        files=[
            UploadPlanFileRead(
                id=spec["id"],
                kind=spec["validated_file"].kind,
                filename=spec["validated_file"].filename,
                content_type=spec["content_type"],
                size_bytes=spec["validated_file"].size_bytes,
                part_size_bytes=settings.UPLOAD_MULTIPART_PART_SIZE_BYTES,
                part_count=spec["part_count"],
            )
            for spec in file_specs
        ],
    )


@router.get("/initiation/{client_request_id}", response_model=UploadPlanRead)
async def resolve_upload_initiation(
    client_request_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> UploadPlanRead:
    """Recover an owned upload plan from the browser's durable request UUID."""
    plan = await _find_owned_upload_plan_by_client_request_id(client_request_id, current_user)
    if plan is None:
        # Keep the ownership boundary opaque, as with project/scene resolvers.
        raise HTTPException(status_code=404, detail="Upload initiation not found")
    plan = await _reload_if_expired_active_upload_plan(plan, current_user)
    return await _load_upload_plan_read(plan, current_user)


@router.get("/{upload_plan_id}/status", response_model=UploadPlanStatusRead)
async def get_upload_plan_status(
    upload_plan_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> UploadPlanStatusRead:
    """Return durable plan state and only the job created by this exact plan."""
    plan = await resolve_owned_upload_plan(upload_plan_id, current_user)
    plan = await _reload_if_expired_active_upload_plan(plan, current_user)

    job_response = await _execute(
        lambda: get_supabase()
        .table("processing_jobs")
        .select("*")
        .eq("upload_plan_id", str(plan["id"]))
        .eq("scene_id", str(plan["scene_id"]))
        .eq("project_id", str(plan["project_id"]))
        .eq("owner_id", current_user.id)
        .limit(1)
        .execute(),
        "Job data is temporarily unavailable",
    )
    job = _first_row(job_response)
    return UploadPlanStatusRead(
        id=plan["id"],
        project_id=plan["project_id"],
        scene_id=plan["scene_id"],
        status=plan["status"],
        expires_at=plan["expires_at"],
        failure_code=plan.get("failure_code"),
        failure_detail=plan.get("failure_detail"),
        job=job,
    )


@router.post("/{upload_plan_id}/parts/sign", response_model=UploadPartSignResponse)
async def sign_upload_parts(
    upload_plan_id: UUID,
    payload: UploadPartSignRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> UploadPartSignResponse:
    """Issue narrowly scoped direct PUT URLs after chunk checksums are known."""
    plan = await resolve_owned_upload_plan(upload_plan_id, current_user)
    expires_at = _parse_timestamp(plan.get("expires_at"))
    if plan.get("status") != "initiated":
        raise HTTPException(status_code=409, detail="This upload plan is not accepting new parts.")
    if expires_at is None or expires_at <= _utcnow():
        await _release_expired_upload_plan(plan, current_user)
        raise HTTPException(status_code=410, detail="This upload plan has expired. Start a new upload.")

    file_response = await _execute(
        lambda: get_supabase()
        .table("upload_plan_files")
        .select("*")
        .eq("id", str(payload.upload_file_id))
        .eq("upload_plan_id", str(upload_plan_id))
        .eq("owner_id", current_user.id)
        .limit(1)
        .execute(),
        "Upload plan data is temporarily unavailable",
    )
    upload_file = _first_row(file_response)
    if upload_file is None:
        raise HTTPException(status_code=404, detail="Upload file not found")
    if upload_file.get("status") != "planned":
        raise HTTPException(status_code=409, detail="This upload file is not accepting new parts.")

    part_count = int(upload_file["part_count"])
    if any(part.part_number > part_count for part in payload.parts):
        raise HTTPException(status_code=422, detail="A requested multipart part is outside this upload plan.")
    checksum_mode = str(upload_file["multipart_checksum_mode"])
    if checksum_mode == "sha256" and any(part.checksum_sha256 is None for part in payload.parts):
        raise HTTPException(status_code=422, detail="Every multipart part requires a SHA-256 checksum.")

    storage = await _get_storage()
    if storage.multipart_checksum_mode != checksum_mode:
        raise HTTPException(
            status_code=409,
            detail="Object storage checksum capabilities changed. Cancel and restart this upload.",
        )
    seconds_remaining = max(1, int((expires_at - _utcnow()).total_seconds()))
    url_ttl = min(settings.STORAGE_SIGNED_URL_TTL_SECONDS, seconds_remaining)
    try:
        instructions = await asyncio.gather(
            *(
                run_in_threadpool(
                    storage.presign_upload_part,
                    str(upload_file["storage_key"]),
                    str(upload_file["multipart_upload_id"]),
                    part.part_number,
                    url_ttl,
                    checksum_sha256=part.checksum_sha256,
                )
                for part in payload.parts
            )
        )
    except (ObjectStorageError, ValueError) as exc:
        logger.info("Unable to sign multipart part", exc_info=True)
        raise HTTPException(status_code=503, detail="Could not prepare direct upload URLs") from exc

    return UploadPartSignResponse(
        upload_file_id=payload.upload_file_id,
        expires_at=_utcnow() + timedelta(seconds=url_ttl),
        parts=[
            UploadPartInstruction(
                part_number=part.part_number,
                url=url,
                headers=(
                    {"x-amz-checksum-sha256": part.checksum_sha256}
                    if checksum_mode == "sha256" and part.checksum_sha256 is not None
                    else {}
                ),
            )
            for part, url in zip(payload.parts, instructions, strict=True)
        ],
    )


async def _publish_completed_job_if_needed(
    dispatch: dict[str, Any],
    job_id: str,
    current_user: CurrentUser,
) -> str:
    """Claim, publish, and settle one durable outbox row at most once per lease."""
    current_status = str(dispatch.get("status") or "pending")
    now = _utcnow()
    dispatch_id = str(dispatch["id"])

    if current_status == "failed":
        return "failed" if await _mark_dispatch_exhausted(dispatch, current_user) else "publishing"

    # A previous API process may have died after claiming the row. Do not take
    # over a live publisher, but make a stale lease retryable so M3's future
    # outbox dispatcher has the same recovery semantics.
    if current_status == "publishing":
        locked_at = _parse_timestamp(dispatch.get("locked_at"))
        lease_cutoff = now - timedelta(seconds=settings.JOB_DISPATCH_LEASE_SECONDS)
        if locked_at is not None and locked_at > lease_cutoff:
            return "publishing"
        previous_lock_token = dispatch.get("locked_by")
        if not isinstance(previous_lock_token, str) or not previous_lock_token:
            # A malformed legacy lock is left for the durable dispatcher; do
            # not risk taking a lease that cannot be compared atomically.
            return "publishing"
        previous_locked_at = dispatch.get("locked_at")
        if not isinstance(previous_locked_at, str) or not previous_locked_at:
            return "publishing"
        recovered = await _execute(
            lambda: get_supabase()
            .table("processing_job_dispatches")
            .update(
                {
                    "status": "retry_scheduled",
                    "available_at": now.isoformat(),
                    "locked_at": None,
                    "locked_by": None,
                    "last_error": "A previous dispatch publisher lease expired.",
                }
            )
            .eq("id", dispatch_id)
            .eq("owner_id", current_user.id)
            .eq("status", "publishing")
            .eq("locked_by", previous_lock_token)
            .eq("locked_at", previous_locked_at)
            .execute(),
            "Job dispatch data is temporarily unavailable",
        )
        if _first_row(recovered) is None:
            return "publishing"
        current_status = "retry_scheduled"
        dispatch = {**dispatch, "status": current_status, "locked_at": None, "locked_by": None}

    if current_status not in {"pending", "retry_scheduled"}:
        return current_status

    available_at = _parse_timestamp(dispatch.get("available_at"))
    if available_at is not None and available_at > now:
        return "retry_scheduled"

    attempt_count = int(dispatch.get("attempt_count") or 0)
    max_attempts = int(dispatch.get("max_attempts") or 1)
    if attempt_count >= max_attempts:
        return (
            "failed"
            if await _mark_dispatch_exhausted(dispatch, current_user)
            else current_status
        )

    lock_token = f"upload-api:{uuid4()}"
    claimed_at = now.isoformat()
    claimed = await _execute(
        lambda: get_supabase()
        .table("processing_job_dispatches")
        .update(
            {
                "status": "publishing",
                "attempt_count": attempt_count + 1,
                "last_attempt_at": claimed_at,
                "locked_at": claimed_at,
                "locked_by": lock_token,
                "last_error": None,
            }
        )
        .eq("id", dispatch_id)
        .eq("owner_id", current_user.id)
        .in_("status", ["pending", "retry_scheduled"])
        .eq("attempt_count", attempt_count)
        .execute(),
        "Job dispatch data is temporarily unavailable",
    )
    if _first_row(claimed) is None:
        return "publishing"

    try:
        await publish_processing_job(job_id)
        published = await _execute(
            lambda: get_supabase()
            .table("processing_job_dispatches")
            .update(
                {
                    "status": "published",
                    "published_at": _utcnow().isoformat(),
                    "locked_at": None,
                    "locked_by": None,
                    "last_error": None,
                }
            )
            .eq("id", dispatch_id)
            .eq("owner_id", current_user.id)
            .eq("status", "publishing")
            .eq("locked_by", lock_token)
            .execute(),
            "Job dispatch data is temporarily unavailable",
        )
        return "published" if _first_row(published) is not None else "publishing"
    except Exception:
        logger.warning("Redis job publication failed; retaining the durable outbox row", exc_info=True)
        try:
            retry = await _execute(
                lambda: get_supabase()
                .table("processing_job_dispatches")
                .update(
                    {
                        "status": "retry_scheduled",
                        "available_at": _dispatch_retry_at(attempt_count + 1).isoformat(),
                        "locked_at": None,
                        "locked_by": None,
                        "last_error": "Initial Redis publication failed.",
                    }
                )
                .eq("id", dispatch_id)
                .eq("owner_id", current_user.id)
                .eq("status", "publishing")
                .eq("locked_by", lock_token)
                .execute(),
                "Job dispatch data is temporarily unavailable",
            )
            if _first_row(retry) is None:
                return "publishing"
        except HTTPException:
            logger.warning("Failed to update durable job dispatch after Redis outage", exc_info=True)
            return "publishing"
        return "retry_scheduled"


async def _mark_dispatch_exhausted(
    dispatch: dict[str, Any],
    current_user: CurrentUser,
) -> bool:
    """Atomically surface an exhausted outbox retry as a terminal job/scene."""
    try:
        response = await _execute(
            lambda: get_supabase()
            .rpc(
                "fail_exhausted_job_dispatch",
                {
                    "p_owner_id": current_user.id,
                    "p_dispatch_id": str(dispatch["id"]),
                },
            )
            .execute(),
            "Job dispatch data is temporarily unavailable",
        )
        return _first_row(response) is not None
    except HTTPException:
        logger.warning("Failed to surface exhausted dispatch as a failed job", exc_info=True)
        return False


async def _completed_upload_response(
    plan: dict[str, Any],
    current_user: CurrentUser,
) -> UploadCompleteResponse:
    """Load the exact durable completion result for normal and retry paths."""
    plan_id = str(plan["id"])
    files_response = await _execute(
        lambda: get_supabase()
        .table("upload_plan_files")
        .select("storage_key")
        .eq("upload_plan_id", plan_id)
        .eq("owner_id", current_user.id)
        .order("file_number")
        .execute(),
        "Upload completion records are temporarily unavailable",
    )
    upload_files = _rows(files_response)
    storage_keys = [str(row["storage_key"]) for row in upload_files]
    if not storage_keys:
        raise HTTPException(status_code=503, detail="Upload completion records are temporarily unavailable")

    scene_response = await _execute(
        lambda: get_supabase()
        .table("scenes")
        .select("*")
        .eq("id", str(plan["scene_id"]))
        .eq("project_id", str(plan["project_id"]))
        .eq("owner_id", current_user.id)
        .limit(1)
        .execute(),
        "Scene data is temporarily unavailable",
    )
    job_response = await _execute(
        lambda: get_supabase()
        .table("processing_jobs")
        .select("*")
        .eq("upload_plan_id", plan_id)
        .eq("scene_id", str(plan["scene_id"]))
        .eq("project_id", str(plan["project_id"]))
        .eq("owner_id", current_user.id)
        .limit(1)
        .execute(),
        "Job data is temporarily unavailable",
    )
    artifact_response = await _execute(
        lambda: get_supabase()
        .table("scene_artifacts")
        .select("*")
        .eq("scene_id", str(plan["scene_id"]))
        .eq("project_id", str(plan["project_id"]))
        .eq("owner_id", current_user.id)
        .in_("storage_key", storage_keys)
        .execute(),
        "Artifact data is temporarily unavailable",
    )
    scene_row = _first_row(scene_response)
    job_row = _first_row(job_response)
    artifacts = _rows(artifact_response)
    if scene_row is None or job_row is None or len(artifacts) != len(storage_keys):
        raise HTTPException(status_code=503, detail="Upload completion records are temporarily unavailable")

    dispatch_response = await _execute(
        lambda: get_supabase()
        .table("processing_job_dispatches")
        .select("*")
        .eq("processing_job_id", str(job_row["id"]))
        .eq("scene_id", str(plan["scene_id"]))
        .eq("project_id", str(plan["project_id"]))
        .eq("owner_id", current_user.id)
        .limit(1)
        .execute(),
        "Job dispatch data is temporarily unavailable",
    )
    dispatch = _first_row(dispatch_response)
    if dispatch is None:
        raise HTTPException(status_code=503, detail="Upload completion records are temporarily unavailable")
    dispatch_status = await _publish_completed_job_if_needed(
        dispatch,
        str(job_row["id"]),
        current_user,
    )
    return UploadCompleteResponse(
        scene=scene_row,
        job=job_row,
        artifacts=artifacts,
        dispatch_status=dispatch_status,
    )


async def _recover_completed_upload_response(
    upload_plan_id: UUID,
    current_user: CurrentUser,
) -> UploadCompleteResponse | None:
    """Recover a committed completion when its original RPC response was lost."""
    try:
        current_plan = await resolve_owned_upload_plan(upload_plan_id, current_user)
        if current_plan.get("status") != "completed":
            return None
        return await _completed_upload_response(current_plan, current_user)
    except HTTPException:
        logger.warning("Unable to confirm an ambiguous upload completion", exc_info=True)
        return None


@router.post("/{upload_plan_id}/complete", response_model=UploadCompleteResponse)
async def complete_upload(
    upload_plan_id: UUID,
    payload: UploadCompleteRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> UploadCompleteResponse:
    """Verify completed objects, atomically create artifacts/job, then publish it."""
    plan = await resolve_owned_upload_plan(upload_plan_id, current_user)
    expires_at = _parse_timestamp(plan.get("expires_at"))
    if plan.get("status") == "completed":
        try:
            return await _completed_upload_response(plan, current_user)
        except HTTPException:
            raise _completion_reconciliation_error() from None
    if plan.get("status") == "completing":
        if expires_at is None or expires_at <= _utcnow():
            await _release_expired_upload_plan(plan, current_user)
            raise HTTPException(
                status_code=410,
                detail="The upload completion lease expired. Start a new upload.",
            )
        raise _completion_reconciliation_error(status_code=409)
    if plan.get("status") != "initiated":
        raise HTTPException(status_code=409, detail="This upload plan cannot be completed in its current state.")
    if expires_at is None or expires_at <= _utcnow():
        await _release_expired_upload_plan(plan, current_user)
        raise HTTPException(status_code=410, detail="This upload plan has expired. Start a new upload.")

    # Resolve every local/database/storage prerequisite before taking the
    # completion lease. A transient failure here therefore leaves the browser
    # free to retry with the same initiated plan rather than stranding it in
    # ``completing``.
    files_response = await _execute(
        lambda: get_supabase()
        .table("upload_plan_files")
        .select("*")
        .eq("upload_plan_id", str(upload_plan_id))
        .eq("owner_id", current_user.id)
        .order("file_number")
        .execute(),
        "Upload plan data is temporarily unavailable",
    )
    upload_files = _rows(files_response)
    completion_by_file_id: dict[str, CompleteUploadFile] = {
        str(item.upload_file_id): item for item in payload.files
    }
    planned_file_ids = {str(row["id"]) for row in upload_files}
    if not upload_files or set(completion_by_file_id) != planned_file_ids:
        raise HTTPException(status_code=422, detail="The completed files did not match the approved upload plan.")

    for row in upload_files:
        completion = completion_by_file_id[str(row["id"])]
        if row.get("status") != "planned" or len(completion.parts) != int(row["part_count"]):
            raise HTTPException(
                status_code=422,
                detail="The completed multipart parts did not match the approved upload plan.",
            )
        if (
            row.get("multipart_checksum_mode") == "sha256"
            and any(part.checksum_sha256 is None for part in completion.parts)
        ):
            raise HTTPException(status_code=422, detail="Every uploaded part requires a checksum.")

    storage = await _get_storage()
    expected_mode = str(upload_files[0]["multipart_checksum_mode"])
    if (
        any(row.get("multipart_checksum_mode") != expected_mode for row in upload_files)
        or storage.multipart_checksum_mode != expected_mode
    ):
        raise HTTPException(
            status_code=409,
            detail="Object storage checksum capabilities changed. Start the upload again.",
        )

    # The compare-and-set transition prevents duplicate CompleteMultipart calls
    # from two browser retries. The completion lease is intentionally longer
    # than signed URLs, because it covers raw server-side checksum streaming
    # and the atomic database finalization.
    lease_expires_at = _utcnow() + timedelta(seconds=settings.UPLOAD_COMPLETION_LEASE_SECONDS)
    transitioned = await _execute(
        lambda: get_supabase()
        .table("upload_plans")
        .update({"status": "completing", "expires_at": lease_expires_at.isoformat()})
        .eq("id", str(upload_plan_id))
        .eq("owner_id", current_user.id)
        .eq("status", "initiated")
        .gt("expires_at", _utcnow().isoformat())
        .execute(),
        "Upload plan data is temporarily unavailable",
    )
    if _first_row(transitioned) is None:
        recovered = await _recover_completed_upload_response(upload_plan_id, current_user)
        if recovered is not None:
            return recovered
        try:
            current_plan = await resolve_owned_upload_plan(upload_plan_id, current_user)
        except HTTPException:
            raise _completion_reconciliation_error() from None
        if current_plan.get("status") == "completing":
            raise _completion_reconciliation_error(status_code=409)
        raise HTTPException(
            status_code=409,
            detail="This upload plan is no longer available for completion. Start a new upload.",
        )

    verified_files: list[dict[str, Any]] = []
    storage_keys = [str(row["storage_key"]) for row in upload_files]
    try:
        # Complete sequentially to bound server-side checksum streaming and
        # avoid multiplying memory/network pressure for multi-file SAR input.
        for row in upload_files:
            completion = completion_by_file_id[str(row["id"])]
            object_info = await _run_with_completion_lease(
                upload_plan_id,
                current_user,
                lambda row=row, completion=completion: storage.complete_multipart_upload(
                    str(row["storage_key"]),
                    str(row["multipart_upload_id"]),
                    [
                        CompletedMultipartPart(
                            part_number=part.part_number,
                            etag=part.etag,
                            checksum_sha256=part.checksum_sha256,
                        )
                        for part in completion.parts
                    ],
                    row.get("expected_checksum_sha256"),
                ),
            )
            await run_in_threadpool(
                validate_completed_object,
                storage,
                object_info,
                _validated_file_from_row(row),
                max_zip_entries=settings.UPLOAD_MAX_ZIP_ENTRIES,
                max_zip_central_directory_bytes=settings.UPLOAD_MAX_ZIP_CENTRAL_DIRECTORY_BYTES,
                max_zip_uncompressed_bytes=settings.UPLOAD_MAX_ZIP_UNCOMPRESSED_BYTES,
                max_zip_compression_ratio=settings.UPLOAD_MAX_ZIP_COMPRESSION_RATIO,
            )
            if object_info.checksum_sha256 is None:
                raise ObjectIntegrityError("Object storage did not return a server-verified SHA-256 checksum.")
            verified_files.append(
                {
                    "upload_plan_file_id": str(row["id"]),
                    "size_bytes": object_info.size_bytes,
                    "checksum_sha256": object_info.checksum_sha256,
                    "etag": object_info.etag,
                    "version_id": object_info.version_id,
                    "content_type": object_info.content_type,
                }
            )
        if not await _renew_completion_lease(upload_plan_id, current_user):
            raise HTTPException(
                status_code=409,
                detail="The upload completion lease was lost. Start a new upload.",
            )
    except UploadValidationError as exc:
        await _delete_completed_objects(storage, storage_keys)
        await _abort_remote_uploads(
            storage,
            ((str(row["storage_key"]), str(row["multipart_upload_id"])) for row in upload_files),
        )
        await _mark_upload_plan_failed(
            upload_plan_id,
            current_user,
            error_code="invalid_upload_content",
            error_detail=str(exc),
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (ObjectIntegrityError, ObjectStorageError, ValueError) as exc:
        await _delete_completed_objects(storage, storage_keys)
        await _abort_remote_uploads(
            storage,
            ((str(row["storage_key"]), str(row["multipart_upload_id"])) for row in upload_files),
        )
        await _mark_upload_plan_failed(
            upload_plan_id,
            current_user,
            error_code="upload_verification_failed",
            error_detail="The uploaded object could not be verified.",
        )
        logger.info("Object verification failed", exc_info=True)
        raise HTTPException(status_code=422, detail="The uploaded object could not be verified.") from exc
    except HTTPException:
        raise
    except Exception:
        await _delete_completed_objects(storage, storage_keys)
        await _abort_remote_uploads(
            storage,
            ((str(row["storage_key"]), str(row["multipart_upload_id"])) for row in upload_files),
        )
        await _mark_upload_plan_failed(
            upload_plan_id,
            current_user,
            error_code="upload_completion_failed",
            error_detail="The uploaded object could not be completed safely.",
        )
        logger.exception("Unexpected upload completion failure")
        raise HTTPException(status_code=503, detail="The uploaded object could not be completed safely.") from None

    try:
        finalized = await _execute(
            lambda: get_supabase()
            .rpc(
                "finalize_upload_plan",
                {
                    "p_owner_id": current_user.id,
                    "p_upload_plan_id": str(upload_plan_id),
                    "p_verified_files": verified_files,
                    "p_dispatch_payload": {"message_schema": "raikou.process_scene.v1"},
                },
            )
            .execute(),
            "Upload completion data is temporarily unavailable",
        )
    except HTTPException:
        # A response timeout is ambiguous: PostgreSQL may have committed the
        # transaction after the client lost the response. Read durable state
        # first and never delete source objects until a failed/expired plan is
        # conclusively claimed by the reaper.
        recovered = await _recover_completed_upload_response(upload_plan_id, current_user)
        if recovered is not None:
            return recovered
        raise _completion_reconciliation_error() from None

    result = _first_row(finalized)
    if result is None:
        recovered = await _recover_completed_upload_response(upload_plan_id, current_user)
        if recovered is not None:
            return recovered
        raise _completion_reconciliation_error()

    try:
        # Source artifacts and the queued scene state have changed. M5 caches
        # are derived only, so clear them before the next authorized search.
        await invalidate_project_evidence_cache(
            owner_id=current_user.id,
            project_id=str(plan["project_id"]),
        )
        return await _completed_upload_response(plan, current_user)
    except HTTPException:
        raise _completion_reconciliation_error() from None


@router.delete("/{upload_plan_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_upload_plan(
    upload_plan_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> Response:
    """Revoke a plan before completion and abort its provider multipart uploads."""
    plan = await resolve_owned_upload_plan(upload_plan_id, current_user)
    if plan.get("status") == "completed":
        raise HTTPException(status_code=409, detail="A completed upload plan cannot be cancelled.")
    if plan.get("status") in {"aborted", "expired", "failed"}:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    expires_at = _parse_timestamp(plan.get("expires_at"))
    if expires_at is None or expires_at <= _utcnow():
        await _release_expired_upload_plan(plan, current_user)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    if plan.get("status") == "completing":
        raise HTTPException(
            status_code=409,
            detail="This upload is already being completed and cannot be cancelled safely.",
        )
    if plan.get("status") not in {"initiated", "uploading"}:
        raise HTTPException(status_code=409, detail="This upload plan cannot be cancelled in its current state.")

    file_rows = await _load_plan_storage_rows(upload_plan_id, current_user)
    transitioned = await _transition_upload_plan_terminal(
        upload_plan_id,
        current_user,
        expected_statuses=["initiated", "uploading"],
        target_status="aborted",
        require_expired=False,
    )
    if not transitioned:
        raise HTTPException(status_code=409, detail="This upload plan is already completing or has expired.")
    # Database state is released before provider cleanup. A storage outage must
    # not keep a user-owned scene unavailable; bucket lifecycle rules and this
    # best-effort abort handle any remaining multipart parts.
    await _cleanup_terminal_plan_objects(upload_plan_id, current_user, file_rows)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
