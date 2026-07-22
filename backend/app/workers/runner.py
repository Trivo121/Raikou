"""Redis Stream consumers for durable M3 CPU and GPU tasks."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import logging
from pathlib import Path
import shutil
import socket
import threading
import time
from uuid import uuid4
from typing import Any

from redis.exceptions import ResponseError

from app.core.config import settings
from app.services.cache.redis import get_redis_stream_client
from app.services.jobs.publisher import processing_task_stream_key
from app.workers.repository import RetryableTaskError, UserFacingTaskError, WorkerRepository
from app.workers.stages import M3Pipeline


logger = logging.getLogger(__name__)


def _decode(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


class M3Worker:
    def __init__(
        self,
        execution_class: str,
        *,
        repository: WorkerRepository | None = None,
        worker_id: str | None = None,
    ) -> None:
        if execution_class not in {"cpu", "gpu"}:
            raise ValueError("execution_class must be 'cpu' or 'gpu'.")
        self.execution_class = execution_class
        self.repository = repository or WorkerRepository()
        self.worker_id = worker_id or f"{execution_class}:{socket.gethostname()}:{uuid4().hex[:8]}"
        # Stream reads block for several seconds when the queue is idle, so
        # they need a timeout longer than the short request/cache client.
        self.redis = get_redis_stream_client()
        self.pipeline = M3Pipeline(self.repository)
        self.stream = processing_task_stream_key(execution_class)
        self.group = f"raikou-m3-{execution_class}-v1"
        self._gpu_semaphore = threading.BoundedSemaphore(settings.M3_GPU_INFERENCE_CONCURRENCY)

    def ensure_group(self) -> None:
        try:
            self.redis.xgroup_create(self.stream, self.group, id="0-0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def run_once(self) -> int:
        self.ensure_group()
        handled = 0
        for message_id, fields in self._reclaim_stale_entries():
            self._handle_message(message_id, fields)
            handled += 1
        messages = self.redis.xreadgroup(
            self.group,
            self.worker_id,
            {self.stream: ">"},
            count=1,
            block=settings.M3_STREAM_BLOCK_MILLISECONDS,
        )
        for _stream, entries in messages:
            for message_id, fields in entries:
                self._handle_message(message_id, fields)
                handled += 1
        return handled

    def run_forever(self) -> None:
        self.ensure_group()
        concurrency = settings.M3_CPU_WORKER_CONCURRENCY if self.execution_class == "cpu" else settings.M3_GPU_INFERENCE_CONCURRENCY
        logger.info("M3 %s worker %s started with safe concurrency=%s", self.execution_class, self.worker_id, concurrency)
        # GPU execution stays in this one process and is guarded below. CPU
        # scale-out is done with independent worker processes so task leases
        # remain transparent and no uncommitted state is shared in memory.
        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                raise
            except Exception:
                logger.exception("M3 %s worker iteration failed", self.execution_class)
                time.sleep(1.0)

    def _reclaim_stale_entries(self) -> list[tuple[str, dict[str, Any]]]:
        try:
            response = self.redis.xautoclaim(
                self.stream,
                self.group,
                self.worker_id,
                # A Redis stream entry must not be reclaimed until its durable
                # Postgres lease has also expired.  Reclaiming it earlier makes
                # ``claim_task`` correctly reject the still-leased task, but
                # the runner would then acknowledge its only stream entry.
                min_idle_time=max(
                    settings.M3_STREAM_CLAIM_IDLE_MILLISECONDS,
                    settings.M3_TASK_LEASE_SECONDS * 1000,
                ),
                start_id="0-0",
                count=10,
            )
        except ResponseError as exc:
            # Older Redis versions may not support XAUTOCLAIM. Normal worker
            # task leases still prevent data loss; operators can upgrade Redis
            # to enable automatic pending-entry recovery.
            logger.warning("Unable to reclaim stale M3 entries: %s", exc)
            return []
        entries = response[1] if isinstance(response, (tuple, list)) and len(response) >= 2 else []
        return [(_decode(message_id), dict(fields)) for message_id, fields in entries]

    def _handle_message(self, message_id: str | bytes, fields: dict[str, Any]) -> None:
        task_id = fields.get("task_id", fields.get(b"task_id"))
        if task_id is None:
            logger.error("Acknowledging malformed M3 stream entry %s", message_id)
            self.redis.xack(self.stream, self.group, message_id)
            return
        task = self.repository.claim_task(_decode(task_id), worker_id=self.worker_id, execution_class=self.execution_class)
        if task is None:
            self.redis.xack(self.stream, self.group, message_id)
            return
        settled = False
        try:
            if self.repository.task_cancel_requested(task) and task["job_kind"] == "process_scene":
                result = self.pipeline.cleanup_cancelled_job(task)
                self.repository.cancel_task_after_cleanup(task, worker_id=self.worker_id, detail=result)
                settled = True
            elif self.execution_class == "gpu":
                # This semaphore is the final per-process guard even if a
                # future runner adds concurrent stream handling.
                with self._gpu_semaphore:
                    outcome = self.pipeline.run(task)
                self.repository.complete_task(task, worker_id=self.worker_id, result=outcome.result,
                                              next_stage=outcome.next_stage, progress=outcome.progress)
                settled = True
            else:
                outcome = self.pipeline.run(task)
                self.repository.complete_task(task, worker_id=self.worker_id, result=outcome.result,
                                              next_stage=outcome.next_stage, progress=outcome.progress)
                settled = True
        except UserFacingTaskError as exc:
            logger.info("M3 task %s failed with user-facing code %s", task["id"], exc.code)
            self.repository.retry_or_fail_task(task, worker_id=self.worker_id, code=exc.code, detail=str(exc), retryable=False)
            settled = True
        except RetryableTaskError as exc:
            logger.warning("M3 task %s will retry: %s", task["id"], exc)
            self.repository.retry_or_fail_task(task, worker_id=self.worker_id, code="DEPENDENCY_UNAVAILABLE", detail=str(exc), retryable=True)
            settled = True
        except Exception:
            logger.exception("Unhandled M3 task failure: %s", task["id"])
            self.repository.retry_or_fail_task(task, worker_id=self.worker_id, code="INTERNAL_ERROR", detail="Unexpected worker failure.", retryable=True)
            settled = True
        finally:
            # Acknowledgement follows the durable transition above. A crash
            # before this point leaves a pending entry which XAUTOCLAIM can
            # safely replay; a crash after it is harmless because Postgres is
            # already authoritative.
            if settled:
                self.redis.xack(self.stream, self.group, message_id)
                self._remove_scratch(task)

    @staticmethod
    def _remove_scratch(task: dict[str, Any]) -> None:
        path = Path(settings.M3_WORKER_SCRATCH_ROOT) / str(task["processing_job_id"]) / str(task["id"])
        try:
            shutil.rmtree(path, ignore_errors=True)
        except OSError:
            logger.warning("Unable to clear disposable worker scratch directory %s", path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Raikou M3 worker")
    parser.add_argument("--class", dest="execution_class", choices=("cpu", "gpu"), required=True)
    parser.add_argument("--once", action="store_true", help="consume/reclaim one available task batch and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    worker = M3Worker(args.execution_class)
    try:
        if args.once:
            worker.run_once()
        else:
            worker.run_forever()
    finally:
        worker.redis.close()


if __name__ == "__main__":
    main()
