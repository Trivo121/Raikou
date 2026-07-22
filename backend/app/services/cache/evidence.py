"""M5 tenant-scoped Redis cache helpers.

These helpers deliberately cache only derived, bounded values.  They never
store database rows, object payloads, signed URLs, bearer tokens, raw query
text, or streamed model output.  PostgreSQL/S3 remain authoritative and every
cache key carries a validated owner/project boundary.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from app.core.config import settings
from app.services.cache.redis import get_async_redis_client, get_redis_client, get_redis_keyspace

logger = logging.getLogger(__name__)

_INDEX_NAMESPACE = "m5-cache-index"
_VERSION_NAMESPACE = "m5-cache-version"
_M5_NAMESPACES = {
    "m5-query-embedding",
    "m5-retrieval",
    "m5-rag-context",
}


def normalize_query(value: str) -> str:
    """Canonicalize a query for a digest-only cache key, never for display."""
    return " ".join(value.casefold().split())


def normalize_filters(value: Mapping[str, Any]) -> str:
    """Return a stable serialization without putting it verbatim in Redis keys."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def m5_cache_key(
    namespace: str,
    *,
    owner_id: str,
    project_id: str,
    normalized_query_text: str,
    normalized_filter_values: Mapping[str, Any],
    scene_id: str | None,
    model_version: str,
    index_version: str,
) -> str:
    """Build one M5 key after ownership and scope validation has completed."""
    if namespace not in _M5_NAMESPACES:
        raise ValueError("Unknown M5 cache namespace")
    return get_redis_keyspace().tenant_query_cache_key(
        namespace,
        owner_id,
        project_id,
        normalized_query=normalized_query_text,
        normalized_filters=normalize_filters(normalized_filter_values),
        model_version=model_version,
        index_version=index_version,
        scene_id=scene_id,
    )


def _project_index_key(*, owner_id: str, project_id: str) -> str:
    # A project index is intentionally broader than a scene cache key. Any
    # scene lifecycle/evidence change can affect project-wide ranking/context,
    # so invalidating the project index is safer than trying to infer which
    # free-text requests included that scene.
    return get_redis_keyspace().tenant_cache_key(
        _INDEX_NAMESPACE,
        owner_id,
        project_id,
        "entries",
    )


def _project_version_key(*, owner_id: str, project_id: str) -> str:
    """Keep a project-local generation outside query-derived cache values."""
    return get_redis_keyspace().tenant_cache_key(
        _VERSION_NAMESPACE,
        owner_id,
        project_id,
        "generation",
    )


async def get_project_evidence_cache_generation(*, owner_id: str, project_id: str) -> str:
    """Read the current invalidation generation after ownership validation.

    Callers incorporate this into their index-version cache component.  A
    lifecycle change increments the generation atomically with deletion of
    indexed entries, so an old key can never be reused by a later search.
    """
    if not settings.REDIS_URL:
        return "cache-disabled"
    try:
        redis = get_async_redis_client()
        version_key = _project_version_key(owner_id=owner_id, project_id=project_id)
        raw = await redis.get(version_key)
        if raw is None:
            await redis.set(version_key, b"0", nx=True)
            raw = await redis.get(version_key)
        if isinstance(raw, bytes):
            raw = raw.decode("ascii", errors="ignore")
        value = str(raw or "0")
        return value if value.isdigit() else "0"
    except Exception:
        # Cache reads then become misses; do not silently use an unversioned
        # key after a lifecycle invalidation failure.
        logger.info("M5 cache generation unavailable", exc_info=True)
        return "cache-unavailable"


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


async def get_json_cache(key: str) -> Any | None:
    """Best-effort read. Redis outages must never deny an authorized request."""
    if not settings.REDIS_URL:
        return None
    try:
        raw = await get_async_redis_client().get(key)
        if raw is None:
            return None
        return json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        # Malformed values are treated as misses; delete only the one key when
        # possible so a transient old deployment cannot keep poisoning reads.
        try:
            await get_async_redis_client().delete(key)
        except Exception:
            pass
        return None
    except Exception:
        logger.info("M5 cache read unavailable", exc_info=True)
        return None


async def set_json_cache(
    key: str,
    value: Any,
    *,
    ttl_seconds: int,
    owner_id: str,
    project_id: str,
) -> None:
    """Best-effort write of a derived value and its project invalidation tag."""
    if not settings.REDIS_URL:
        return
    try:
        redis = get_async_redis_client()
        index_key = _project_index_key(owner_id=owner_id, project_id=project_id)
        encoded = _json_bytes(value)
        # ``MULTI`` is unnecessary for correctness: a missing index merely
        # produces a harmless cache miss after expiry, never a cross-tenant
        # response. Keep each operation simple across Redis/ElastiCache modes.
        await redis.set(key, encoded, ex=ttl_seconds)
        await redis.sadd(index_key, key)
        await redis.expire(index_key, settings.M5_CACHE_INDEX_TTL_SECONDS)
    except Exception:
        logger.info("M5 cache write unavailable", exc_info=True)


async def invalidate_project_evidence_cache(*, owner_id: str, project_id: str) -> None:
    """Remove every derived M5 value affected by a project/scene change."""
    if not settings.REDIS_URL:
        return
    try:
        redis = get_async_redis_client()
        index_key = _project_index_key(owner_id=owner_id, project_id=project_id)
        version_key = _project_version_key(owner_id=owner_id, project_id=project_id)
        members = await redis.smembers(index_key)
        keys = [member.decode("utf-8") if isinstance(member, bytes) else str(member) for member in members]
        pipeline = redis.pipeline(transaction=True)
        # Advance the generation before any next request can use a cache key.
        # The transaction also clears currently indexed entries for memory
        # hygiene; correctness does not depend on deleting every old key.
        pipeline.incr(version_key)
        if keys:
            pipeline.delete(*keys)
        pipeline.delete(index_key)
        await pipeline.execute()
    except Exception:
        logger.info("M5 cache invalidation unavailable", exc_info=True)


def invalidate_project_evidence_cache_sync(*, owner_id: str, project_id: str) -> None:
    """Worker-safe invalidation counterpart for M3's synchronous stages."""
    if not settings.REDIS_URL:
        return
    try:
        redis = get_redis_client()
        index_key = _project_index_key(owner_id=owner_id, project_id=project_id)
        version_key = _project_version_key(owner_id=owner_id, project_id=project_id)
        members = redis.smembers(index_key)
        keys = [member.decode("utf-8") if isinstance(member, bytes) else str(member) for member in members]
        pipeline = redis.pipeline(transaction=True)
        pipeline.incr(version_key)
        if keys:
            pipeline.delete(*keys)
        pipeline.delete(index_key)
        pipeline.execute()
    except Exception:
        logger.info("M5 worker cache invalidation unavailable", exc_info=True)
