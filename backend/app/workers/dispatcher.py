"""Always-on M3 PostgreSQL-outbox to Redis-Stream dispatcher."""

from __future__ import annotations

import argparse
import logging
import socket
import time
from uuid import uuid4

from app.core.config import settings
from app.services.cache.redis import close_redis_client, get_redis_client
from app.services.jobs.publisher import processing_task_stream_key
from app.workers.repository import WorkerRepository


logger = logging.getLogger(__name__)


class OutboxDispatcher:
    def __init__(self, repository: WorkerRepository | None = None, worker_id: str | None = None) -> None:
        self.repository = repository or WorkerRepository()
        self.worker_id = worker_id or f"dispatcher:{socket.gethostname()}:{uuid4().hex[:8]}"
        self.redis = get_redis_client()

    def dispatch_once(self) -> int:
        bootstrapped = self.repository.bootstrap_m2_jobs()
        dispatches = self.repository.claim_task_dispatches(self.worker_id)
        published = 0
        for dispatch in dispatches:
            try:
                stream = processing_task_stream_key(str(dispatch["execution_class"]))
                message_id = self.redis.xadd(
                    stream,
                    {
                        "task_id": str(dispatch["task_id"]),
                        "job_id": str(dispatch["processing_job_id"]),
                        "schema": "raikou.m3.task.v1",
                    },
                )
                self.repository.settle_task_dispatch(dispatch["id"], self.worker_id)
                logger.debug("Published M3 task %s as stream entry %s", dispatch["task_id"], message_id)
                published += 1
            except Exception as exc:
                logger.warning("Unable to publish M3 task dispatch %s", dispatch["id"], exc_info=True)
                self.repository.settle_task_dispatch(dispatch["id"], self.worker_id, error=type(exc).__name__)
        return bootstrapped + published

    def run_forever(self) -> None:
        logger.info("M3 outbox dispatcher %s started", self.worker_id)
        while True:
            try:
                count = self.dispatch_once()
                if count == 0:
                    time.sleep(settings.M3_OUTBOX_POLL_SECONDS)
            except KeyboardInterrupt:
                raise
            except Exception:
                logger.exception("M3 dispatcher iteration failed")
                time.sleep(min(5.0, settings.M3_OUTBOX_POLL_SECONDS * 2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Raikou M3 outbox dispatcher")
    parser.add_argument("--once", action="store_true", help="dispatch available rows once and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    dispatcher = OutboxDispatcher()
    try:
        if args.once:
            dispatcher.dispatch_once()
        else:
            dispatcher.run_forever()
    finally:
        close_redis_client()


if __name__ == "__main__":
    main()
