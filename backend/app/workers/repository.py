"""PostgreSQL authority for M3 dispatcher and workers.

Redis Streams are intentionally absent from this module.  Every claim, lease,
state transition, artifact reference, and retry is committed here before a
worker acknowledges a stream entry.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
import json
import logging
from typing import Any, Iterator
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.core.config import settings


logger = logging.getLogger(__name__)
TERMINAL_JOB_STATUSES = {"ready", "failed", "cancelled"}
TERMINAL_TASK_STATUSES = {"succeeded", "failed", "cancelled"}


class RetryableTaskError(RuntimeError):
    """A transient integration failure that should use the durable retry path."""


class UserFacingTaskError(RuntimeError):
    """A validated input or processing failure safe to expose as an error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class WorkerRepository:
    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn or settings.require_worker_database_url()

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection[dict[str, Any]]]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            with connection.transaction():
                yield connection

    @staticmethod
    def _one(cursor: Any) -> dict[str, Any] | None:
        row = cursor.fetchone()
        return dict(row) if row else None

    def bootstrap_m2_jobs(self, *, limit: int = 50) -> int:
        """Turn durable M2 ingress rows into the first M3 CPU task exactly once."""
        created = 0
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                select d.id as dispatch_id, d.processing_job_id
                from public.processing_job_dispatches d
                join public.processing_jobs j on j.id = d.processing_job_id
                where d.status not in ('failed'::public.processing_job_dispatch_status,
                                       'cancelled'::public.processing_job_dispatch_status)
                  and j.kind = 'process_scene'::public.processing_job_kind
                  and not exists (
                    select 1 from public.processing_job_tasks t
                    where t.processing_job_id = j.id
                  )
                order by d.created_at
                for update of d, j skip locked
                limit %s
                """,
                (limit,),
            )
            rows = cursor.fetchall()
            for row in rows:
                cursor.execute(
                    "select public.m3_enqueue_task(%s, 'validate_upload'::public.processing_job_stage, 'cpu'::public.processing_execution_class, '{}'::jsonb, %s) as task_id",
                    (row["processing_job_id"], settings.M3_TASK_MAX_ATTEMPTS),
                )
                cursor.execute(
                    """
                    update public.processing_job_dispatches
                    set status = 'published'::public.processing_job_dispatch_status,
                        published_at = coalesce(published_at, now()),
                        locked_at = null,
                        locked_by = null,
                        last_error = null
                    where id = %s
                    """,
                    (row["dispatch_id"],),
                )
                created += 1
        return created

    def claim_task_dispatches(self, worker_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """Lease M3 outbox rows for one dispatcher process."""
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                with candidates as (
                  select id
                  from public.processing_task_dispatches
                  where attempt_count < max_attempts
                    and (
                      (status in ('pending'::public.processing_task_dispatch_status,
                                  'retry_scheduled'::public.processing_task_dispatch_status)
                       and available_at <= now())
                      or (status = 'publishing'::public.processing_task_dispatch_status
                          and locked_at < now() - make_interval(secs => %s))
                    )
                  order by available_at, created_at
                  for update skip locked
                  limit %s
                )
                update public.processing_task_dispatches d
                set status = 'publishing'::public.processing_task_dispatch_status,
                    locked_at = now(), locked_by = %s,
                    attempt_count = d.attempt_count + 1
                from candidates
                where d.id = candidates.id
                returning d.*
                """,
                (settings.M3_DISPATCH_LEASE_SECONDS, limit, worker_id),
            )
            return [dict(row) for row in cursor.fetchall()]

    def settle_task_dispatch(self, dispatch_id: str | UUID, worker_id: str, *, error: str | None = None) -> None:
        with self.transaction() as connection, connection.cursor() as cursor:
            if error is None:
                cursor.execute(
                    """
                    update public.processing_task_dispatches
                    set status = 'published'::public.processing_task_dispatch_status,
                        published_at = now(), locked_at = null, locked_by = null, last_error = null
                    where id = %s and locked_by = %s
                    """,
                    (dispatch_id, worker_id),
                )
                return
            cursor.execute(
                """
                update public.processing_task_dispatches
                set status = case when attempt_count >= max_attempts
                                  then 'failed'::public.processing_task_dispatch_status
                                  else 'retry_scheduled'::public.processing_task_dispatch_status end,
                    available_at = now() + make_interval(secs => least(300, 5 * power(2, greatest(attempt_count - 1, 0))::integer)),
                    locked_at = null, locked_by = null, last_error = left(%s, 500)
                where id = %s and locked_by = %s
                """,
                (error, dispatch_id, worker_id),
            )

    def claim_task(self, task_id: str | UUID, *, worker_id: str, execution_class: str) -> dict[str, Any] | None:
        """Lease one task and atomically move its parent into an active state."""
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                select t.*, j.kind as job_kind, j.status as job_status, j.stage as job_stage,
                       j.progress as job_progress, j.cancel_requested_at, j.attempt as job_attempt
                from public.processing_job_tasks t
                join public.processing_jobs j on j.id = t.processing_job_id
                where t.id = %s
                for update of t, j
                """,
                (task_id,),
            )
            task = self._one(cursor)
            if task is None or task["execution_class"] != execution_class:
                return None
            if task["status"] in TERMINAL_TASK_STATUSES or task["job_status"] in TERMINAL_JOB_STATUSES:
                return None
            if task["status"] == "leased" and task["locked_at"] is not None:
                cursor.execute(
                    "select now() - %s::timestamptz > make_interval(secs => %s) as expired",
                    (task["locked_at"], settings.M3_TASK_LEASE_SECONDS),
                )
                if not bool(self._one(cursor)["expired"]):
                    return None
            if int(task["attempt"]) >= int(task["max_attempts"]):
                self._fail_task_locked(cursor, task, "RETRY_EXHAUSTED", "Worker retry budget exhausted.")
                return None

            next_job_status = "validating" if task["stage"] == "validate_upload" else "processing"
            cursor.execute(
                """
                update public.processing_job_tasks
                set status = 'leased'::public.processing_task_status,
                    locked_by = %s, locked_at = now(), started_at = coalesce(started_at, now()),
                    attempt = attempt + 1
                where id = %s
                returning attempt
                """,
                (worker_id, task_id),
            )
            attempt = int(self._one(cursor)["attempt"])
            cursor.execute(
                """
                update public.processing_jobs
                set status = %s::public.processing_job_status,
                    stage = %s::public.processing_job_stage,
                    -- Task retries are per-stage and may legitimately exceed
                    -- the legacy parent-job retry budget.  The task table is
                    -- the source of truth for its retry count; keep the
                    -- parent summary within its own database constraint.
                    attempt = least(greatest(attempt, %s), max_attempts),
                    started_at = coalesce(started_at, now()),
                    error_code = null, error_detail = null
                where id = %s
                """,
                (next_job_status, task["stage"], attempt, task["processing_job_id"]),
            )
            if task["job_kind"] == "process_scene":
                cursor.execute(
                    """
                    update public.scenes set status = 'processing'::public.scene_status
                    where id = %s and status <> 'deleting'::public.scene_status
                    """,
                    (task["scene_id"],),
                )
            self._insert_event_locked(
                cursor, task, status=next_job_status, stage=task["stage"], progress=int(task["job_progress"]),
                attempt=attempt, event_type="task_claimed", detail={"worker_id": worker_id},
            )
            task["attempt"] = attempt
            task["job_status"] = next_job_status
            return task

    def task_cancel_requested(self, task: dict[str, Any]) -> bool:
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute("select cancel_requested_at from public.processing_jobs where id = %s", (task["processing_job_id"],))
            row = self._one(cursor)
            return bool(row and row["cancel_requested_at"])

    def complete_task(
        self,
        task: dict[str, Any],
        *,
        worker_id: str,
        result: dict[str, Any],
        next_stage: tuple[str, str] | None,
        progress: int,
    ) -> None:
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "select * from public.processing_job_tasks where id = %s for update",
                (task["id"],),
            )
            current = self._one(cursor)
            if current is None or current["status"] != "leased" or current["locked_by"] != worker_id:
                return
            cursor.execute(
                """
                update public.processing_job_tasks
                set status = 'succeeded'::public.processing_task_status, result = %s,
                    finished_at = now(), locked_at = null, locked_by = null,
                    error_code = null, error_detail = null
                where id = %s
                """,
                (Jsonb(result), current["id"]),
            )
            if next_stage is not None:
                stage, execution_class = next_stage
                cursor.execute(
                    "select public.m3_enqueue_task(%s, %s::public.processing_job_stage, %s::public.processing_execution_class, '{}'::jsonb, %s)",
                    (current["processing_job_id"], stage, execution_class, settings.M3_TASK_MAX_ATTEMPTS),
                )
                next_status = "processing" if stage != "validate_upload" else "validating"
                cursor.execute(
                    """
                    update public.processing_jobs
                    set status = %s::public.processing_job_status,
                        stage = %s::public.processing_job_stage, progress = %s
                    where id = %s
                    """,
                    (next_status, stage, progress, current["processing_job_id"]),
                )
                self._insert_event_locked(cursor, current, status=next_status, stage=stage, progress=progress,
                                          attempt=int(current["attempt"]), event_type="stage_completed", detail=result)
                return

            final_status = "ready"
            cursor.execute(
                """
                update public.processing_jobs
                set status = 'ready'::public.processing_job_status, stage = 'finalize'::public.processing_job_stage,
                    progress = 100, finished_at = now(), retry_after = null
                where id = %s
                """,
                (current["processing_job_id"],),
            )
            if bool(result.get("delete_scene")):
                self._insert_event_locked(cursor, current, status=final_status, stage="cleanup", progress=100,
                                          attempt=int(current["attempt"]), event_type="scene_cleanup_completed", detail=result)
                # Delete the database scope last. Its cascading foreign keys
                # remove the cleanup job only after all external objects and
                # tenant vectors have been removed by the worker stage.
                cursor.execute("delete from public.scenes where id = %s and owner_id = %s", (current["scene_id"], current["owner_id"]))
                return
            cursor.execute(
                "update public.scenes set status = 'ready'::public.scene_status where id = %s and status <> 'deleting'::public.scene_status",
                (current["scene_id"],),
            )
            self._insert_event_locked(cursor, current, status=final_status, stage="finalize", progress=100,
                                      attempt=int(current["attempt"]), event_type="job_ready", detail=result)

    def retry_or_fail_task(self, task: dict[str, Any], *, worker_id: str, code: str, detail: str, retryable: bool) -> None:
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute("select * from public.processing_job_tasks where id = %s for update", (task["id"],))
            current = self._one(cursor)
            if current is None or current["status"] != "leased" or current["locked_by"] != worker_id:
                return
            should_retry = retryable and int(current["attempt"]) < int(current["max_attempts"])
            if should_retry:
                cursor.execute(
                    """
                    update public.processing_job_tasks
                    set status = 'retry_scheduled'::public.processing_task_status,
                        available_at = now() + make_interval(secs => least(300, 5 * power(2, greatest(attempt - 1, 0))::integer)),
                        locked_at = null, locked_by = null, error_code = %s, error_detail = left(%s, 500)
                    where id = %s
                    """,
                    (code, detail, current["id"]),
                )
                cursor.execute(
                    """
                    update public.processing_task_dispatches
                    set status = 'retry_scheduled'::public.processing_task_dispatch_status,
                        available_at = now(), locked_at = null, locked_by = null
                    where task_id = %s
                    """,
                    (current["id"],),
                )
                cursor.execute(
                    "update public.processing_jobs set retry_after = now(), error_code = %s, error_detail = left(%s, 500) where id = %s",
                    (code, detail, current["processing_job_id"]),
                )
                self._insert_event_locked(cursor, current, status="processing", stage=current["stage"],
                                          progress=self._job_progress(cursor, current["processing_job_id"]),
                                          attempt=int(current["attempt"]), event_type="task_retry_scheduled", error_code=code,
                                          detail={"message": detail})
                return
            self._fail_task_locked(cursor, current, code, detail)

    def cancel_task_after_cleanup(self, task: dict[str, Any], *, worker_id: str, detail: dict[str, Any]) -> None:
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute("select * from public.processing_job_tasks where id = %s for update", (task["id"],))
            current = self._one(cursor)
            if current is None or current["status"] != "leased" or current["locked_by"] != worker_id:
                return
            cursor.execute(
                """
                update public.processing_job_tasks
                set status = 'cancelled'::public.processing_task_status, finished_at = now(),
                    locked_at = null, locked_by = null, result = %s
                where id = %s
                """,
                (Jsonb(detail), current["id"]),
            )
            cursor.execute(
                """
                update public.processing_jobs
                set status = 'cancelled'::public.processing_job_status, finished_at = now(),
                    error_code = 'CANCELLED', error_detail = null
                where id = %s
                """,
                (current["processing_job_id"],),
            )
            cursor.execute(
                "update public.scenes set status = 'cancelled'::public.scene_status where id = %s and status <> 'deleting'::public.scene_status",
                (current["scene_id"],),
            )
            self._insert_event_locked(cursor, current, status="cancelled", stage=current["stage"],
                                      progress=self._job_progress(cursor, current["processing_job_id"]),
                                      attempt=int(current["attempt"]), event_type="job_cancelled", detail=detail)

    def job_sources(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                select * from public.scene_artifacts
                where scene_id = %s and owner_id = %s and project_id = %s
                  and kind in ('source_archive'::public.artifact_kind, 'source_raster'::public.artifact_kind, 'metadata'::public.artifact_kind)
                  and status = 'available'::public.artifact_status
                order by created_at
                """,
                (task["scene_id"], task["owner_id"], task["project_id"]),
            )
            return [dict(row) for row in cursor.fetchall()]

    def scene(self, task: dict[str, Any]) -> dict[str, Any]:
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute("select * from public.scenes where id = %s and owner_id = %s", (task["scene_id"], task["owner_id"]))
            row = self._one(cursor)
            if row is None:
                raise UserFacingTaskError("SCENE_NOT_FOUND", "Scene no longer exists.")
            return row

    def upsert_artifact(
        self, task: dict[str, Any], *, kind: str, logical_key: str, storage_bucket: str,
        storage_key: str, content_type: str, size_bytes: int, checksum_sha256: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                insert into public.scene_artifacts (
                  owner_id, project_id, scene_id, kind, status, storage_bucket, storage_key,
                  content_type, size_bytes, checksum_sha256, metadata, logical_key, producer_job_id
                ) values (%s, %s, %s, %s::public.artifact_kind, 'available'::public.artifact_status,
                  %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (scene_id, logical_key) where logical_key is not null do update
                  set kind = excluded.kind, status = 'available'::public.artifact_status,
                      storage_bucket = excluded.storage_bucket, storage_key = excluded.storage_key,
                      content_type = excluded.content_type, size_bytes = excluded.size_bytes,
                      checksum_sha256 = excluded.checksum_sha256, metadata = excluded.metadata,
                      producer_job_id = excluded.producer_job_id
                returning *
                """,
                (task["owner_id"], task["project_id"], task["scene_id"], kind, storage_bucket,
                 storage_key, content_type, size_bytes, checksum_sha256, Jsonb(metadata or {}), logical_key,
                 task["processing_job_id"]),
            )
            return self._one(cursor) or {}

    def update_scene_metadata(self, task: dict[str, Any], metadata: dict[str, Any]) -> None:
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                update public.scenes
                set metadata = coalesce(metadata, '{}'::jsonb) || %s,
                    sensor = coalesce(%s, sensor),
                    acquisition_time = coalesce(%s::timestamptz, acquisition_time),
                    polarizations = case when cardinality(%s::text[]) > 0 then %s::text[] else polarizations end
                where id = %s and owner_id = %s
                """,
                (Jsonb(metadata), metadata.get("sensor"), metadata.get("acquisition_date"),
                 metadata.get("polarization", []), metadata.get("polarization", []), task["scene_id"], task["owner_id"]),
            )

    def artifact_by_logical_key(self, task: dict[str, Any], logical_key: str) -> dict[str, Any] | None:
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                select * from public.scene_artifacts
                where scene_id = %s and owner_id = %s and logical_key = %s
                  and status = 'available'::public.artifact_status
                limit 1
                """,
                (task["scene_id"], task["owner_id"], logical_key),
            )
            return self._one(cursor)

    def upsert_evidence_record(
        self, task: dict[str, Any], *, summary: str | None, facts: list[dict[str, Any]],
        metadata: dict[str, Any], model_name: str | None, model_version: str | None,
    ) -> None:
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "update public.scene_evidence_records set is_current = false, status = 'superseded'::public.evidence_record_status where scene_id = %s and is_current",
                (task["scene_id"],),
            )
            cursor.execute(
                """
                insert into public.scene_evidence_records (
                  owner_id, project_id, scene_id, record_version, status, is_current,
                  summary, facts, metadata, model_name, model_version
                ) values (
                  %s, %s, %s,
                  coalesce((select max(record_version) + 1 from public.scene_evidence_records where scene_id = %s), 1),
                  'ready'::public.evidence_record_status, true, %s, %s, %s, %s, %s
                )
                """,
                (task["owner_id"], task["project_id"], task["scene_id"], task["scene_id"], summary,
                 Jsonb(facts), Jsonb(metadata), model_name, model_version),
            )

    def upsert_patch(
        self, task: dict[str, Any], *, patch_id: UUID, patch_key: str, row_start: int, col_start: int,
        patch_size: int, source_artifact_id: str | UUID | None, preview_artifact_id: str | UUID | None = None,
    ) -> None:
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                insert into public.patches (
                  id, qdrant_point_id, owner_id, project_id, scene_id, source_artifact_id,
                  preview_artifact_id, row_start, row_end, col_start, col_end, patch_size,
                  status, patch_key
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          'pending'::public.patch_status, %s)
                on conflict (scene_id, patch_key) where patch_key is not null do update
                  set preview_artifact_id = coalesce(excluded.preview_artifact_id, public.patches.preview_artifact_id),
                      source_artifact_id = coalesce(excluded.source_artifact_id, public.patches.source_artifact_id)
                """,
                (patch_id, patch_id, task["owner_id"], task["project_id"], task["scene_id"], source_artifact_id,
                 preview_artifact_id, row_start, row_start + patch_size, col_start, col_start + patch_size,
                 patch_size, patch_key),
            )

    def mark_patches_ready(self, task: dict[str, Any], *, embedding_artifact_id: str | UUID, model_name: str, model_version: str) -> None:
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                update public.patches
                set status = 'ready'::public.patch_status, embedding_artifact_id = %s,
                    model_name = %s, model_version = %s
                where scene_id = %s and owner_id = %s and status = 'pending'::public.patch_status
                """,
                (embedding_artifact_id, model_name, model_version, task["scene_id"], task["owner_id"]),
            )

    def artifacts_for_cleanup(self, task: dict[str, Any], *, include_sources: bool) -> list[dict[str, Any]]:
        with self.transaction() as connection, connection.cursor() as cursor:
            kinds_clause = "" if include_sources else "and kind not in ('source_archive'::public.artifact_kind, 'source_raster'::public.artifact_kind, 'metadata'::public.artifact_kind)"
            cursor.execute(
                f"""
                select * from public.scene_artifacts
                where scene_id = %s and owner_id = %s and status <> 'deleted'::public.artifact_status
                {kinds_clause}
                """,
                (task["scene_id"], task["owner_id"]),
            )
            return [dict(row) for row in cursor.fetchall()]

    def mark_artifacts_deleted(self, artifact_ids: list[str | UUID]) -> None:
        if not artifact_ids:
            return
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "update public.scene_artifacts set status = 'deleted'::public.artifact_status where id = any(%s::uuid[])",
                ([str(value) for value in artifact_ids],),
            )

    def clear_derived_scene_records(self, task: dict[str, Any]) -> None:
        """Make cancelled scenes non-searchable even before a delete cascades."""
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "update public.patches set status = 'deleted'::public.patch_status where scene_id = %s and owner_id = %s",
                (task["scene_id"], task["owner_id"]),
            )
            cursor.execute(
                """
                update public.scene_evidence_records
                set status = 'superseded'::public.evidence_record_status, is_current = false
                where scene_id = %s and owner_id = %s and status = 'ready'::public.evidence_record_status
                """,
                (task["scene_id"], task["owner_id"]),
            )

    def cleanup_scene_is_ready(self, task: dict[str, Any]) -> bool:
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                select not exists (
                  select 1 from public.processing_jobs
                  where scene_id = %s and id <> %s and kind = 'process_scene'::public.processing_job_kind
                    and status not in ('ready'::public.processing_job_status, 'failed'::public.processing_job_status, 'cancelled'::public.processing_job_status)
                ) as ready
                """,
                (task["scene_id"], task["processing_job_id"]),
            )
            return bool(self._one(cursor)["ready"])

    def delete_scene_row(self, task: dict[str, Any]) -> None:
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute("delete from public.scenes where id = %s and owner_id = %s", (task["scene_id"], task["owner_id"]))

    def _fail_task_locked(self, cursor: Any, task: dict[str, Any], code: str, detail: str) -> None:
        cursor.execute(
            """
            update public.processing_job_tasks
            set status = 'failed'::public.processing_task_status, finished_at = now(),
                locked_at = null, locked_by = null, error_code = %s, error_detail = left(%s, 500)
            where id = %s
            """,
            (code, detail, task["id"]),
        )
        cursor.execute(
            """
            update public.processing_jobs
            set status = 'failed'::public.processing_job_status, finished_at = now(),
                error_code = %s, error_detail = left(%s, 500)
            where id = %s
            """,
            (code, detail, task["processing_job_id"]),
        )
        cursor.execute("update public.scenes set status = 'failed'::public.scene_status, failure_code = %s, failure_detail = left(%s, 500) where id = %s and status <> 'deleting'::public.scene_status", (code, detail, task["scene_id"]))
        self._insert_event_locked(cursor, task, status="failed", stage=task["stage"],
                                  progress=self._job_progress(cursor, task["processing_job_id"]),
                                  attempt=int(task.get("attempt") or 0), event_type="task_failed",
                                  error_code=code, detail={"message": detail})

    def _job_progress(self, cursor: Any, job_id: str | UUID) -> int:
        cursor.execute("select progress from public.processing_jobs where id = %s", (job_id,))
        row = self._one(cursor)
        return int(row["progress"]) if row else 0

    @staticmethod
    def _insert_event_locked(
        cursor: Any, task: dict[str, Any], *, status: str, stage: str, progress: int,
        attempt: int, event_type: str, detail: dict[str, Any], error_code: str | None = None,
    ) -> None:
        cursor.execute(
            """
            insert into public.processing_job_events (
              processing_job_id, task_id, owner_id, project_id, scene_id, status, stage,
              progress, attempt, event_type, error_code, detail
            ) values (%s, %s, %s, %s, %s, %s::public.processing_job_status,
              %s::public.processing_job_stage, %s, %s, %s, %s, %s)
            """,
            (task["processing_job_id"], task.get("id"), task["owner_id"], task["project_id"], task["scene_id"],
             status, stage, progress, attempt, event_type, error_code, Jsonb(detail)),
        )
