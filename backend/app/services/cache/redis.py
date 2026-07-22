"""Backend-only Redis foundation for V1 caches and workers.

Redis is deliberately not a source of truth: PostgreSQL, S3, and Qdrant retain
their existing roles.  This module centralizes client construction and cache
key construction so future work cannot accidentally place one tenant's result
in another tenant's cache namespace.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from typing import TypeAlias
from urllib.parse import quote
from uuid import UUID

from redis import Redis
from redis.asyncio import Redis as AsyncRedis

from app.core.config import settings

RedisKeyPart: TypeAlias = str | int | UUID


def _key_part(value: RedisKeyPart) -> str:
    """Return a non-empty Redis-safe key part without changing its identity."""
    text = str(value).strip()
    if not text:
        raise ValueError("Redis key parts must not be empty.")
    # Keep UUIDs and conventional namespace tokens readable while escaping
    # delimiters or arbitrary query text supplied by future callers.
    return quote(text, safe="-_.")


def digest_cache_input(value: str | bytes) -> str:
    """Return a stable digest for potentially sensitive cache-key input.

    Query text, serialized normalized filters, and model/index versions belong
    in a cache key only as a digest.  This keeps sensitive prompt text out of
    Redis key listings while still making logically different requests map to
    different values.
    """
    raw_value = value.encode("utf-8") if isinstance(value, str) else value
    return sha256(raw_value).hexdigest()


@dataclass(frozen=True, slots=True)
class RedisKeyspace:
    """Build stable, namespaced Redis keys.

    Use :meth:`tenant_cache_key` for all user-derived cached values.  It makes
    ``owner_id`` and ``project_id`` mandatory and includes an optional scene
    boundary in the key, which mirrors V1's PostgreSQL and Qdrant ownership
    rules.
    """

    prefix: str

    def tenant_cache_key(
        self,
        namespace: str,
        owner_id: RedisKeyPart,
        project_id: RedisKeyPart,
        *parts: RedisKeyPart,
        scene_id: RedisKeyPart | None = None,
    ) -> str:
        """Build a tenant-safe cache key for a future query or artifact value.

        Values derived from user input must be passed in as
        :func:`digest_cache_input` output rather than raw text.
        """
        scoped_parts: list[RedisKeyPart] = [
            "cache",
            "owner",
            owner_id,
            "project",
            project_id,
        ]
        if scene_id is not None:
            scoped_parts.extend(("scene", scene_id))
        scoped_parts.extend(("namespace", namespace, *parts))
        return ":".join((self.prefix, *(_key_part(part) for part in scoped_parts)))

    def tenant_query_cache_key(
        self,
        namespace: str,
        owner_id: RedisKeyPart,
        project_id: RedisKeyPart,
        *,
        normalized_query: str,
        normalized_filters: str,
        model_version: str,
        index_version: str,
        scene_id: RedisKeyPart | None = None,
    ) -> str:
        """Build a scoped search/cache key without exposing raw query text.

        Callers must normalize the query and filters before invoking this
        method so equivalent requests produce the same digest.  The project
        boundary is intentionally required; V1 search never spans projects.
        """
        return self.tenant_cache_key(
            namespace,
            owner_id,
            project_id,
            "query",
            digest_cache_input(normalized_query),
            "filters",
            digest_cache_input(normalized_filters),
            "model",
            digest_cache_input(model_version),
            "index",
            digest_cache_input(index_version),
            scene_id=scene_id,
        )


@lru_cache
def get_redis_keyspace() -> RedisKeyspace:
    return RedisKeyspace(prefix=settings.REDIS_KEY_PREFIX)


def _client_options() -> dict[str, object]:
    return {
        "decode_responses": False,
        "socket_connect_timeout": settings.REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS,
        "socket_timeout": settings.REDIS_SOCKET_TIMEOUT_SECONDS,
        "health_check_interval": 30,
    }


@lru_cache
def get_redis_client() -> Redis:
    """Return the sync backend Redis client for worker/process integrations."""
    return Redis.from_url(settings.require_redis_url(), **_client_options())


def get_redis_stream_client() -> Redis:
    """Return a Redis client whose read timeout permits M3's blocking read.

    ``XREADGROUP BLOCK`` deliberately waits for up to
    :attr:`Settings.M3_STREAM_BLOCK_MILLISECONDS`.  Reusing the short cache
    client timeout here would interrupt an idle, healthy stream before Redis
    can return its normal empty response.
    """
    options = _client_options()
    options["socket_timeout"] = max(
        settings.REDIS_SOCKET_TIMEOUT_SECONDS,
        settings.M3_STREAM_BLOCK_MILLISECONDS / 1000 + 1,
    )
    return Redis.from_url(settings.require_redis_url(), **options)


@lru_cache
def get_async_redis_client() -> AsyncRedis:
    """Return the async backend Redis client for FastAPI request lifecycle work."""
    return AsyncRedis.from_url(settings.require_redis_url(), **_client_options())


def close_redis_client() -> None:
    """Close the cached sync client when the application shuts down."""
    if get_redis_client.cache_info().currsize:
        get_redis_client().close()
        get_redis_client.cache_clear()


async def close_async_redis_client() -> None:
    """Close the cached async client when the application shuts down."""
    if get_async_redis_client.cache_info().currsize:
        await get_async_redis_client().aclose()
        get_async_redis_client.cache_clear()
