"""Optical (Sentinel-2) land-cover runs: availability, search, start, list, read.

An optical run is an ordinary scene (scenes.metadata.pipeline = 'optical') with
one Sentinel-2 acquisition of one to three dates on a fixed UTM grid. Search is
proxied here so provider credentials stay server-side; the imagery fetch and
the land-cover model run in M3 workers, so an API restart loses nothing.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
import json
import logging
from typing import Any, Callable, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from starlette.concurrency import run_in_threadpool

from app.api.deps import CurrentUser, get_current_user, resolve_owned_project, resolve_owned_scene
from app.core.config import settings
from app.schemas.optical import (
    OpticalBudgetRead,
    OpticalGridRead,
    OpticalModelRead,
    OpticalProductRead,
    OpticalProvidersRead,
    OpticalRunCreate,
    OpticalRunCreateResponse,
    OpticalRunDetail,
    OpticalRunMode,
    OpticalRunRead,
    OpticalSearchRequest,
    OpticalSearchResponse,
    OpticalSeasonRead,
    OpticalSimilarRequest,
    OpticalSimilarResponse,
    OpticalSimilarScope,
)
from app.services.acquisitions import copernicus, sentinel2
from app.services.database import get_supabase
from app.services.optical import similar as similar_service
from app.services.optical.artifacts import EMBEDDINGS_KEY, RECORD_KEY, WINDOWS_KEY, public_name
from app.services.optical.embeddings import LABEL as SIMILAR_LABEL
from app.services.optical.embeddings import MEANING as SIMILAR_MEANING
from app.services.optical.grid import GridError, OpticalGrid, estimate_pu, grid_for_box
from app.services.storage.object_store import get_object_storage
from app.services.storage.qdrant import QdrantStore

router = APIRouter()
logger = logging.getLogger(__name__)

PIPELINE = "optical"
PROJECT_NAME = "Optical land cover"
PROJECT_MARKER = "raikou:optical-workspace"
PROJECT_DESCRIPTION = f"Sentinel-2 land-cover runs from the Optical page. ({PROJECT_MARKER})"
SEASONS = [
    ("post_monsoon", "10-15", "11-30"),
    ("rabi", "01-15", "02-28"),
    ("summer", "04-01", "05-20"),
]
MAX_RECORD_BYTES = 2 * 1024 * 1024
REPROCESS_DETAIL = (
    "This run was made before similarity search was added. Reprocess it to add it; "
    "the stored imagery is reused, so no processing units are spent."
)


async def _execute(operation: Callable[[], Any], unavailable_detail: str) -> Any:
    try:
        return await run_in_threadpool(operation)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Optical database operation failed")
        raise HTTPException(status_code=503, detail=unavailable_detail) from None


def _rows(response: Any) -> list[dict[str, Any]]:
    data = getattr(response, "data", None)
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def _first(response: Any) -> dict[str, Any] | None:
    rows = _rows(response)
    return rows[0] if rows else None


def _reason() -> str | None:
    if not settings.OPTICAL_ENABLED:
        return "disabled"
    if not settings.COPERNICUS_ENABLED or not settings.copernicus_configured:
        return "not_configured"
    return None


def _unavailable(reason: str) -> HTTPException:
    details = {
        "disabled": "Optical land-cover runs are switched off on this server.",
        "not_configured": "Copernicus access is not configured on this server.",
        "schema_not_applied": "Optical runs are unavailable because their database migration has not been applied.",
        "model_unavailable": "The optical land-cover model is not installed on this server.",
    }
    return HTTPException(status_code=503, detail=details.get(reason, "Optical runs are unavailable."))


def _provider_error(exc: Exception) -> NoReturn:
    if isinstance(exc, copernicus.CopernicusAuthError):
        logger.error("Copernicus rejected the configured OAuth client", exc_info=True)
        raise HTTPException(status_code=503, detail="Copernicus rejected this server's credentials. An operator needs to check them.") from None
    if isinstance(exc, copernicus.CopernicusProductNotFoundError):
        raise HTTPException(status_code=404, detail="That Sentinel-2 product is no longer available.") from None
    if isinstance(exc, copernicus.CopernicusUnavailableError):
        raise HTTPException(status_code=503, detail="Copernicus is busy or unavailable right now. Try again shortly.") from None
    logger.warning("Copernicus request failed", exc_info=True)
    raise HTTPException(status_code=502, detail="Copernicus returned an unexpected response.") from None


async def _schema_ready() -> bool:
    try:
        response = await run_in_threadpool(lambda: get_supabase().rpc("m9_optical_schema_ready").execute())
    except Exception:
        logger.info("Optical schema probe failed", exc_info=True)
        return False
    return getattr(response, "data", None) is True


def _model() -> OpticalModelRead | None:
    try:
        from app.services.optical import lulc_model

        s = lulc_model.card_summary()
    except Exception:
        logger.info("Optical model card is not readable", exc_info=True)
        return None
    return OpticalModelRead(
        name=s.get("name"),
        version=s.get("version"),
        classes=s.get("classes") or [],
        single_date=s.get("single_date"),
        seasonal_stack=s.get("seasonal_stack"),
        reference=s.get("reference"),
        temperature=s.get("temperature"),
    )


async def _budget(user_id: str) -> OpticalBudgetRead | None:
    try:
        response = await run_in_threadpool(
            lambda: get_supabase().rpc("optical_processing_units_this_month", {"p_owner_id": user_id}).execute()
        )
    except Exception:
        logger.warning("Could not read the processing-unit budget", exc_info=True)
        return None
    data = getattr(response, "data", None)
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return None
    return OpticalBudgetRead(
        user_month_cap=settings.OPTICAL_PU_BUDGET_PER_USER_MONTH,
        user_used=round(float(data.get("user_optical") or 0.0), 2),
        account_month_cap=settings.OPTICAL_PU_GLOBAL_MONTH_CAP,
        account_used=round(float(data.get("all") or 0.0), 2),
    )


def _grid_read(grid: OpticalGrid) -> OpticalGridRead:
    return OpticalGridRead(
        epsg=grid.epsg,
        width=grid.width,
        height=grid.height,
        width_km=round(grid.width_km, 2),
        height_km=round(grid.height_km, 2),
        corners_lonlat=grid.corners_lonlat(),
        processing_units_per_date=estimate_pu(grid.width, grid.height),
    )


def _season_for(day: date) -> str:
    md = (day.month, day.day)
    for name, start, end in SEASONS:
        m0, d0 = (int(x) for x in start.split("-"))
        m1, d1 = (int(x) for x in end.split("-"))
        if (m0, d0) <= md <= (m1, d1) or (name == "rabi" and md == (2, 29)):
            return name
    return day.strftime("%b").lower()


def _side_for(mode: OpticalRunMode) -> float:
    return settings.OPTICAL_MAX_SIDE_KM_STACK if mode is OpticalRunMode.SEASONAL_STACK else settings.OPTICAL_MAX_SIDE_KM_SINGLE


# --------------------------------------------------------------------------- providers


@router.get("/providers", response_model=OpticalProvidersRead)
async def providers(current_user: CurrentUser = Depends(get_current_user)) -> OpticalProvidersRead:
    """Availability, limits, the user's processing-unit budget and the model's held-out numbers."""
    reason = _reason()
    if reason is None and not await _schema_ready():
        reason = "schema_not_applied"
    model = _model()
    if reason is None and model is None:
        reason = "model_unavailable"
    budget = await _budget(current_user.id) if reason is None else None
    return OpticalProvidersRead(
        enabled=reason is None,
        reason=reason,
        max_side_km_single=settings.OPTICAL_MAX_SIDE_KM_SINGLE,
        max_side_km_stack=settings.OPTICAL_MAX_SIDE_KM_STACK,
        min_side_px=settings.OPTICAL_MIN_SIDE_PX,
        default_max_cloud=settings.OPTICAL_DEFAULT_MAX_CLOUD,
        max_search_days=settings.OPTICAL_MAX_SEARCH_DAYS,
        max_results=settings.OPTICAL_MAX_SEARCH_RESULTS,
        processing_units_per_10km_date=estimate_pu(1000, 1000),
        seasons=[OpticalSeasonRead(name=n, start_month_day=s, end_month_day=e) for n, s, e in SEASONS],
        budget=budget,
        model=model,
    )


# --------------------------------------------------------------------------- search


def _box(west: float, south: float, east: float, north: float) -> copernicus.BoundingBox:
    if east <= west:
        raise HTTPException(status_code=422, detail="The area must have its east edge past its west edge (antimeridian boxes are not supported).")
    if north <= south:
        raise HTTPException(status_code=422, detail="The area must have its north edge above its south edge.")
    return copernicus.BoundingBox(west=west, south=south, east=east, north=north)


@router.post("/search", response_model=OpticalSearchResponse)
async def search(payload: OpticalSearchRequest, current_user: CurrentUser = Depends(get_current_user)) -> OpticalSearchResponse:
    """Sentinel-2 L2A dates over the box, plus the grid and cost each mode would use."""
    del current_user
    reason = _reason()
    if reason is not None:
        raise _unavailable(reason)
    bbox = _box(payload.west, payload.south, payload.east, payload.north)
    if payload.end_date < payload.start_date:
        raise HTTPException(status_code=422, detail="The end date must not precede the start date.")
    if (payload.end_date - payload.start_date).days > settings.OPTICAL_MAX_SEARCH_DAYS:
        raise HTTPException(status_code=422, detail=f"Search a window of at most {settings.OPTICAL_MAX_SEARCH_DAYS} days.")
    start = datetime.combine(payload.start_date, time.min, tzinfo=timezone.utc)
    end = datetime.combine(payload.end_date, time.max, tzinfo=timezone.utc)
    max_cloud = settings.OPTICAL_DEFAULT_MAX_CLOUD if payload.max_cloud is None else payload.max_cloud
    limit = min(payload.limit or settings.OPTICAL_MAX_SEARCH_RESULTS, settings.OPTICAL_MAX_SEARCH_RESULTS)
    try:
        products = await run_in_threadpool(
            lambda: sentinel2.search(bbox=bbox, start=start, end=end, max_cloud=max_cloud, limit=limit)
        )
    except copernicus.CopernicusError as exc:
        _provider_error(exc)

    notes: list[str] = []
    grids: dict[str, OpticalGridRead] = {}
    for mode, label in ((OpticalRunMode.SINGLE_DATE, "Single date"), (OpticalRunMode.SEASONAL_STACK, "Seasonal stack")):
        try:
            grid = await run_in_threadpool(
                lambda m=mode: grid_for_box(payload.west, payload.south, payload.east, payload.north, max_side_km=_side_for(m))
            )
            grids[mode.value] = _grid_read(grid)
        except GridError as exc:
            notes.append(f"{label}: {exc}")
    return OpticalSearchResponse(
        items=[
            OpticalProductRead(
                product_id=p.product_id,
                name=p.name,
                sensing_start=p.sensing_start,
                cloud_cover=p.cloud_cover,
                tile_id=p.tile_id,
                platform=p.platform,
                relative_orbit=p.relative_orbit,
                processing_baseline=p.processing_baseline,
                online=p.online,
                covers_aoi=p.covers(bbox),
                footprint=p.footprint,
            )
            for p in products
        ],
        grid_single_date=grids.get(OpticalRunMode.SINGLE_DATE.value),
        grid_seasonal_stack=grids.get(OpticalRunMode.SEASONAL_STACK.value),
        notes=notes,
    )


# --------------------------------------------------------------------------- runs


def _compose(scene: dict[str, Any], acquisition: dict[str, Any] | None, job: dict[str, Any] | None) -> dict[str, Any]:
    meta = scene.get("metadata") if isinstance(scene.get("metadata"), dict) else {}
    optical = meta.get("optical") if isinstance(meta.get("optical"), dict) else {}
    stack = (acquisition or {}).get("stack") or []
    dates = [str(s.get("sensing_start", ""))[:10] for s in stack if isinstance(s, dict)] or list(optical.get("dates") or [])
    mode = optical.get("mode")
    return {
        "scene_id": scene["id"],
        "project_id": scene["project_id"],
        "name": scene.get("name") or "",
        "status": str(scene.get("status") or ""),
        "mode": mode if mode in {m.value for m in OpticalRunMode} else None,
        "created_at": scene.get("created_at"),
        "dates": dates,
        "grid": (acquisition or {}).get("grid") or optical.get("grid"),
        "corners_lonlat": optical.get("corners_lonlat"),
        "processing_units": (acquisition or {}).get("processing_units"),
        "estimated_processing_units": (acquisition or {}).get("estimated_processing_units"),
        "acquisition_status": (acquisition or {}).get("status"),
        "job_id": (job or {}).get("id"),
        "job_status": (job or {}).get("status"),
        "job_stage": (job or {}).get("stage"),
        "job_progress": (job or {}).get("progress"),
        "failure_code": scene.get("failure_code") or (job or {}).get("error_code"),
        "failure_detail": scene.get("failure_detail") or (job or {}).get("error_detail"),
    }


async def _latest(table: str, scene_ids: list[str], owner_id: str, extra: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    if not scene_ids:
        return {}

    def op():
        q = get_supabase().table(table).select("*").in_("scene_id", scene_ids).eq("owner_id", owner_id)
        for k, v in (extra or {}).items():
            q = q.eq(k, v)
        return q.order("created_at", desc=True).limit(500).execute()

    out: dict[str, dict[str, Any]] = {}
    for row in _rows(await _execute(op, "Optical run data is temporarily unavailable")):
        out.setdefault(str(row["scene_id"]), row)
    return out


async def _run_read(scene: dict[str, Any], owner_id: str) -> OpticalRunRead:
    sid = str(scene["id"])
    acq = (await _latest("scene_acquisitions", [sid], owner_id, {"collection": "SENTINEL-2"})).get(sid)
    job = (await _latest("processing_jobs", [sid], owner_id, {"kind": "process_scene"})).get(sid)
    return OpticalRunRead(**_compose(scene, acq, job))


async def _optical_project(project_id: UUID | None, current_user: CurrentUser) -> dict[str, Any]:
    if project_id is not None:
        return await resolve_owned_project(project_id, current_user)
    found = _first(
        await _execute(
            lambda: get_supabase()
            .table("projects")
            .select("*")
            .eq("owner_id", current_user.id)
            .like("description", f"%{PROJECT_MARKER}%")
            .order("created_at")
            .limit(1)
            .execute(),
            "Project data is temporarily unavailable",
        )
    )
    if found is not None:
        return found
    created = _first(
        await _execute(
            lambda: get_supabase()
            .table("projects")
            .insert({"owner_id": current_user.id, "name": PROJECT_NAME, "description": PROJECT_DESCRIPTION})
            .execute(),
            "Project data is temporarily unavailable",
        )
    )
    if created is None:
        raise HTTPException(status_code=503, detail="The optical project could not be created")
    return created


@router.post("/runs", response_model=OpticalRunCreateResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_run(payload: OpticalRunCreate, current_user: CurrentUser = Depends(get_current_user)) -> OpticalRunCreateResponse:
    """Verify the dates with Copernicus, check the budget, then durably queue the run."""
    reason = _reason()
    if reason is None and not await _schema_ready():
        reason = "schema_not_applied"
    if reason is None and _model() is None:
        reason = "model_unavailable"
    if reason is not None:
        raise _unavailable(reason)

    # A retried POST returns the original run and creates nothing.
    if payload.client_request_id:
        prior = _first(
            await _execute(
                lambda: get_supabase()
                .table("scene_acquisitions")
                .select("scene_id")
                .eq("owner_id", current_user.id)
                .eq("client_request_id", payload.client_request_id)
                .limit(1)
                .execute(),
                "Optical run data is temporarily unavailable",
            )
        )
        if prior is not None:
            scene = await resolve_owned_scene(prior["scene_id"], current_user)
            run = await _run_read(scene, current_user.id)
            return OpticalRunCreateResponse(run=run, processing_job_id=run.job_id, replayed=True)

    aoi = payload.aoi
    bbox = _box(aoi.west, aoi.south, aoi.east, aoi.north)
    try:
        grid = await run_in_threadpool(
            lambda: grid_for_box(aoi.west, aoi.south, aoi.east, aoi.north, max_side_km=_side_for(payload.mode))
        )
    except GridError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    products: list[sentinel2.S2Product] = []
    for product_id in payload.product_ids:
        try:
            product = await run_in_threadpool(lambda pid=product_id: sentinel2.get(pid))
        except copernicus.CopernicusError as exc:
            _provider_error(exc)
        if not product.online:
            raise HTTPException(status_code=409, detail=f"{product.name} is in the long-term archive and must be ordered first.")
        if product.sensing_start is None:
            raise HTTPException(status_code=409, detail=f"{product.name} has no acquisition time.")
        products.append(product)
    products.sort(key=lambda p: p.sensing_start)
    days = [p.sensing_start.date() for p in products]
    if len(set(days)) != len(days):
        raise HTTPException(status_code=422, detail="Two of the chosen products are from the same day; pick one per date.")
    if (days[-1] - days[0]).days > settings.OPTICAL_MAX_SEARCH_DAYS:
        raise HTTPException(status_code=422, detail=f"The dates must fall within {settings.OPTICAL_MAX_SEARCH_DAYS} days of each other.")

    estimate = estimate_pu(grid.width, grid.height, dates=len(products))
    budget = await _budget(current_user.id)
    if budget is None:
        raise HTTPException(status_code=503, detail="The processing-unit budget could not be checked; try again shortly.")
    if budget.user_used + estimate > budget.user_month_cap:
        left = max(0.0, budget.user_month_cap - budget.user_used)
        raise HTTPException(
            status_code=429,
            detail=f"This run needs about {estimate:.0f} processing units and your optical allowance has {left:.0f} left this month. Draw a smaller area or use fewer dates.",
        )
    if budget.account_used + estimate > budget.account_month_cap:
        raise HTTPException(status_code=429, detail="The shared Copernicus allowance for optical runs is used up for this month.")

    project = await _optical_project(payload.project_id, current_user)
    corners = grid.corners_lonlat()
    lon = sum(c[0] for c in corners) / 4.0
    lat = sum(c[1] for c in corners) / 4.0
    name = payload.name or f"Land cover {days[0].isoformat()} ({lat:.3f}, {lon:.3f})"
    scene = _first(
        await _execute(
            lambda: get_supabase()
            .table("scenes")
            .insert(
                {
                    "owner_id": current_user.id,
                    "project_id": str(project["id"]),
                    "name": name[:160],
                    "status": "draft",
                    "metadata": {
                        "pipeline": PIPELINE,
                        "optical": {
                            "mode": payload.mode.value,
                            "aoi": aoi.model_dump(),
                            "grid": grid.as_dict(),
                            "corners_lonlat": corners,
                            "dates": [d.isoformat() for d in days],
                            "estimated_processing_units": estimate,
                        },
                    },
                }
            )
            .execute(),
            "Scene data is temporarily unavailable",
        )
    )
    if scene is None:
        raise HTTPException(status_code=503, detail="The optical run could not be created")

    stack = [
        {
            "product_id": p.product_id,
            "product_name": p.name,
            "sensing_start": p.sensing_start.isoformat(),
            "cloud_cover": p.cloud_cover,
            "tile_id": p.tile_id,
            "processing_baseline": p.processing_baseline,
            "season": _season_for(p.sensing_start.date()),
        }
        for p in products
    ]
    request = {
        "stack": stack,
        "grid": grid.as_dict(),
        "west": aoi.west,
        "south": aoi.south,
        "east": aoi.east,
        "north": aoi.north,
        "expected_size_bytes": max(1, grid.width * grid.height * 13 * 2 * len(products)),
        "footprint": products[0].footprint,
        "cloud_cover": max((p.cloud_cover or 0.0) for p in products),
        "estimated_processing_units": estimate,
    }
    try:
        result = _first(
            await _execute(
                lambda: get_supabase()
                .rpc(
                    "start_optical_acquisition",
                    {
                        "p_owner_id": current_user.id,
                        "p_scene_id": str(scene["id"]),
                        "p_client_request_id": payload.client_request_id,
                        "p_request": request,
                    },
                )
                .execute(),
                "Optical runs are temporarily unavailable",
            )
        )
        if result is None or not result.get("accepted"):
            detail = {
                "not_optical_scene": "That scene is not an optical run.",
                "scene_not_acceptable": "This run already has imagery.",
                "active_job": "This run is already processing.",
                "acquisition_in_progress": "A fetch is already running for this run.",
                "source_already_present": "This run already has imagery.",
            }.get(str((result or {}).get("reason")), "The optical run could not be started.")
            raise HTTPException(status_code=409, detail=detail)
    except HTTPException:
        # Nothing durable references the fresh scene yet; do not leave an empty run behind.
        await _execute(
            lambda: get_supabase().table("scenes").delete().eq("id", str(scene["id"])).eq("owner_id", current_user.id).execute(),
            "Scene data is temporarily unavailable",
        )
        raise

    logger.info(
        "Queued an optical run scene_id=%s dates=%s grid=%sx%s estimate_pu=%s",
        scene["id"], len(products), grid.width, grid.height, estimate,
    )
    run = await _run_read(await resolve_owned_scene(scene["id"], current_user), current_user.id)
    job_id = result.get("job_id")
    return OpticalRunCreateResponse(
        run=run, processing_job_id=UUID(str(job_id)) if job_id else None, replayed=bool(result.get("replayed"))
    )


@router.get("/runs", response_model=list[OpticalRunRead])
async def list_runs(current_user: CurrentUser = Depends(get_current_user)) -> list[OpticalRunRead]:
    """The caller's optical runs, newest first."""
    scenes = _rows(
        await _execute(
            lambda: get_supabase()
            .table("scenes")
            .select("*")
            .eq("owner_id", current_user.id)
            .eq("metadata->>pipeline", PIPELINE)
            .order("created_at", desc=True)
            .limit(100)
            .execute(),
            "Optical run data is temporarily unavailable",
        )
    )
    ids = [str(s["id"]) for s in scenes]
    acquisitions = await _latest("scene_acquisitions", ids, current_user.id, {"collection": "SENTINEL-2"})
    jobs = await _latest("processing_jobs", ids, current_user.id, {"kind": "process_scene"})
    return [OpticalRunRead(**_compose(s, acquisitions.get(str(s["id"])), jobs.get(str(s["id"])))) for s in scenes]


@router.get("/runs/{scene_id}", response_model=OpticalRunDetail)
async def read_run(scene_id: UUID, current_user: CurrentUser = Depends(get_current_user)) -> OpticalRunDetail:
    """One run, its preview/download artifact ids, and its scene record once ready."""
    scene = await resolve_owned_scene(scene_id, current_user)
    meta = scene.get("metadata") if isinstance(scene.get("metadata"), dict) else {}
    if meta.get("pipeline") != PIPELINE:
        raise HTTPException(status_code=404, detail="Optical run not found")
    base = (await _run_read(scene, current_user.id)).model_dump()

    rows = _rows(
        await _execute(
            lambda: get_supabase()
            .table("scene_artifacts")
            .select("id,logical_key,kind,storage_key,size_bytes,content_type")
            .eq("scene_id", str(scene_id))
            .eq("owner_id", current_user.id)
            .eq("status", "available")
            .execute(),
            "Artifact data is temporarily unavailable",
        )
    )
    artifacts: dict[str, str] = {}
    record_row = None
    for row in rows:
        key = str(row.get("logical_key") or "")
        name = public_name(key)
        if name is not None:
            artifacts[name] = str(row["id"])
        if key == RECORD_KEY:
            record_row = row

    record = None
    if record_row is not None and int(record_row.get("size_bytes") or 0) <= MAX_RECORD_BYTES:
        try:
            raw = await run_in_threadpool(lambda: get_object_storage().read_range(str(record_row["storage_key"]), 0, None))
            record = json.loads(raw.decode("utf-8"))
        except Exception:
            logger.info("Could not read an optical scene record", exc_info=True)
            record = None
    return OpticalRunDetail(**base, artifacts=artifacts, record=record)


# --------------------------------------------------------------------------- similarity


@router.post("/runs/{scene_id}/similar", response_model=OpticalSimilarResponse)
async def similar(
    scene_id: UUID, payload: OpticalSimilarRequest, current_user: CurrentUser = Depends(get_current_user)
) -> OpticalSimilarResponse:
    """Where else this spot looks the same to the model: a heatmap over the run, or windows in your other runs."""
    scene = await resolve_owned_scene(scene_id, current_user)
    meta = scene.get("metadata") if isinstance(scene.get("metadata"), dict) else {}
    if meta.get("pipeline") != PIPELINE:
        raise HTTPException(status_code=404, detail="Optical run not found")
    if str(scene.get("status") or "") != "ready":
        raise HTTPException(status_code=409, detail="This run is still processing; similarity is available once it is ready.")
    rows = _rows(
        await _execute(
            lambda: get_supabase()
            .table("scene_artifacts")
            .select("id,logical_key,storage_key,size_bytes,checksum_sha256,metadata")
            .eq("scene_id", str(scene_id))
            .eq("owner_id", current_user.id)
            .eq("status", "available")
            .in_("logical_key", [EMBEDDINGS_KEY, WINDOWS_KEY])
            .execute(),
            "Artifact data is temporarily unavailable",
        )
    )
    found = {str(row.get("logical_key")): row for row in rows}
    embedding = found.get(EMBEDDINGS_KEY)
    if embedding is None:
        raise HTTPException(status_code=409, detail=REPROCESS_DETAIL)
    emb_meta = embedding.get("metadata") or {}
    model_label = " ".join(str(v) for v in (emb_meta.get("model_name"), emb_meta.get("model_version")) if v)
    try:
        grid = OpticalGrid.from_dict(emb_meta.get("grid") or {})
        token_px = int(emb_meta.get("token_px") or 4)
        row, col = await run_in_threadpool(similar_service.seed_token, grid, token_px, payload.lon, payload.lat)
        seed = {"lon": payload.lon, "lat": payload.lat, "token_row": row, "token_col": col, "token_m": token_px * grid.res}

        if payload.scope is OpticalSimilarScope.RUN:
            reduced, valid = await run_in_threadpool(similar_service.load_embeddings, get_object_storage(), embedding)
            heat = await run_in_threadpool(
                lambda: similar_service.run_heatmap(reduced, valid, row, col, grid=grid, token_px=token_px)
            )
            return OpticalSimilarResponse(
                scope=OpticalSimilarScope.RUN,
                label=SIMILAR_LABEL,
                meaning=SIMILAR_MEANING,
                seed=seed,
                image=heat["image"],
                areas=heat["areas"],
                basis={
                    "model": model_label,
                    "embedding": emb_meta.get("basis"),
                    "dims": emb_meta.get("dims"),
                    "energy_captured": emb_meta.get("energy_captured"),
                    "token_m": token_px * grid.res,
                },
            )

        windows = found.get(WINDOWS_KEY)
        if windows is None:
            raise HTTPException(status_code=409, detail=REPROCESS_DETAIL)
        win_meta = windows.get("metadata") or {}
        limit = min(payload.limit or 10, settings.OPTICAL_SIMILAR_MAX_MATCHES)
        seed_point, hits, window = await run_in_threadpool(
            lambda: similar_service.library_matches(
                QdrantStore.get_instance(),
                scene_id=str(scene_id),
                owner_id=current_user.id,
                model_version=str(win_meta.get("model_version") or "unknown"),
                window_tokens=int(win_meta.get("window_tokens") or settings.OPTICAL_WINDOW_TOKENS),
                row=row,
                col=col,
                limit=limit,
            )
        )
    except similar_service.SimilarError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None
    except GridError:
        raise HTTPException(status_code=409, detail="This run's stored grid is unreadable; reprocess it.") from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Optical similarity failed scene_id=%s", scene_id)
        raise HTTPException(status_code=503, detail="Similarity search is temporarily unavailable.") from None

    # PostgreSQL stays the authority: a window only counts if its run still exists and is the caller's.
    ids = sorted({str((hit.get("payload") or {}).get("scene_id")) for hit in hits if (hit.get("payload") or {}).get("scene_id")})
    names: dict[str, Any] = {}
    if ids:
        scene_rows = _rows(
            await _execute(
                lambda: get_supabase().table("scenes").select("id,name").eq("owner_id", current_user.id).in_("id", ids).execute(),
                "Scene data is temporarily unavailable",
            )
        )
        names = {str(r["id"]): r.get("name") for r in scene_rows}
    matches = [
        similar_service.window_match(hit.get("payload") or {}, hit.get("score"), names[str((hit.get("payload") or {}).get("scene_id"))])
        for hit in hits
        if str((hit.get("payload") or {}).get("scene_id")) in names
    ]
    notes = [] if matches else ["No windows from your other optical runs yet; run more areas to compare across them."]
    return OpticalSimilarResponse(
        scope=OpticalSimilarScope.LIBRARY,
        label=SIMILAR_LABEL,
        meaning=similar_service.LIBRARY_MEANING,
        seed={**seed, "window_row": window[0], "window_col": window[1]},
        seed_window=similar_service.window_match(seed_point.get("payload") or {}, None, scene.get("name")),
        matches=matches,
        basis={
            "model": model_label,
            "embedding": win_meta.get("embedding"),
            "window_m": win_meta.get("window_m"),
            "dim": win_meta.get("dim"),
        },
        notes=notes,
    )
