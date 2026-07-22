"""Application configuration and runtime validation.

All environment-backed settings live here so routes and services do not read
``os.environ`` directly.  The application intentionally remains runnable in a
local development shell without cloud credentials, but production startup
fails fast when a required integration is not configured.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    PROJECT_NAME: str = "Raikou SAR API"
    API_V1_STR: str = "/api/v1"
    ENVIRONMENT: Literal["development", "test", "staging", "production"] = "development"

    # HTTP / browser integration
    CORS_ORIGINS: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://localhost:5173"]
    )
    CORS_ALLOW_CREDENTIALS: bool = True
    # Session-ID routes predate the V1 ownership model. They are opt-in only
    # for local prototype compatibility until M3 moves ingestion/processing to
    # durable scene resources.
    ENABLE_LEGACY_SESSION_API: bool = False

    # Supabase is used as the authoritative user and domain-data store.  The
    # service key is intentionally backend-only and must never be sent to React.
    SUPABASE_URL: str | None = None
    SUPABASE_SERVICE_KEY: str | None = None
    # Workers and the outbox dispatcher use a direct PostgreSQL connection for
    # transactional leases (FOR UPDATE SKIP LOCKED). It is never exposed to
    # the browser and is not required by the request-serving API process.
    SUPABASE_DB_URL: str | None = None

    # Vector store
    QDRANT_URL: str = "http://localhost:6333"
    QDRANT_API_KEY: str | None = None
    QDRANT_COLLECTION: str = "sar_patches"

    # Redis backs short-lived, tenant-scoped caches and the later processing
    # queue. It is intentionally optional for an unconfigured local shell,
    # but production must always provide a durable Redis endpoint.
    REDIS_URL: str | None = None
    REDIS_KEY_PREFIX: str = "raikou:v1"
    REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS: float = Field(default=2.0, gt=0, le=60)
    REDIS_SOCKET_TIMEOUT_SECONDS: float = Field(default=2.0, gt=0, le=60)

    # Public HTTP hardening. Source rasters never traverse the API—the browser
    # sends them directly to S3 using presigned part URLs—so control-plane
    # requests can stay deliberately small and bounded.
    MAX_API_REQUEST_BYTES: int = Field(default=4 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024)
    API_RATE_LIMIT_PER_MINUTE: int = Field(default=120, ge=1, le=10_000)
    UPLOAD_INITIATE_RATE_LIMIT_PER_MINUTE: int = Field(default=12, ge=1, le=1_000)
    METRICS_TOKEN: str | None = None
    LOG_JSON: bool = True

    # Object storage. M2 uses an S3-compatible API for both production S3 and
    # local MinIO; callers never need a separate filesystem code path.
    STORAGE_BACKEND: Literal["s3", "minio"] = "s3"
    STORAGE_BUCKET: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "STORAGE_BUCKET", "AWS_S3_BUCKET", "S3_BUCKET_NAME"
        ),
    )
    STORAGE_ENDPOINT_URL: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "STORAGE_ENDPOINT_URL", "S3_ENDPOINT_URL", "MINIO_ENDPOINT_URL"
        ),
    )
    STORAGE_REGION: str = Field(
        default="us-east-1",
        validation_alias=AliasChoices(
            "STORAGE_REGION", "AWS_REGION", "AWS_DEFAULT_REGION"
        ),
    )
    # ``None`` selects the safe backend default: path-style for MinIO and
    # virtual-hosted-style for AWS S3. Set the value explicitly to override it.
    STORAGE_FORCE_PATH_STYLE: bool | None = None
    STORAGE_SIGNED_URL_TTL_SECONDS: int = Field(default=900, ge=60, le=3600)
    # M4 preview grants are intentionally much shorter than upload-part
    # grants. They are created only after an owned artifact is selected and
    # are never retained in browser query caches.
    ARTIFACT_PREVIEW_TTL_SECONDS: int = Field(default=90, ge=30, le=300)
    M4_MAX_EVIDENCE_RECORD_BYTES: int = Field(default=2 * 1024 * 1024, ge=1_024, le=16 * 1024 * 1024)
    # ``auto`` uses native S3 part checksums and downgrades only an older
    # MinIO server that explicitly rejects them. ``server_verified`` is an
    # escape hatch for known-incompatible local MinIO installations.
    STORAGE_MULTIPART_CHECKSUM_MODE: Literal[
        "auto", "sha256", "server_verified"
    ] = "auto"
    STORAGE_ACCESS_KEY_ID: str | None = Field(
        default=None,
        validation_alias=AliasChoices("STORAGE_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID"),
    )
    STORAGE_SECRET_ACCESS_KEY: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "STORAGE_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY"
        ),
    )
    STORAGE_SESSION_TOKEN: str | None = Field(
        default=None,
        validation_alias=AliasChoices("STORAGE_SESSION_TOKEN", "AWS_SESSION_TOKEN"),
    )

    # M2 upload guardrails.  These limits are checked before signing and again
    # against completed object metadata; workers perform deeper SAR validation
    # in M3.  A 16 MiB part size stays well above S3's 5 MiB multipart minimum
    # while keeping even a 20 GiB archive far below the 10,000-part limit.
    # The signing lease is a minimum, not a whole-file deadline. Each signed
    # multipart URL remains short-lived, while plan expiry is size-aware so a
    # valid large upload is not restricted to one 15-minute window.
    UPLOAD_PLAN_TTL_SECONDS: int = Field(default=900, ge=60, le=86_400)
    UPLOAD_PLAN_MAX_TTL_SECONDS: int = Field(default=86_400, ge=300, le=172_800)
    UPLOAD_PLAN_MIN_BYTES_PER_SECOND: int = Field(default=512 * 1024, ge=64 * 1024)
    # Once completion begins, a longer lease protects the server-side object
    # verification and the atomic database finalization from the browser's
    # short-lived signing deadline. An expired lease is safely reclaimed
    # before another upload can be started for the same scene.
    UPLOAD_COMPLETION_LEASE_SECONDS: int = Field(default=7200, ge=300, le=86_400)
    UPLOAD_MULTIPART_PART_SIZE_BYTES: int = Field(
        default=16 * 1024 * 1024,
        ge=5 * 1024 * 1024,
        le=512 * 1024 * 1024,
    )
    UPLOAD_MAX_ARCHIVE_BYTES: int = Field(default=20 * 1024 * 1024 * 1024, ge=1)
    UPLOAD_MAX_RASTER_BYTES: int = Field(default=10 * 1024 * 1024 * 1024, ge=1)
    UPLOAD_MAX_METADATA_BYTES: int = Field(default=1 * 1024 * 1024, ge=1)
    UPLOAD_MAX_TOTAL_BYTES: int = Field(default=20 * 1024 * 1024 * 1024, ge=1)
    UPLOAD_MAX_ZIP_ENTRIES: int = Field(default=20_000, ge=1, le=100_000)
    # Read this much ZIP central-directory metadata at most before handing an
    # archive to Python's zipfile module. It prevents a crafted ZIP64 central
    # directory from consuming unbounded API memory or object-store egress.
    UPLOAD_MAX_ZIP_CENTRAL_DIRECTORY_BYTES: int = Field(
        default=32 * 1024 * 1024,
        ge=64 * 1024,
        le=256 * 1024 * 1024,
    )
    UPLOAD_MAX_ZIP_UNCOMPRESSED_BYTES: int = Field(
        default=80 * 1024 * 1024 * 1024,
        ge=1,
    )
    UPLOAD_MAX_ZIP_COMPRESSION_RATIO: float = Field(default=100.0, ge=1, le=10_000)
    # A short lease prevents concurrent API retries from publishing the same
    # durable outbox row. A later dispatcher can reclaim a crashed publisher.
    JOB_DISPATCH_LEASE_SECONDS: int = Field(default=60, ge=10, le=3600)
    # Redis publication retries use bounded exponential backoff. M2 status
    # polling can nudge a durable outbox row, but must not exhaust its retry
    # budget immediately while Redis is temporarily unavailable.
    JOB_DISPATCH_RETRY_BASE_SECONDS: int = Field(default=10, ge=1, le=3600)
    JOB_DISPATCH_RETRY_MAX_SECONDS: int = Field(default=300, ge=1, le=86_400)

    # M3 durable worker runtime. Queue streams deliberately have a different
    # namespace from cache keys so cache eviction can never remove work.
    M3_OUTBOX_POLL_SECONDS: float = Field(default=1.0, gt=0, le=60)
    M3_TASK_LEASE_SECONDS: int = Field(default=300, ge=30, le=86_400)
    M3_DISPATCH_LEASE_SECONDS: int = Field(default=60, ge=10, le=3600)
    M3_STREAM_BLOCK_MILLISECONDS: int = Field(default=5000, ge=100, le=60_000)
    M3_STREAM_CLAIM_IDLE_MILLISECONDS: int = Field(default=120_000, ge=10_000, le=86_400_000)
    M3_WORKER_SCRATCH_ROOT: str = "/tmp/raikou-workers"
    M3_CPU_WORKER_CONCURRENCY: int = Field(default=2, ge=1, le=32)
    # One process is scheduled per physical GPU. This is a hard per-process
    # semaphore, not an aspirational setting; raise only after VRAM profiling.
    M3_GPU_INFERENCE_CONCURRENCY: int = Field(default=1, ge=1, le=8)
    M3_TASK_MAX_ATTEMPTS: int = Field(default=5, ge=1, le=20)
    M3_QDRANT_BATCH_SIZE: int = Field(default=64, ge=1, le=512)
    M3_MAX_PATCH_PREVIEWS: int = Field(default=128, ge=0, le=10_000)
    M3_VLLM_TIMEOUT_SECONDS: int = Field(default=45, ge=1, le=600)
    M3_VLLM_MAX_IMAGES: int = Field(default=4, ge=1, le=16)
    M3_VLLM_MAX_TOKENS: int = Field(default=512, ge=1, le=4096)
    M3_VLLM_MAX_IMAGE_BYTES: int = Field(default=4 * 1024 * 1024, ge=1)

    # M5 keeps every cache entry below a validated tenant/project boundary.
    # Redis remains an optimisation only: a cache miss always rebuilds from
    # the authoritative database, object store, and scoped Qdrant search.
    M5_QUERY_EMBEDDING_TTL_SECONDS: int = Field(default=3600, ge=60, le=86_400)
    M5_RETRIEVAL_TTL_SECONDS: int = Field(default=300, ge=10, le=3600)
    M5_RAG_CONTEXT_TTL_SECONDS: int = Field(default=120, ge=10, le=3600)
    M5_CACHE_INDEX_TTL_SECONDS: int = Field(default=7200, ge=60, le=86_400)
    M5_SEARCH_MAX_RESULTS: int = Field(default=10, ge=1, le=25)
    M5_WEAK_RETRIEVAL_SCORE: float = Field(default=0.16, ge=-1.0, le=1.0)
    M5_MAX_QUERY_CHARS: int = Field(default=1000, ge=64, le=10_000)
    M5_MAX_HISTORY_MESSAGES: int = Field(default=12, ge=1, le=50)
    M5_MAX_HISTORY_CHARS: int = Field(default=6000, ge=500, le=40_000)
    M5_MAX_CONTEXT_CHARS: int = Field(default=12_000, ge=1_000, le=80_000)
    M5_MAX_CONTEXT_FACTS: int = Field(default=50, ge=1, le=500)
    M5_MAX_OVERVIEWS_PER_PROMPT: int = Field(default=2, ge=0, le=8)
    M5_MAX_PATCH_IMAGES_PER_PROMPT: int = Field(default=4, ge=0, le=12)
    # Scene descriptions use one overview plus these deliberate quadrant crops.
    # They are generated transiently in the RAG request from the authorized
    # overview; no new ingestion artifact or workflow is required.
    M5_SCENE_QUADRANT_SAMPLES: int = Field(default=4, ge=0, le=4)
    M5_SCENE_QUADRANT_MAX_PIXELS: int = Field(default=512, ge=128, le=2048)
    M5_MAX_IMAGE_BYTES: int = Field(default=4 * 1024 * 1024, ge=1, le=16 * 1024 * 1024)
    M5_OUTPUT_MAX_TOKENS: int = Field(default=700, ge=32, le=4096)
    M5_GENERATION_TIMEOUT_SECONDS: int = Field(default=75, ge=5, le=600)
    # Change this deliberately when the Qdrant payload/index contract changes;
    # versioned keys then bypass stale retrieval/context cache entries.
    M5_QDRANT_INDEX_VERSION: str = "v1"

    # Model / worker compatibility settings retained from the existing pipeline.
    VLLM_BASE_URL: str = "http://localhost:8001/v1"
    SARCHAT_MODEL_ID: str = "/models/SARChat-InternVL2.5-2B"
    # The HTTP control plane must be able to boot without a GPU/model image.
    # Processing/search runtimes opt in explicitly when they need SARCLIP.
    PRELOAD_SARCLIP: bool = False
    SARCLIP_CHECKPOINT_PATH: str = "/home/ubuntu/backend/models/SARVLM/SARCLIP-GeoRS-ViT-L-14.pt"
    SARCLIP_DEVICE: str | None = None
    SARCLIP_BATCH_SIZE: int = Field(default=8, ge=1)
    SESSION_ROOT: str | None = Field(
        default=None,
        validation_alias=AliasChoices("SESSION_ROOT", "RAIKOU_SESSION_ROOT"),
    )
    SESSION_TTL_HOURS: int = Field(
        default=168,
        ge=1,
        validation_alias=AliasChoices("SESSION_TTL_HOURS", "RAIKOU_SESSION_TTL_HOURS"),
    )
    SESSION_CLEANUP_INTERVAL_SECONDS: int = Field(default=900, ge=1)
    INGEST_UPLOAD_DIR: str = "./data/uploads"
    SARCLIP_MODEL_NAME: str = "SARVLM"
    SARCLIP_MODEL_VERSION: str = "SARCLIP-GeoRS-ViT-L-14"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("API_V1_STR")
    @classmethod
    def api_prefix_must_start_with_slash(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("API_V1_STR must start with '/'.")
        return value.rstrip("/") or "/"

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: object) -> object:
        """Accept a JSON list or a simple comma-separated environment value."""
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return []
            if value.startswith("["):
                return json.loads(value)
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("REDIS_URL", mode="before")
    @classmethod
    def normalize_redis_url(cls, value: object) -> object:
        """Treat an empty environment value as an unconfigured local Redis."""
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("REDIS_URL")
    @classmethod
    def validate_redis_url(cls, value: str | None) -> str | None:
        if value is None:
            return value

        try:
            parsed = urlparse(value)
            # Accessing ``port`` performs stdlib validation instead of
            # deferring malformed values such as ``host:not-a-port`` until a
            # readiness request tries to open a Redis connection.
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("REDIS_URL contains an invalid host or port.") from exc
        if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
            raise ValueError(
                "REDIS_URL must be a redis:// or rediss:// URL with a host."
            )
        if parsed.path not in {"", "/"}:
            database = parsed.path.lstrip("/")
            if not database.isdigit() or "/" in database:
                raise ValueError(
                    "REDIS_URL database path must be a non-negative integer, for example '/0'."
                )
        return value

    @field_validator("REDIS_KEY_PREFIX")
    @classmethod
    def validate_redis_key_prefix(cls, value: str) -> str:
        normalized = value.strip().strip(":")
        if not normalized:
            raise ValueError("REDIS_KEY_PREFIX must not be empty.")
        if any(not segment or not re.fullmatch(r"[A-Za-z0-9_.-]+", segment) for segment in normalized.split(":")):
            raise ValueError(
                "REDIS_KEY_PREFIX may contain colon-separated letters, numbers, dots, underscores, and hyphens only."
            )
        return normalized

    @field_validator(
        "STORAGE_BUCKET",
        "STORAGE_ENDPOINT_URL",
        "STORAGE_ACCESS_KEY_ID",
        "STORAGE_SECRET_ACCESS_KEY",
        "STORAGE_SESSION_TOKEN",
        mode="before",
    )
    @classmethod
    def normalize_optional_storage_values(cls, value: object) -> object:
        """Treat blank storage settings as intentionally unconfigured."""
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("STORAGE_FORCE_PATH_STYLE", mode="before")
    @classmethod
    def normalize_optional_path_style(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("STORAGE_BUCKET")
    @classmethod
    def validate_storage_bucket(cls, value: str | None) -> str | None:
        if value is None:
            return value

        # Both AWS S3 and MinIO use DNS-compatible bucket names. Keeping this
        # narrow also prevents accidentally treating an endpoint or path as a
        # bucket name.
        if not re.fullmatch(
            r"(?=.{3,63}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])", value
        ):
            raise ValueError("STORAGE_BUCKET must be a DNS-compatible bucket name.")
        if ".." in value or ".-" in value or "-." in value:
            raise ValueError("STORAGE_BUCKET contains an invalid dot or hyphen sequence.")
        if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", value):
            raise ValueError("STORAGE_BUCKET must not be formatted as an IP address.")
        return value

    @field_validator("STORAGE_ENDPOINT_URL")
    @classmethod
    def validate_storage_endpoint_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            parsed = urlparse(value)
            # Trigger stdlib validation of malformed values such as
            # ``http://minio:not-a-port`` during configuration parsing.
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("STORAGE_ENDPOINT_URL contains an invalid host or port.") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(
                "STORAGE_ENDPOINT_URL must be an http:// or https:// URL with a host."
            )
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "STORAGE_ENDPOINT_URL must not include credentials, a query string, or a fragment."
            )
        return value.rstrip("/")

    @field_validator("STORAGE_REGION")
    @classmethod
    def validate_storage_region(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or not re.fullmatch(r"[A-Za-z0-9-]+", normalized):
            raise ValueError("STORAGE_REGION must contain only letters, numbers, and hyphens.")
        return normalized

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    def startup_issues(self) -> list[str]:
        """Return missing configuration that prevents the V1 API being ready."""
        issues: list[str] = []
        if not self.SUPABASE_URL:
            issues.append("SUPABASE_URL is not configured")
        if not self.SUPABASE_SERVICE_KEY:
            issues.append("SUPABASE_SERVICE_KEY is not configured")
        if not self.QDRANT_URL:
            issues.append("QDRANT_URL is not configured")
        if not self.CORS_ORIGINS:
            issues.append("CORS_ORIGINS must contain at least one frontend origin")
        if self.is_production:
            if not self.REDIS_URL:
                issues.append("REDIS_URL is not configured")
            if not self.STORAGE_BUCKET:
                issues.append("STORAGE_BUCKET is not configured")
            if self.STORAGE_BACKEND == "minio" and not self.STORAGE_ENDPOINT_URL:
                issues.append(
                    "STORAGE_ENDPOINT_URL is required when STORAGE_BACKEND=minio"
                )
            if (
                self.STORAGE_ENDPOINT_URL
                and urlparse(self.STORAGE_ENDPOINT_URL).scheme != "https"
            ):
                issues.append(
                    "STORAGE_ENDPOINT_URL must use HTTPS in production because browsers upload directly to it"
                )
            if bool(self.STORAGE_ACCESS_KEY_ID) != bool(self.STORAGE_SECRET_ACCESS_KEY):
                issues.append(
                    "STORAGE_ACCESS_KEY_ID and STORAGE_SECRET_ACCESS_KEY must be configured together"
                )
            normalized_origins = {origin.rstrip("/").lower() for origin in self.CORS_ORIGINS}
            non_local_origins = {
                origin
                for origin in normalized_origins
                if urlparse(origin).hostname not in {"localhost", "127.0.0.1", "::1"}
            }
            if "*" in normalized_origins:
                issues.append("CORS_ORIGINS cannot contain '*' in production")
            if not non_local_origins:
                issues.append("CORS_ORIGINS must contain a non-local frontend origin in production")
            if len(non_local_origins) != len(normalized_origins):
                issues.append("CORS_ORIGINS cannot contain local development origins in production")
        if self.is_production and self.ENABLE_LEGACY_SESSION_API:
            issues.append(
                "ENABLE_LEGACY_SESSION_API must be false in production because session IDs are not tenant-scoped"
            )
        return issues

    def validate_startup(self) -> list[str]:
        """Validate configuration and fail closed in production.

        Development can start without remote services so contributors can work
        on isolated portions of the app.  ``/readyz`` still reports such an app
        as unavailable until these dependencies are configured.
        """
        issues = self.startup_issues()
        if issues and self.is_production:
            details = "; ".join(issues)
            raise RuntimeError(f"Invalid production configuration: {details}")
        return issues

    def require_supabase(self) -> tuple[str, str]:
        if not self.SUPABASE_URL or not self.SUPABASE_SERVICE_KEY:
            raise RuntimeError(
                "Supabase is not configured. Set SUPABASE_URL and SUPABASE_SERVICE_KEY."
            )
        # Some users copy the REST endpoint from the dashboard.  The Python
        # client expects the project root URL.
        url = self.SUPABASE_URL.rstrip("/").replace("/rest/v1", "")
        return url, self.SUPABASE_SERVICE_KEY

    def require_worker_database_url(self) -> str:
        if not self.SUPABASE_DB_URL:
            raise RuntimeError(
                "SUPABASE_DB_URL is required by the M3 dispatcher and workers."
            )
        return self.SUPABASE_DB_URL

    def require_redis_url(self) -> str:
        """Return the configured backend Redis endpoint or raise a clear error."""
        if not self.REDIS_URL:
            raise RuntimeError(
                "Redis is not configured. Set REDIS_URL to enable backend caching or workers."
            )
        return self.REDIS_URL

    @property
    def storage_force_path_style(self) -> bool:
        """Return the explicit setting or the backend-safe addressing default."""
        if self.STORAGE_FORCE_PATH_STYLE is not None:
            return self.STORAGE_FORCE_PATH_STYLE
        return self.STORAGE_BACKEND == "minio"

    def require_object_storage(self) -> tuple[str, str]:
        """Return the configured storage backend and bucket, or fail clearly.

        Credential discovery is deliberately left to boto3 when explicit keys
        are not supplied, so deployed AWS workloads can use IAM roles.
        """
        if not self.STORAGE_BUCKET:
            raise RuntimeError(
                "Object storage is not configured. Set STORAGE_BUCKET, AWS_S3_BUCKET, or S3_BUCKET_NAME."
            )
        if self.STORAGE_BACKEND == "minio" and not self.STORAGE_ENDPOINT_URL:
            raise RuntimeError(
                "STORAGE_ENDPOINT_URL is required when STORAGE_BACKEND=minio."
            )
        if bool(self.STORAGE_ACCESS_KEY_ID) != bool(self.STORAGE_SECRET_ACCESS_KEY):
            raise RuntimeError(
                "Set STORAGE_ACCESS_KEY_ID and STORAGE_SECRET_ACCESS_KEY together."
            )
        if self.STORAGE_SESSION_TOKEN and not self.STORAGE_ACCESS_KEY_ID:
            raise RuntimeError(
                "STORAGE_SESSION_TOKEN requires explicit storage access credentials."
            )
        if self.UPLOAD_PLAN_TTL_SECONDS > self.UPLOAD_PLAN_MAX_TTL_SECONDS:
            raise RuntimeError(
                "UPLOAD_PLAN_TTL_SECONDS must not exceed UPLOAD_PLAN_MAX_TTL_SECONDS."
            )
        if self.JOB_DISPATCH_RETRY_BASE_SECONDS > self.JOB_DISPATCH_RETRY_MAX_SECONDS:
            raise RuntimeError(
                "JOB_DISPATCH_RETRY_BASE_SECONDS must not exceed JOB_DISPATCH_RETRY_MAX_SECONDS."
            )
        if self.UPLOAD_MAX_TOTAL_BYTES < self.UPLOAD_MAX_METADATA_BYTES:
            raise RuntimeError("UPLOAD_MAX_TOTAL_BYTES must allow the metadata file limit.")
        if self.UPLOAD_MAX_TOTAL_BYTES < min(
            self.UPLOAD_MAX_ARCHIVE_BYTES,
            self.UPLOAD_MAX_RASTER_BYTES,
        ):
            raise RuntimeError(
                "UPLOAD_MAX_TOTAL_BYTES must allow at least one source archive or raster."
            )
        return self.STORAGE_BACKEND, self.STORAGE_BUCKET


class VLLMConfig(BaseSettings):
    MAX_PATCHES_PER_PROMPT: int = Field(default=5, ge=1)
    MAX_OVERVIEWS_PER_PROMPT: int = Field(default=4, ge=1)
    OUTPUT_MAX_TOKENS: int = Field(default=1024, ge=1)
    NUM_CROPS: int = Field(default=4, ge=1)
    MAX_MODEL_LEN: int = Field(default=4096, ge=1)
    MAX_NUM_SEQS: int = Field(default=2, ge=1)
    LIMIT_MM_PER_PROMPT: str = "image=9"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_prefix="VLLM_",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
vllm_settings = VLLMConfig()
