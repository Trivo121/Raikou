"""FastAPI application entry point."""

from __future__ import annotations

import asyncio
import logging
import secrets

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool

from app.api.routes import evidence, jobs, projects, scenes, uploads, workspace
from app.core.config import settings
from app.core.middleware import ReleaseHardeningMiddleware, ResponseHeadersMiddleware
from app.core.observability import configure_logging, metrics
from app.services.cache.redis import close_async_redis_client, close_redis_client, get_async_redis_client
from app.services.database import get_supabase
from app.services.storage.object_store import get_object_storage
from app.services.storage.qdrant import QdrantStore

logger = logging.getLogger(__name__)

configure_logging(json_logs=settings.LOG_JSON)

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
)


@app.on_event("startup")
async def startup() -> None:
    """Validate the runtime before starting background work or loading models."""
    for issue in settings.validate_startup():
        logger.warning("Startup configuration issue: %s", issue)

    if settings.ENABLE_LEGACY_SESSION_API:
        # Keep the M1 identity/control plane independent from the legacy
        # raster/ML runtime. These imports are intentionally deferred until a
        # local developer explicitly opts into session-era endpoints.
        from app.services.models.sarclip_encoder import SARCLIPEncoder
        from app.services.session_cache import start_cleanup_loop

        if settings.PRELOAD_SARCLIP:
            await run_in_threadpool(SARCLIPEncoder.load_singleton)

        app.state.session_cleanup_task = asyncio.create_task(
            start_cleanup_loop(
                interval_seconds=settings.SESSION_CLEANUP_INTERVAL_SECONDS,
                ttl_hours=settings.SESSION_TTL_HOURS,
            )
        )


@app.on_event("shutdown")
async def shutdown() -> None:
    cleanup_task = getattr(app.state, "session_cleanup_task", None)
    if cleanup_task is not None:
        cleanup_task.cancel()

    await close_async_redis_client()
    close_redis_client()


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=settings.CORS_ALLOW_CREDENTIALS,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(ResponseHeadersMiddleware)
app.add_middleware(ReleaseHardeningMiddleware)

app.include_router(projects.router, prefix=f"{settings.API_V1_STR}/projects", tags=["projects"])
app.include_router(scenes.router, prefix=f"{settings.API_V1_STR}/scenes", tags=["scenes"])
app.include_router(uploads.router, prefix=f"{settings.API_V1_STR}/uploads", tags=["uploads"])
app.include_router(jobs.router, prefix=f"{settings.API_V1_STR}/jobs", tags=["jobs"])
app.include_router(workspace.router, prefix=settings.API_V1_STR, tags=["workspace"])
app.include_router(evidence.router, prefix=settings.API_V1_STR, tags=["evidence"])

# Session-oriented routes have no durable project ownership relationship yet.
# Keep them available to local prototype work only; the production V1 surface
# is the authenticated project/scene API above.
if settings.ENABLE_LEGACY_SESSION_API:
    from app.api.routes import ingestion, processing, search

    app.include_router(ingestion.router, prefix=f"{settings.API_V1_STR}/ingestion", tags=["legacy-ingestion"])
    app.include_router(processing.router, prefix=f"{settings.API_V1_STR}/processing", tags=["legacy-processing"])
    app.include_router(search.router, prefix=f"{settings.API_V1_STR}/search", tags=["legacy-search"])


@app.get("/healthz", tags=["health"])
async def healthz() -> dict[str, str]:
    """Liveness probe: the HTTP process is accepting requests."""
    return {"status": "ok"}


@app.get("/readyz", tags=["health"])
async def readyz() -> JSONResponse:
    """Readiness probe: required V1 dependencies and schema are usable."""
    issues = settings.startup_issues()
    if not issues:
        try:
            # This validates service-role credentials, M1's projects table,
            # and the side-effect-free M2/M3 migration/RPC probes without
            # transferring user data or creating a job.
            await run_in_threadpool(
                lambda: get_supabase().table("projects").select("id").limit(1).execute()
            )
            m2_probe = await run_in_threadpool(
                lambda: get_supabase().rpc("m2_upload_schema_ready").execute()
            )
            if getattr(m2_probe, "data", None) is not True:
                raise RuntimeError("M2 upload schema readiness probe returned false")
            m3_probe = await run_in_threadpool(
                lambda: get_supabase().rpc("m3_pipeline_schema_ready").execute()
            )
            if getattr(m3_probe, "data", None) is not True:
                raise RuntimeError("M3 pipeline schema readiness probe returned false")
            m4_probe = await run_in_threadpool(
                lambda: get_supabase().rpc("m4_workspace_schema_ready").execute()
            )
            if getattr(m4_probe, "data", None) is not True:
                raise RuntimeError("M4 workspace schema readiness probe returned false")
            m5_probe = await run_in_threadpool(
                lambda: get_supabase().rpc("m5_chat_schema_ready").execute()
            )
            if getattr(m5_probe, "data", None) is not True:
                raise RuntimeError("M5 evidence/chat schema readiness probe returned false")
        except Exception:
            logger.info("Supabase readiness check failed", exc_info=True)
            issues.append("Supabase is unavailable or the M1/M2/M3/M4/M5 schema is not applied")

        try:
            storage = await run_in_threadpool(get_object_storage)
            await run_in_threadpool(storage.check_bucket_access)
        except Exception:
            logger.info("Object storage readiness check failed", exc_info=True)
            issues.append("Object storage is unavailable or the M2 bucket is not configured")

        try:
            await run_in_threadpool(
                lambda: QdrantStore.get_instance().client.get_collections()
            )
        except Exception:
            logger.info("Qdrant readiness check failed", exc_info=True)
            issues.append("Qdrant is unavailable")

    # Redis is optional only when a local developer deliberately leaves
    # REDIS_URL unset. When it is configured (and always in production),
    # readiness requires a live connection rather than merely a valid URL.
    # Keep this check independent of other configuration issues so an
    # operator can see a broken Redis connection in the same readiness reply.
    if settings.REDIS_URL:
        try:
            await get_async_redis_client().ping()
        except Exception:
            logger.info("Redis readiness check failed", exc_info=True)
            issues.append("Redis is unavailable")

    status_code = 200 if not issues else 503
    return JSONResponse(
        status_code=status_code,
        content={"status": "ready" if not issues else "not_ready", "issues": issues},
    )


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics(x_metrics_token: str | None = Header(default=None)) -> PlainTextResponse:
    """Expose low-cardinality runtime metrics on the private API interface."""
    if settings.METRICS_TOKEN and not secrets.compare_digest(x_metrics_token or "", settings.METRICS_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid metrics token")
    return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")


@app.get("/", tags=["health"])
def root() -> dict[str, str]:
    return {"message": "Raikou SAR API is running."}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
