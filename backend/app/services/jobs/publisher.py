"""Compatibility boundary between M2 upload completion and M3 dispatching.

M3 promotes the PostgreSQL outbox into the only work-publishing authority.
The former direct FastAPI-to-Redis call is intentionally a no-op so an API
restart or Redis outage cannot affect durable scene processing.
"""

from __future__ import annotations

from uuid import UUID

from app.core.config import settings


def processing_job_stream_key() -> str:
    """Return the non-cache Redis Stream name used only for job dispatch."""
    return f"{settings.REDIS_KEY_PREFIX}:queue:processing"


def processing_task_stream_key(execution_class: str) -> str:
    """Return the M3 stream for one execution class, never a cache key."""
    if execution_class not in {"cpu", "gpu"}:
        raise ValueError("M3 execution_class must be 'cpu' or 'gpu'.")
    return f"{settings.REDIS_KEY_PREFIX}:stream:processing:{execution_class}"


async def publish_processing_job(job_id: UUID | str) -> str:
    """Retain the M2 call surface while M3 owns all actual publication.

    M2 persists ``processing_job_dispatches`` in the same transaction as the
    upload completion. The M3 outbox dispatcher consumes that durable row and
    creates the first CPU task; FastAPI no longer publishes work to Redis.
    """
    del job_id
    return "m3-outbox"
