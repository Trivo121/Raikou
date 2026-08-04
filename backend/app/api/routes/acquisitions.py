"""Provider-fetched scene sources: catalogue search proxy and acquisition start.

The browser never receives provider credentials or upstream URLs. Search is
proxied here, and the actual product download happens in an M3 worker so a
FastAPI restart cannot lose a gigabyte of transfer.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
import logging
from typing import Any, Callable, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from starlette.concurrency import run_in_threadpool

from app.api.deps import (
    CurrentUser,
    get_current_user,
    resolve_owned_scene,
    resolve_owned_scene_acquisition,
)
from app.core.config import settings
from app.schemas.acquisitions import (
    AcquisitionCreateRequest,
    AcquisitionProductRead,
    AcquisitionProviderRead,
    AcquisitionProvidersRead,
    AcquisitionSearchRequest,
    AcquisitionSearchResponse,
    SceneAcquisitionCreateResponse,
    SceneAcquisitionRead,
)
from app.services.acquisitions import copernicus
from app.services.database import get_supabase
from app.services.processing.scene_geography import bbox_extent_km

router = APIRouter()
logger = logging.getLogger(__name__)


async def _execute(operation: Callable[[], Any], unavailable_detail: str) -> Any:
    try:
        return await run_in_threadpool(operation)
    except HTTPException:
        raise
    except Exception:
        logger.exception("M7 acquisition database operation failed")
        raise HTTPException(status_code=503, detail=unavailable_detail) from None


def _first_row(response: Any) -> dict[str, Any] | None:
    data = getattr(response, "data", None)
    if isinstance(data, list):
        return data[0] if data else None
    if isinstance(data, dict):
        return data
    return None


def _provider_unavailable(reason: str) -> HTTPException:
    details = {
        "not_configured": (
            "Copernicus fetch is not configured on this server. "
            "An operator must set COPERNICUS_CLIENT_ID and COPERNICUS_CLIENT_SECRET."
        ),
        "disabled": "Copernicus fetch is switched off on this server.",
        "schema_not_applied": (
            "Copernicus fetch is unavailable because its database migration has not been applied."
        ),
    }
    return HTTPException(status_code=503, detail=details.get(reason, "Copernicus fetch is unavailable."))


def _raise_for_provider_error(exc: Exception) -> NoReturn:
    """Translate a provider failure into an honest HTTP status.

    A credential problem is the operator's, not the caller's, so it is a 503
    rather than a 401 the user could do nothing about.
    """
    if isinstance(exc, copernicus.CopernicusAuthError):
        logger.error("Copernicus rejected the configured OAuth client", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Copernicus rejected this server's credentials. An operator needs to check them.",
        ) from None
    if isinstance(exc, copernicus.CopernicusProductNotFoundError):
        raise HTTPException(status_code=404, detail="That Copernicus product is no longer available.") from None
    if isinstance(exc, copernicus.CopernicusUnavailableError):
        raise HTTPException(
            status_code=503,
            detail="Copernicus is busy or unavailable right now. Try again shortly.",
        ) from None
    logger.warning("Copernicus request failed", exc_info=True)
    raise HTTPException(status_code=502, detail="Copernicus returned an unexpected response.") from None


async def _acquisition_schema_ready() -> bool:
    """Probe the M7 schema without making it a readiness gate."""
    try:
        response = await run_in_threadpool(
            lambda: get_supabase().rpc("m7_acquisition_schema_ready").execute()
        )
    except Exception:
        logger.info("M7 acquisition schema probe failed", exc_info=True)
        return False
    return getattr(response, "data", None) is True


def _copernicus_reason() -> str | None:
    if not settings.COPERNICUS_ENABLED:
        return "disabled"
    if not settings.copernicus_configured:
        return "not_configured"
    return None


@router.get("/providers", response_model=AcquisitionProvidersRead)
async def list_providers(
    current_user: CurrentUser = Depends(get_current_user),
) -> AcquisitionProvidersRead:
    """Report availability and the limits the UI must mirror. No upstream call."""
    del current_user
    reason = _copernicus_reason()
    if reason is None and not await _acquisition_schema_ready():
        reason = "schema_not_applied"
    return AcquisitionProvidersRead(
        copernicus=AcquisitionProviderRead(
            enabled=reason is None,
            reason=reason,
            max_results=settings.COPERNICUS_MAX_SEARCH_RESULTS,
            max_search_days=settings.COPERNICUS_MAX_SEARCH_DAYS,
            max_aoi_sq_km=settings.COPERNICUS_MAX_AOI_SQ_KM,
            product_type=copernicus.REQUIRED_PRODUCT_TYPE,
            polarisation_channels=copernicus.REQUIRED_POLARISATION_CHANNELS,
            # Sentinel-1 GRD ships as whole ~250x170 km frames and there is no
            # provider API returning "just my AOI" as a SAFE product. Cropping
            # would destroy the SAFE layout the pipeline reads.
            aoi_is_crop=False,
        )
    )


def _validated_bbox(payload: AcquisitionSearchRequest) -> copernicus.BoundingBox:
    if payload.east <= payload.west:
        raise HTTPException(
            status_code=422,
            detail=(
                "The search area must have its east edge past its west edge. "
                "Areas crossing the antimeridian are not supported yet; "
                "search each side separately."
            ),
        )
    if payload.north <= payload.south:
        raise HTTPException(
            status_code=422, detail="The search area must have its north edge above its south edge."
        )
    width_km, height_km = bbox_extent_km(payload.west, payload.south, payload.east, payload.north)
    area = width_km * height_km
    if area > settings.COPERNICUS_MAX_AOI_SQ_KM:
        raise HTTPException(
            status_code=422,
            detail=(
                f"That area is about {round(area):,} km². "
                f"Draw a box under {round(settings.COPERNICUS_MAX_AOI_SQ_KM):,} km²."
            ),
        )
    return copernicus.BoundingBox(
        west=payload.west, south=payload.south, east=payload.east, north=payload.north
    )


def _validated_window(payload: AcquisitionSearchRequest) -> tuple[datetime, datetime]:
    if payload.end_date < payload.start_date:
        raise HTTPException(status_code=422, detail="The end date must not precede the start date.")
    span_days = (payload.end_date - payload.start_date).days
    if span_days > settings.COPERNICUS_MAX_SEARCH_DAYS:
        raise HTTPException(
            status_code=422,
            detail=f"Search a window of at most {settings.COPERNICUS_MAX_SEARCH_DAYS} days.",
        )
    start = datetime.combine(payload.start_date, time.min, tzinfo=timezone.utc)
    end = datetime.combine(payload.end_date, time.max, tzinfo=timezone.utc)
    return start, end


@router.post("/search", response_model=AcquisitionSearchResponse)
async def search_products(
    payload: AcquisitionSearchRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> AcquisitionSearchResponse:
    """Proxy one catalogue search so provider credentials stay server-side."""
    del current_user
    reason = _copernicus_reason()
    if reason is not None:
        raise _provider_unavailable(reason)

    bbox = _validated_bbox(payload)
    start, end = _validated_window(payload)
    limit = min(
        payload.limit or settings.COPERNICUS_MAX_SEARCH_RESULTS,
        settings.COPERNICUS_MAX_SEARCH_RESULTS,
    )

    try:
        products = await run_in_threadpool(
            lambda: copernicus.search_products(bbox=bbox, start=start, end=end, limit=limit)
        )
    except copernicus.CopernicusError as exc:
        _raise_for_provider_error(exc)

    return AcquisitionSearchResponse(
        items=[
            AcquisitionProductRead(
                product_id=product.product_id,
                name=product.name,
                # search_products already dropped anything that did not match
                # the locked contract, so these are never None here.
                product_type=product.product_type or copernicus.REQUIRED_PRODUCT_TYPE,
                polarisation_channels=(
                    product.polarisation_channels or copernicus.REQUIRED_POLARISATION_CHANNELS
                ),
                sensing_start=product.sensing_start,
                online=product.online,
                size_bytes=product.size_bytes,
                footprint=product.footprint,
            )
            for product in products
        ],
        aoi_is_crop=False,
    )


def _assert_product_is_usable(
    product: copernicus.CopernicusProduct, requested_name: str
) -> None:
    """Re-verify at accept time. The browser is not trusted for any of this."""
    if not copernicus.PRODUCT_NAME_PATTERN.match(product.name):
        raise HTTPException(
            status_code=409,
            detail=(
                "That product is not a dual-polarisation Sentinel-1 IW GRDH scene, "
                "which is what this pipeline can process."
            ),
        )
    if product.product_type != copernicus.REQUIRED_PRODUCT_TYPE:
        raise HTTPException(
            status_code=409,
            detail=f"This pipeline needs a {copernicus.REQUIRED_PRODUCT_TYPE} product.",
        )
    if product.polarisation_channels != copernicus.REQUIRED_POLARISATION_CHANNELS:
        raise HTTPException(
            status_code=409,
            detail=(
                "This pipeline needs both VV and VH polarisations; the scattering, "
                "mechanism map, and land-cover blocks cannot be built without them."
            ),
        )
    if not product.online:
        raise HTTPException(
            status_code=409,
            detail=(
                "This product is in the long-term archive and must be ordered "
                "before it can be downloaded."
            ),
        )
    if not product.size_bytes or product.size_bytes <= 0:
        raise HTTPException(
            status_code=409, detail="Copernicus reported no download size for that product."
        )
    if product.size_bytes > settings.UPLOAD_MAX_ARCHIVE_BYTES:
        raise HTTPException(
            status_code=409,
            detail="That product is larger than this workspace's per-scene limit.",
        )
    if product.name != requested_name:
        # The catalogue moved under an open browser tab.
        raise HTTPException(
            status_code=409,
            detail="That search result is out of date. Search again and reselect the scene.",
        )


@router.post("", response_model=SceneAcquisitionCreateResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_acquisition(
    payload: AcquisitionCreateRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> SceneAcquisitionCreateResponse:
    """Verify a product against the provider, then durably queue its download."""
    reason = _copernicus_reason()
    if reason is not None:
        raise _provider_unavailable(reason)

    scene = await resolve_owned_scene(payload.scene_id, current_user)

    try:
        product = await run_in_threadpool(lambda: copernicus.get_product(payload.product_id))
    except copernicus.CopernicusError as exc:
        _raise_for_provider_error(exc)
    _assert_product_is_usable(product, payload.product_name)

    response = await _execute(
        lambda: get_supabase()
        .rpc(
            "start_scene_acquisition",
            {
                "p_owner_id": current_user.id,
                "p_scene_id": str(payload.scene_id),
                "p_client_request_id": payload.client_request_id,
                # Only server-fetched values are persisted.
                "p_product": {
                    "product_id": product.product_id,
                    "product_name": product.name,
                    "product_type": product.product_type,
                    "polarisation_channels": product.polarisation_channels,
                    "sensing_start": (
                        product.sensing_start.isoformat() if product.sensing_start else None
                    ),
                    "online": product.online,
                    "expected_size_bytes": product.size_bytes,
                    "footprint": product.footprint,
                },
            },
        )
        .execute(),
        "Scene acquisition is temporarily unavailable",
    )

    result = _first_row(response)
    if result is None:
        raise HTTPException(status_code=404, detail="Scene not found")
    if not result.get("accepted"):
        rejection = str(result.get("reason") or "not_acceptable")
        details = {
            "scene_not_acceptable": "This scene already has a source and cannot take another.",
            "upload_in_progress": "An upload is already in progress for this scene.",
            "active_job": "This scene already has active processing.",
            "acquisition_in_progress": "A Copernicus download is already running for this scene.",
            "source_already_present": "This scene already has a source file.",
            "product_offline": (
                "This product is in the long-term archive and must be ordered "
                "before it can be downloaded."
            ),
        }
        raise HTTPException(
            status_code=409, detail=details.get(rejection, "This scene cannot fetch a source now.")
        )

    acquisition_id = result.get("acquisition_id")
    if not acquisition_id:
        raise HTTPException(status_code=503, detail="The acquisition was not created durably")

    row = await _execute(
        lambda: get_supabase()
        .table("scene_acquisitions")
        .select("*")
        .eq("id", str(acquisition_id))
        .eq("owner_id", current_user.id)
        .limit(1)
        .execute(),
        "Scene acquisition is temporarily unavailable",
    )
    created = _first_row(row)
    if created is None:
        raise HTTPException(status_code=503, detail="The acquisition was not created durably")

    logger.info(
        "Queued a Copernicus acquisition scene_id=%s project_id=%s bytes=%s replayed=%s",
        payload.scene_id,
        scene.get("project_id"),
        product.size_bytes,
        bool(result.get("replayed")),
    )
    job_id = result.get("job_id")
    return SceneAcquisitionCreateResponse(
        acquisition=SceneAcquisitionRead.model_validate(created),
        processing_job_id=UUID(str(job_id)) if job_id else None,
        replayed=bool(result.get("replayed")),
    )


@router.get("/{acquisition_id}", response_model=SceneAcquisitionRead)
async def read_acquisition(
    acquisition_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> SceneAcquisitionRead:
    """Read one owned acquisition."""
    acquisition = await resolve_owned_scene_acquisition(acquisition_id, current_user)
    return SceneAcquisitionRead.model_validate(acquisition)
