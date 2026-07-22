"""M5 tenant-scoped evidence retrieval and grounded NDJSON chat."""

from __future__ import annotations

import asyncio
import base64
from io import BytesIO
import json
import logging
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from openai import AsyncOpenAI
from PIL import Image
from starlette.concurrency import run_in_threadpool

from app.api.deps import (
    CurrentUser,
    get_current_user,
    resolve_owned_conversation,
    resolve_owned_project,
    resolve_owned_scene_in_project,
)
from app.api.routes.projects import _execute, _first_row
from app.core.config import settings
from app.schemas.artifacts import SceneArtifactRead
from app.schemas.evidence import (
    ChatStreamRequest,
    ConversationCreateRequest,
    ConversationMessagePage,
    ConversationMessageRead,
    ConversationRead,
    EvidenceCitation,
    EvidenceMetadataFilters,
    EvidenceSearchCard,
    EvidenceSearchRequest,
    EvidenceSearchResponse,
)
from app.schemas.workspace import PatchBoundsRead
from app.services.cache.evidence import (
    get_project_evidence_cache_generation,
    get_json_cache,
    m5_cache_key,
    normalize_query,
    set_json_cache,
)
from app.services.database import get_supabase
from app.services.models.sarclip_encoder import SARCLIPEncoder
from app.services.processing.chat_policy import (
    ChatIntent,
    classify_scene_query,
    detector_answer,
    environment_answer,
)
from app.services.storage.object_store import ObjectStorageError, get_object_storage
from app.services.storage.qdrant import QdrantStore

router = APIRouter()
logger = logging.getLogger(__name__)

_chat_client = AsyncOpenAI(base_url=settings.VLLM_BASE_URL, api_key="sk-no-key")
_ARTIFACT_IMAGE_KINDS = {"overview", "thumbnail", "patch_preview"}


def _rows(response: Any) -> list[dict[str, Any]]:
    data = getattr(response, "data", None)
    return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []


def _ndjson(event_type: str, data: Any) -> str:
    return json.dumps({"type": event_type, "data": data}, ensure_ascii=False, separators=(",", ":")) + "\n"


def _safe_message_error() -> str:
    return "Grounded response generation is temporarily unavailable. Your message was saved."


def _safe_facts(value: Any) -> list[dict[str, Any]]:
    """Project only approved detector facts into a bounded model-safe shape."""
    if not isinstance(value, list):
        return []
    facts: list[dict[str, Any]] = []
    for item in value[: settings.M5_MAX_CONTEXT_FACTS]:
        if not isinstance(item, dict):
            continue
        label = item.get("label")
        confidence = item.get("confidence")
        bounds = item.get("bounding_box_px")
        if not isinstance(label, str) or not isinstance(confidence, (int, float)) or not isinstance(bounds, dict):
            continue
        copied_bounds = {key: bounds.get(key) for key in ("x_min", "y_min", "x_max", "y_max")}
        if not all(isinstance(number, (int, float)) for number in copied_bounds.values()):
            continue
        facts.append(
            {
                "id": str(item.get("id") or "")[:100] or None,
                "label": label[:160],
                "confidence": max(0.0, min(float(confidence), 1.0)),
                "bounding_box_px": copied_bounds,
            }
        )
    return facts


def _bounded_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value[:limit] if value else None


def _safe_detector_metadata(value: Any) -> dict[str, Any]:
    """Keep provenance useful to chat without exposing arbitrary sidecar JSON."""
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in ("status", "name", "version", "confidence_semantics", "raw_detection_count", "deduplicated_object_count"):
        item = value.get(key)
        if isinstance(item, (str, int, float, bool)):
            result[key] = item
    return result


def _detector_spatial_groups(facts: list[dict[str, Any]], record: dict[str, Any]) -> list[dict[str, Any]]:
    """Summarize detector candidates by coarse scene region for narration."""
    scene = record.get("scene") if isinstance(record.get("scene"), dict) else {}
    raster = scene.get("raster") if isinstance(scene.get("raster"), dict) else {}
    width, height = raster.get("width_px"), raster.get("height_px")
    if not isinstance(width, (int, float)) or not isinstance(height, (int, float)) or width <= 0 or height <= 0:
        return []
    counts: dict[tuple[str, str], int] = {}
    for fact in facts:
        bounds = fact.get("bounding_box_px") if isinstance(fact, dict) else None
        label = fact.get("label") if isinstance(fact, dict) else None
        if not isinstance(bounds, dict) or not isinstance(label, str):
            continue
        try:
            x = (float(bounds["x_min"]) + float(bounds["x_max"])) / 2.0
            y = (float(bounds["y_min"]) + float(bounds["y_max"])) / 2.0
        except (KeyError, TypeError, ValueError):
            continue
        vertical = "upper" if y < height / 3 else "lower" if y > (2 * height) / 3 else "central"
        horizontal = "left" if x < width / 3 else "right" if x > (2 * width) / 3 else "central"
        region = f"{vertical}-{horizontal}" if vertical != "central" and horizontal != "central" else (vertical if horizontal == "central" else horizontal)
        counts[(label, region)] = counts.get((label, region), 0) + 1
    return [
        {"label": label, "region": region, "count": count}
        for (label, region), count in sorted(counts.items())
    ]


def _record_artifact_id(evidence: dict[str, Any] | None) -> str | None:
    metadata = evidence.get("metadata") if isinstance(evidence, dict) else None
    value = metadata.get("record_artifact_id") if isinstance(metadata, dict) else None
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


async def _load_scene_records(
    *,
    evidence_by_scene: dict[str, dict[str, Any]],
    project_id: UUID,
    current_user: CurrentUser,
) -> dict[str, dict[str, Any]]:
    """Read current scene-record artifacts after a fresh ownership check.

    This is an RAG projection over the already-produced durable record.  It
    does not alter ingestion, detector execution, or Qdrant indexing.
    """
    artifact_to_scene = {
        artifact_id: scene_id
        for scene_id, evidence in evidence_by_scene.items()
        if (artifact_id := _record_artifact_id(evidence)) is not None
    }
    if not artifact_to_scene:
        return {}
    response = await _execute(
        lambda: get_supabase()
        .table("scene_artifacts")
        .select("id,scene_id,project_id,owner_id,kind,status,content_type,size_bytes,storage_key")
        .eq("owner_id", current_user.id)
        .eq("project_id", str(project_id))
        .eq("kind", "scene_record")
        .eq("status", "available")
        .in_("id", list(artifact_to_scene))
        .execute(),
        "Evidence data is temporarily unavailable",
    )
    records: dict[str, dict[str, Any]] = {}
    for artifact in _rows(response):
        artifact_id = str(artifact.get("id") or "")
        expected_scene_id = artifact_to_scene.get(artifact_id)
        if expected_scene_id is None or str(artifact.get("scene_id") or "") != expected_scene_id:
            continue
        size_bytes = artifact.get("size_bytes")
        if not isinstance(size_bytes, int) or not 1 <= size_bytes <= settings.M4_MAX_EVIDENCE_RECORD_BYTES:
            continue
        try:
            payload = await run_in_threadpool(
                lambda artifact=artifact: get_object_storage().read_range(
                    str(artifact["storage_key"]), 0, int(artifact["size_bytes"]) - 1
                )
            )
            parsed = json.loads(payload.decode("utf-8"))
        except (ObjectStorageError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            logger.info("Skipping unavailable or malformed scene record", exc_info=True)
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("scene"), dict):
            records[expected_scene_id] = parsed
    return records


def _artifact_read(row: dict[str, Any] | None) -> SceneArtifactRead | None:
    return SceneArtifactRead.model_validate(row) if row is not None else None


def _metadata_filter_values(filters: EvidenceMetadataFilters, *, limit: int) -> dict[str, Any]:
    return {**filters.normalized(), "limit": limit}


async def _owned_search_scenes(
    *,
    project_id: UUID,
    selected_scene_id: UUID | None,
    filters: EvidenceMetadataFilters,
    current_user: CurrentUser,
) -> list[dict[str, Any]]:
    """Resolve PostgreSQL metadata filters after mandatory ownership checks."""
    if selected_scene_id is not None:
        await resolve_owned_scene_in_project(selected_scene_id, project_id, current_user)

    def operation():
        query = (
            get_supabase()
            .table("scenes")
            .select("id,project_id,owner_id,name,status,sensor,acquisition_time,polarizations,metadata")
            .eq("project_id", str(project_id))
            .eq("owner_id", current_user.id)
            .order("created_at", desc=False)
        )
        if selected_scene_id is not None:
            query = query.eq("id", str(selected_scene_id))
        if filters.ready_only:
            query = query.eq("status", "ready")
        if filters.sensor:
            query = query.eq("sensor", filters.sensor)
        if filters.acquisition_from:
            query = query.gte("acquisition_time", filters.acquisition_from.isoformat())
        if filters.acquisition_to:
            query = query.lte("acquisition_time", filters.acquisition_to.isoformat())
        if filters.polarization:
            # PostgREST's contains translates to a JSON/array containment
            # check; the database remains the authority for metadata scope.
            query = query.contains("polarizations", [filters.polarization])
        return query.execute()

    response = await _execute(operation, "Scene data is temporarily unavailable")
    return _rows(response)


async def _query_embedding(
    *,
    query: str,
    owner_id: str,
    project_id: UUID,
    scene_id: UUID | None,
    filters: EvidenceMetadataFilters,
) -> list[float]:
    normalized = normalize_query(query)
    cache_generation = await get_project_evidence_cache_generation(
        owner_id=owner_id,
        project_id=str(project_id),
    )
    key = m5_cache_key(
        "m5-query-embedding",
        owner_id=owner_id,
        project_id=str(project_id),
        scene_id=str(scene_id) if scene_id else None,
        normalized_query_text=normalized,
        normalized_filter_values=filters.normalized(),
        model_version=settings.SARCLIP_MODEL_VERSION,
        index_version=f"{settings.M5_QDRANT_INDEX_VERSION}:cache-{cache_generation}",
    )
    cached = await get_json_cache(key)
    if isinstance(cached, list) and len(cached) == 768 and all(isinstance(item, (int, float)) for item in cached):
        return [float(item) for item in cached]

    try:
        vector = await run_in_threadpool(lambda: SARCLIPEncoder.load_singleton().encode_text(query))
    except Exception as exc:
        logger.info("M5 query embedding failed", exc_info=True)
        raise HTTPException(status_code=503, detail="Evidence retrieval is temporarily unavailable") from exc
    if not isinstance(vector, list) or len(vector) != 768:
        raise HTTPException(status_code=503, detail="Evidence retrieval is temporarily unavailable")
    result = [float(item) for item in vector]
    await set_json_cache(
        key,
        result,
        ttl_seconds=settings.M5_QUERY_EMBEDDING_TTL_SECONDS,
        owner_id=owner_id,
        project_id=str(project_id),
    )
    return result


async def _retrieval_ids(
    *,
    query: str,
    vector: list[float],
    owner_id: str,
    project_id: UUID,
    selected_scene_id: UUID | None,
    allowed_scene_ids: list[str] | None,
    filters: EvidenceMetadataFilters,
    limit: int,
) -> list[dict[str, Any]]:
    cache_generation = await get_project_evidence_cache_generation(
        owner_id=owner_id,
        project_id=str(project_id),
    )
    key = m5_cache_key(
        "m5-retrieval",
        owner_id=owner_id,
        project_id=str(project_id),
        scene_id=str(selected_scene_id) if selected_scene_id else None,
        normalized_query_text=normalize_query(query),
        normalized_filter_values=_metadata_filter_values(filters, limit=limit),
        model_version=settings.SARCLIP_MODEL_VERSION,
        index_version=f"{settings.M5_QDRANT_INDEX_VERSION}:{settings.QDRANT_COLLECTION}:cache-{cache_generation}",
    )
    cached = await get_json_cache(key)
    if isinstance(cached, list):
        valid = [
            {"point_id": str(item["point_id"]), "score": float(item["score"])}
            for item in cached
            if isinstance(item, dict)
            and isinstance(item.get("point_id"), str)
            and isinstance(item.get("score"), (int, float))
        ]
        # Empty retrieval is a legitimate, short-lived result. Cache it too
        # so repeated weak/empty queries do not keep hitting Qdrant.
        if len(valid) == len(cached):
            return valid[:limit]

    try:
        raw_hits = await run_in_threadpool(
            lambda: QdrantStore.get_instance().search_scoped_vectors(
                settings.QDRANT_COLLECTION,
                vector,
                owner_id=owner_id,
                project_id=str(project_id),
                scene_id=str(selected_scene_id) if selected_scene_id else None,
                scene_ids=allowed_scene_ids,
                limit=limit,
            )
        )
    except Exception as exc:
        logger.info("M5 scoped Qdrant query failed", exc_info=True)
        raise HTTPException(status_code=503, detail="Evidence retrieval is temporarily unavailable") from exc

    result: list[dict[str, Any]] = []
    for hit in raw_hits:
        if not isinstance(hit, dict):
            continue
        point_id = hit.get("id")
        score = hit.get("score")
        payload = hit.get("payload")
        # This is defense in depth in addition to the mandatory Qdrant filter.
        if not isinstance(point_id, str) or not isinstance(score, (int, float)) or not isinstance(payload, dict):
            continue
        if str(payload.get("owner_id")) != owner_id or str(payload.get("project_id")) != str(project_id):
            continue
        if selected_scene_id and str(payload.get("scene_id")) != str(selected_scene_id):
            continue
        if allowed_scene_ids is not None and str(payload.get("scene_id")) not in set(allowed_scene_ids):
            continue
        result.append({"point_id": point_id, "score": float(score)})
    await set_json_cache(
        key,
        result,
        ttl_seconds=settings.M5_RETRIEVAL_TTL_SECONDS,
        owner_id=owner_id,
        project_id=str(project_id),
    )
    return result


async def _resolve_evidence_cards(
    *,
    hits: list[dict[str, Any]],
    project_id: UUID,
    selected_scene_id: UUID | None,
    allowed_scene_ids: list[str] | None,
    scenes_by_id: dict[str, dict[str, Any]],
    current_user: CurrentUser,
) -> list[EvidenceSearchCard]:
    score_by_point: dict[str, float] = {}
    for hit in hits:
        try:
            point_id = str(UUID(str(hit["point_id"])))
            score_by_point.setdefault(point_id, float(hit["score"]))
        except (KeyError, TypeError, ValueError):
            continue
    if not score_by_point:
        return []

    def patches_operation():
        query = (
            get_supabase()
            .table("patches")
            .select("id,qdrant_point_id,project_id,scene_id,owner_id,source_artifact_id,preview_artifact_id,row_start,row_end,col_start,col_end,patch_size,status,model_name,model_version")
            .eq("owner_id", current_user.id)
            .eq("project_id", str(project_id))
            .eq("status", "ready")
            .in_("qdrant_point_id", list(score_by_point))
        )
        if selected_scene_id:
            query = query.eq("scene_id", str(selected_scene_id))
        elif allowed_scene_ids is not None:
            query = query.in_("scene_id", allowed_scene_ids)
        return query.execute()

    patches_response = await _execute(patches_operation, "Evidence data is temporarily unavailable")
    patches = _rows(patches_response)
    preview_ids = [str(row["preview_artifact_id"]) for row in patches if row.get("preview_artifact_id")]
    preview_by_id: dict[str, dict[str, Any]] = {}
    if preview_ids:
        preview_response = await _execute(
            lambda: get_supabase()
            .table("scene_artifacts")
            .select("*")
            .eq("owner_id", current_user.id)
            .eq("project_id", str(project_id))
            .eq("kind", "patch_preview")
            .eq("status", "available")
            .in_("id", preview_ids)
            .execute(),
            "Evidence data is temporarily unavailable",
        )
        preview_by_id = {str(item["id"]): item for item in _rows(preview_response)}

    cards_by_point: dict[str, EvidenceSearchCard] = {}
    for patch in patches:
        point_id = str(patch.get("qdrant_point_id") or "")
        scene_id = str(patch.get("scene_id") or "")
        scene = scenes_by_id.get(scene_id)
        # PostgreSQL is canonical: reject malformed joins or rows that escaped
        # a stale Qdrant payload/cache entry.
        if not point_id or point_id not in score_by_point or scene is None:
            continue
        preview = preview_by_id.get(str(patch.get("preview_artifact_id")))
        bounds = PatchBoundsRead(
            row_start=patch["row_start"], row_end=patch["row_end"],
            col_start=patch["col_start"], col_end=patch["col_end"],
        )
        patch_uuid = UUID(str(patch["id"]))
        scene_uuid = UUID(scene_id)
        citation = EvidenceCitation(
            source_type="patch",
            source_id=patch_uuid,
            scene_id=scene_uuid,
            artifact_id=UUID(str(preview["id"])) if preview else None,
            patch_id=patch_uuid,
            bounds=bounds,
            retrieval_score=score_by_point[point_id],
            why_provided="SARCLIP retrieved this authorized patch within the requested project scope.",
            provenance={
                "source_artifact_id": str(patch.get("source_artifact_id") or "") or None,
                "model_name": patch.get("model_name"),
                "model_version": patch.get("model_version"),
            },
        )
        cards_by_point[point_id] = EvidenceSearchCard(
            patch_id=patch_uuid,
            scene_id=scene_uuid,
            scene_name=str(scene.get("name") or "Unnamed scene")[:200],
            bounds=bounds,
            retrieval_score=score_by_point[point_id],
            source_artifact_id=UUID(str(patch["source_artifact_id"])) if patch.get("source_artifact_id") else None,
            preview_artifact=_artifact_read(preview),
            model_name=_bounded_text(patch.get("model_name"), 128),
            model_version=_bounded_text(patch.get("model_version"), 128),
            citation=citation,
        )
    return [cards_by_point[point_id] for point_id in score_by_point if point_id in cards_by_point]


async def _search_authorized(
    request: EvidenceSearchRequest,
    current_user: CurrentUser,
) -> tuple[EvidenceSearchResponse, dict[str, dict[str, Any]]]:
    """Perform all ownership checks before cache or Qdrant access."""
    await resolve_owned_project(request.project_id, current_user)
    scenes = await _owned_search_scenes(
        project_id=request.project_id,
        selected_scene_id=request.scene_id,
        filters=request.filters,
        current_user=current_user,
    )
    scenes_by_id = {str(scene["id"]): scene for scene in scenes}
    if not scenes:
        return (
            EvidenceSearchResponse(
                project_id=request.project_id,
                scene_id=request.scene_id,
                query=request.query,
                filters=request.filters,
                retrieval_state="empty",
                message="No authorized ready scenes match the selected scope and metadata filters.",
            ),
            scenes_by_id,
        )

    # No filter list means the Qdrant project filter is sufficient. Once a
    # metadata filter narrowed the owned scenes, repeat that narrowing in
    # Qdrant as a should group as well.
    filters_narrow_scope = request.scene_id is not None or request.filters.normalized() != EvidenceMetadataFilters().normalized()
    allowed_scene_ids = list(scenes_by_id) if filters_narrow_scope and request.scene_id is None else None
    vector = await _query_embedding(
        query=request.query,
        owner_id=current_user.id,
        project_id=request.project_id,
        scene_id=request.scene_id,
        filters=request.filters,
    )
    limit = min(request.limit, settings.M5_SEARCH_MAX_RESULTS)
    hits = await _retrieval_ids(
        query=request.query,
        vector=vector,
        owner_id=current_user.id,
        project_id=request.project_id,
        selected_scene_id=request.scene_id,
        allowed_scene_ids=allowed_scene_ids,
        filters=request.filters,
        limit=limit,
    )
    cards = await _resolve_evidence_cards(
        hits=hits,
        project_id=request.project_id,
        selected_scene_id=request.scene_id,
        allowed_scene_ids=allowed_scene_ids,
        scenes_by_id=scenes_by_id,
        current_user=current_user,
    )
    is_weak = bool(cards) and max(card.retrieval_score for card in cards) < settings.M5_WEAK_RETRIEVAL_SCORE
    state = "empty" if not cards else "weak" if is_weak else "results"
    message = {
        "empty": "No authorized evidence patches matched this query.",
        "weak": "The retrieved evidence is weak. Treat it as insufficient for a confident conclusion.",
        "results": "Retrieved authorized evidence patches for this scope.",
    }[state]
    return (
        EvidenceSearchResponse(
            project_id=request.project_id,
            scene_id=request.scene_id,
            query=request.query,
            filters=request.filters,
            cards=cards,
            retrieval_state=state,
            message=message,
        ),
        scenes_by_id,
    )


@router.post("/search", response_model=EvidenceSearchResponse)
async def search_evidence(
    request: EvidenceSearchRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> EvidenceSearchResponse:
    """Search only a verified owner/project scope, then resolve DB evidence."""
    response, _ = await _search_authorized(request, current_user)
    return response


def _conversation_read(row: dict[str, Any]) -> ConversationRead:
    if not row.get("project_id"):
        raise ValueError("Unscoped legacy conversation cannot be returned by M5")
    return ConversationRead.model_validate(row)


def _citations(value: Any) -> list[EvidenceCitation]:
    if not isinstance(value, list):
        return []
    citations: list[EvidenceCitation] = []
    for item in value[:100]:
        try:
            citations.append(EvidenceCitation.model_validate(item))
        except (TypeError, ValueError):
            continue
    return citations


def _message_read(row: dict[str, Any]) -> ConversationMessageRead:
    return ConversationMessageRead.model_validate({**row, "citations": _citations(row.get("sources"))})


@router.post("/conversations", response_model=ConversationRead, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    request: ConversationCreateRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> ConversationRead:
    await resolve_owned_project(request.project_id, current_user)
    if request.scene_id is not None:
        await resolve_owned_scene_in_project(request.scene_id, request.project_id, current_user)
    payload = {
        "owner_id": current_user.id,
        "user_id": current_user.id,
        "project_id": str(request.project_id),
        "scene_id": str(request.scene_id) if request.scene_id else None,
        "title": request.title or "Untitled conversation",
        "status": "active",
        "metadata": {"protocol": "m5-ndjson", "evidence_bound": True},
    }
    response = await _execute(
        lambda: get_supabase().table("conversations").insert(payload).execute(),
        "Conversation data is temporarily unavailable",
    )
    row = _first_row(response)
    if row is None:
        raise HTTPException(status_code=503, detail="Conversation could not be created")
    return _conversation_read(row)


@router.get("/projects/{project_id}/conversations", response_model=list[ConversationRead])
async def list_conversations(
    project_id: UUID,
    scene_id: UUID | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
) -> list[ConversationRead]:
    await resolve_owned_project(project_id, current_user)
    if scene_id is not None:
        await resolve_owned_scene_in_project(scene_id, project_id, current_user)

    def operation():
        query = (
            get_supabase()
            .table("conversations")
            .select("*")
            .eq("owner_id", current_user.id)
            .eq("project_id", str(project_id))
            .eq("status", "active")
            .order("updated_at", desc=True)
            .limit(100)
        )
        if scene_id is not None:
            query = query.eq("scene_id", str(scene_id))
        return query.execute()

    response = await _execute(operation, "Conversation data is temporarily unavailable")
    conversations: list[ConversationRead] = []
    for row in _rows(response):
        try:
            conversations.append(_conversation_read(row))
        except (TypeError, ValueError):
            logger.warning("Ignoring malformed conversation row in M5 list")
    return conversations


@router.get("/conversations/{conversation_id}/messages", response_model=ConversationMessagePage)
async def list_conversation_messages(
    conversation_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> ConversationMessagePage:
    conversation = await resolve_owned_conversation(conversation_id, current_user)
    if not conversation.get("project_id"):
        raise HTTPException(status_code=409, detail="This legacy conversation is not scoped to a project")
    response = await _execute(
        lambda: get_supabase()
        .table("messages")
        .select("*")
        .eq("conversation_id", str(conversation_id))
        .eq("owner_id", current_user.id)
        .eq("project_id", str(conversation["project_id"]))
        .order("created_at", desc=False)
        .limit(200)
        .execute(),
        "Conversation data is temporarily unavailable",
    )
    return ConversationMessagePage(items=[_message_read(row) for row in _rows(response)])


async def _authorized_conversation_scope(
    *,
    conversation_id: UUID,
    requested_scene_id: UUID | None,
    current_user: CurrentUser,
) -> tuple[dict[str, Any], UUID, UUID | None]:
    conversation = await resolve_owned_conversation(conversation_id, current_user)
    project_raw = conversation.get("project_id")
    if not project_raw:
        raise HTTPException(status_code=409, detail="This legacy conversation is not scoped to a project")
    project_id = UUID(str(project_raw))
    await resolve_owned_project(project_id, current_user)
    conversation_scene = UUID(str(conversation["scene_id"])) if conversation.get("scene_id") else None
    if conversation_scene and requested_scene_id and conversation_scene != requested_scene_id:
        raise HTTPException(status_code=409, detail="This conversation is already scoped to another scene")
    # The database copies the parent conversation scope to every message.
    # Allowing a project-scoped conversation to carry an ad-hoc scene here
    # would therefore either fail its trigger or create misleading history.
    # A user who needs scene-scoped history must start a scene-scoped
    # conversation; project-scoped history stays project-scoped.
    if conversation_scene is None and requested_scene_id is not None:
        raise HTTPException(
            status_code=409,
            detail="This conversation is project-scoped. Start a new scene-scoped conversation to narrow it.",
        )
    selected_scene_id = conversation_scene
    if selected_scene_id:
        await resolve_owned_scene_in_project(selected_scene_id, project_id, current_user)
    return conversation, project_id, selected_scene_id


async def _load_rag_context(
    *,
    search: EvidenceSearchResponse,
    scenes_by_id: dict[str, dict[str, Any]],
    current_user: CurrentUser,
) -> dict[str, Any]:
    """Build/cache bounded derived context; never cache artifact records/bytes."""
    cache_generation = await get_project_evidence_cache_generation(
        owner_id=current_user.id,
        project_id=str(search.project_id),
    )
    cache_key = m5_cache_key(
        "m5-rag-context",
        owner_id=current_user.id,
        project_id=str(search.project_id),
        scene_id=str(search.scene_id) if search.scene_id else None,
        normalized_query_text=normalize_query(search.query),
        normalized_filter_values=_metadata_filter_values(search.filters, limit=len(search.cards)),
        model_version=f"{settings.SARCLIP_MODEL_VERSION}:{settings.SARCHAT_MODEL_ID}:scene-first-rag-v2",
        index_version=f"{settings.M5_QDRANT_INDEX_VERSION}:{settings.QDRANT_COLLECTION}:cache-{cache_generation}",
    )
    cached = await get_json_cache(cache_key)
    if isinstance(cached, dict) and isinstance(cached.get("citations"), list) and isinstance(cached.get("scene_context"), list):
        return cached

    scene_ids = list(dict.fromkeys([str(card.scene_id) for card in search.cards] + ([str(search.scene_id)] if search.scene_id else [])))
    scene_ids = [value for value in scene_ids if value in scenes_by_id]
    evidence_by_scene: dict[str, dict[str, Any]] = {}
    scene_records: dict[str, dict[str, Any]] = {}
    overview_by_scene: dict[str, dict[str, Any]] = {}
    if scene_ids:
        evidence_response = await _execute(
            lambda: get_supabase()
            .table("scene_evidence_records")
            .select("id,scene_id,summary,facts,model_name,model_version,metadata")
            .eq("owner_id", current_user.id)
            .eq("project_id", str(search.project_id))
            .eq("status", "ready")
            .eq("is_current", True)
            .in_("scene_id", scene_ids)
            .execute(),
            "Evidence data is temporarily unavailable",
        )
        evidence_by_scene = {str(row["scene_id"]): row for row in _rows(evidence_response)}
        scene_records = await _load_scene_records(
            evidence_by_scene=evidence_by_scene,
            project_id=search.project_id,
            current_user=current_user,
        )
        overview_response = await _execute(
            lambda: get_supabase()
            .table("scene_artifacts")
            .select("id,scene_id,project_id,owner_id,kind,status,content_type,size_bytes,created_at")
            .eq("owner_id", current_user.id)
            .eq("project_id", str(search.project_id))
            .eq("kind", "overview")
            .eq("status", "available")
            .in_("scene_id", scene_ids)
            .order("created_at", desc=True)
            .execute(),
            "Evidence data is temporarily unavailable",
        )
        for row in _rows(overview_response):
            overview_by_scene.setdefault(str(row["scene_id"]), row)

    citations: list[dict[str, Any]] = [card.citation.model_dump(mode="json") for card in search.cards]
    scene_context: list[dict[str, Any]] = []
    overview_artifact_ids: list[str] = []
    for scene_id in scene_ids:
        scene = scenes_by_id[scene_id]
        evidence = evidence_by_scene.get(scene_id)
        record = scene_records.get(scene_id, {})
        record_context = record.get("context") if isinstance(record.get("context"), dict) else {}
        record_caption = record.get("model_generated_caption") if isinstance(record.get("model_generated_caption"), dict) else {}
        record_detector = record.get("detector") if isinstance(record.get("detector"), dict) else {}
        fallback_detector = (evidence.get("metadata") or {}).get("detector") if isinstance(evidence, dict) and isinstance(evidence.get("metadata"), dict) else {}
        record_objects = record.get("objects") if isinstance(record.get("objects"), list) else None
        detector_facts = _safe_facts(record_objects if record_objects is not None else (evidence.get("facts") if evidence else None))
        observation = _bounded_text(record_caption.get("text"), 2000) or _bounded_text(evidence.get("summary") if evidence else None, 2000)
        scene_context.append(
            {
                "scene_id": scene_id,
                "name": _bounded_text(scene.get("name"), 200) or "Unnamed scene",
                "sensor": _bounded_text(scene.get("sensor"), 128),
                "acquisition_time": str(scene.get("acquisition_time") or "") or None,
                "polarizations": [str(item)[:32] for item in list(scene.get("polarizations") or [])[:8]],
                "land_water": record_context.get("land_water") if isinstance(record_context.get("land_water"), dict) else None,
                "detector": _safe_detector_metadata(record_detector or fallback_detector),
                "validated_detector_facts": detector_facts,
                "detector_spatial_groups": _detector_spatial_groups(detector_facts, record),
                "model_observation": observation,
                "model_name": _bounded_text(record_caption.get("model_name"), 128) or _bounded_text(evidence.get("model_name") if evidence else None, 128),
                "model_version": _bounded_text(record_caption.get("model_version"), 128) or _bounded_text(evidence.get("model_version") if evidence else None, 128),
                "limitations": [item[:500] for item in record.get("limitations", [])[:8] if isinstance(item, str)],
                "scene_record_available": bool(record),
            }
        )
        citations.append(
            EvidenceCitation(
                source_type="metadata",
                source_id=scene_id,
                scene_id=UUID(scene_id),
                why_provided="Authorized scene acquisition metadata provides context for the retrieved evidence.",
                provenance={"source": "scene metadata"},
            ).model_dump(mode="json")
        )
        record_artifact_id = _record_artifact_id(evidence)
        if record_artifact_id and record:
            citations.append(
                EvidenceCitation(
                    source_type="metadata",
                    source_id=scene_id,
                    scene_id=UUID(scene_id),
                    artifact_id=UUID(record_artifact_id),
                    why_provided="The authorized scene record supplies detector provenance and conservative scene context.",
                    provenance={"kind": "scene_record", "record_artifact_id": record_artifact_id},
                ).model_dump(mode="json")
            )
        if evidence and observation:
            citations.append(
                EvidenceCitation(
                    source_type="model_observation",
                    source_id=str(evidence["id"]),
                    scene_id=UUID(scene_id),
                    why_provided="A model-generated scene observation was provided as non-verified context.",
                    provenance={"model_name": evidence.get("model_name"), "model_version": evidence.get("model_version"), "verified_object_source": False},
                ).model_dump(mode="json")
            )
        if evidence and detector_facts:
            citations.append(
                EvidenceCitation(
                    source_type="validated_detector_evidence",
                    source_id=str(evidence["id"]),
                    scene_id=UUID(scene_id),
                    why_provided="Approved detector sidecar facts were provided as validated evidence.",
                    provenance={"record_id": str(evidence["id"])},
                ).model_dump(mode="json")
            )
        overview = overview_by_scene.get(scene_id)
        if overview and len(overview_artifact_ids) < settings.M5_MAX_OVERVIEWS_PER_PROMPT:
            overview_artifact_ids.append(str(overview["id"]))
            citations.append(
                EvidenceCitation(
                    source_type="overview",
                    source_id=str(overview["id"]),
                    scene_id=UUID(scene_id),
                    artifact_id=UUID(str(overview["id"])),
                    why_provided="Authorized overview image provides broad scene context.",
                    provenance={"kind": "overview"},
                ).model_dump(mode="json")
            )

    patch_preview_ids = [str(card.preview_artifact.id) for card in search.cards if card.preview_artifact]
    context = {
        "citations": citations[:100],
        "scene_context": scene_context[: max(1, settings.M5_MAX_OVERVIEWS_PER_PROMPT + settings.M5_MAX_PATCH_IMAGES_PER_PROMPT)],
        "patches": [
            {
                "patch_id": str(card.patch_id), "scene_id": str(card.scene_id), "scene_name": card.scene_name,
                "bounds": card.bounds.model_dump(), "retrieval_score": card.retrieval_score,
                "model_name": card.model_name, "model_version": card.model_version,
                "preview_artifact_id": str(card.preview_artifact.id) if card.preview_artifact else None,
            }
            for card in search.cards
        ],
        # IDs are not artifacts: each request re-authorizes/reloads the DB
        # artifact row before private object bytes are read.
        "overview_artifact_ids": overview_artifact_ids,
        "patch_preview_artifact_ids": patch_preview_ids[: settings.M5_MAX_PATCH_IMAGES_PER_PROMPT],
    }
    await set_json_cache(
        cache_key,
        context,
        ttl_seconds=settings.M5_RAG_CONTEXT_TTL_SECONDS,
        owner_id=current_user.id,
        project_id=str(search.project_id),
    )
    return context


def _bounded_context_text(context: dict[str, Any]) -> str:
    lines = ["AUTHORIZED EVIDENCE CONTEXT (do not invent evidence beyond this block):"]
    for scene in context.get("scene_context", []):
        if not isinstance(scene, dict):
            continue
        lines.append(
            "SCENE {name} [{scene_id}] sensor={sensor} acquisition={acquisition} polarizations={polarizations}".format(
                name=scene.get("name") or "Unnamed", scene_id=scene.get("scene_id"),
                sensor=scene.get("sensor") or "unknown", acquisition=scene.get("acquisition_time") or "unknown",
                polarizations=", ".join(scene.get("polarizations") or []) or "unknown",
            )
        )
        land_water = scene.get("land_water")
        if isinstance(land_water, dict):
            lines.append(
                "SCENE CONTEXT (heuristic, not segmentation): "
                + json.dumps(land_water, separators=(",", ":"))
            )
        detector = scene.get("detector")
        if isinstance(detector, dict) and detector:
            lines.append("DETECTOR PROVENANCE: " + json.dumps(detector, separators=(",", ":")))
        observation = _bounded_text(scene.get("model_observation"), 2000)
        if observation:
            lines.append(f"MODEL OBSERVATION (not a detection): {observation}")
        facts = scene.get("validated_detector_facts")
        if isinstance(facts, list) and facts:
            lines.append("VALIDATED DETECTOR FACTS: " + json.dumps(facts[: settings.M5_MAX_CONTEXT_FACTS], separators=(",", ":")))
        spatial_groups = scene.get("detector_spatial_groups")
        if isinstance(spatial_groups, list) and spatial_groups:
            lines.append("DETECTOR SPATIAL GROUPS (coarse image regions): " + json.dumps(spatial_groups, separators=(",", ":")))
        limitations = scene.get("limitations")
        if isinstance(limitations, list):
            for limitation in limitations[:4]:
                if isinstance(limitation, str):
                    lines.append(f"LIMITATION: {limitation[:500]}")
    for patch in context.get("patches", []):
        if isinstance(patch, dict):
            lines.append(
                "RETRIEVED PATCH {patch_id} scene={scene_id} bounds={bounds} score={score:.3f} model={model}".format(
                    patch_id=patch.get("patch_id"), scene_id=patch.get("scene_id"), bounds=json.dumps(patch.get("bounds") or {}, separators=(",", ":")),
                    score=float(patch.get("retrieval_score") or 0.0), model=patch.get("model_name") or "unknown",
                )
            )
    return "\n".join(lines)[: settings.M5_MAX_CONTEXT_CHARS]


def _selected_scene_context(context: dict[str, Any], scene_id: UUID) -> dict[str, Any] | None:
    for item in context.get("scene_context", []):
        if isinstance(item, dict) and str(item.get("scene_id")) == str(scene_id):
            return item
    return None


def _scene_first_answer(
    *,
    intent: ChatIntent,
    query: str,
    context: dict[str, Any],
    scene_id: UUID,
) -> str | None:
    """Return answers that should never depend on similarity retrieval."""
    scene = _selected_scene_context(context, scene_id)
    if scene is None:
        return None
    if intent in {"detector_count", "detector_presence", "detector_location"}:
        return detector_answer(
            query=query,
            facts=scene.get("validated_detector_facts") or [],
            detector=scene.get("detector") if isinstance(scene.get("detector"), dict) else None,
            spatial_groups=scene.get("detector_spatial_groups") or [],
        )
    if intent == "environment":
        return environment_answer(
            query,
            scene.get("land_water") if isinstance(scene.get("land_water"), dict) else None,
        )
    return None


def _overview_quadrant_images(image_bytes: bytes) -> list[dict[str, Any]]:
    """Make small NW/NE/SW/SE samples from one authorized overview image."""
    if settings.M5_SCENE_QUADRANT_SAMPLES == 0:
        return []
    try:
        with Image.open(BytesIO(image_bytes)) as source:
            image = source.convert("RGB")
            if image.width < 2 or image.height < 2:
                return []
            mid_x, mid_y = image.width // 2, image.height // 2
            boxes = (
                ("north-west", (0, 0, mid_x, mid_y)),
                ("north-east", (mid_x, 0, image.width, mid_y)),
                ("south-west", (0, mid_y, mid_x, image.height)),
                ("south-east", (mid_x, mid_y, image.width, image.height)),
            )
            samples: list[dict[str, Any]] = []
            for _label, box in boxes[: settings.M5_SCENE_QUADRANT_SAMPLES]:
                crop = image.crop(box)
                crop.thumbnail(
                    (settings.M5_SCENE_QUADRANT_MAX_PIXELS, settings.M5_SCENE_QUADRANT_MAX_PIXELS),
                    Image.Resampling.LANCZOS,
                )
                encoded = BytesIO()
                crop.save(encoded, format="JPEG", quality=85, optimize=True)
                samples.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/jpeg;base64," + base64.b64encode(encoded.getvalue()).decode("ascii")
                        },
                    }
                )
            return samples
    except (OSError, ValueError):
        logger.info("Skipping invalid overview quadrant samples", exc_info=True)
        return []


async def _load_authorized_context_images(
    *,
    context: dict[str, Any],
    project_id: UUID,
    current_user: CurrentUser,
    include_scene_quadrants: bool = False,
) -> list[dict[str, Any]]:
    artifact_ids = list(dict.fromkeys(
        [str(item) for item in context.get("overview_artifact_ids", [])]
        + [str(item) for item in context.get("patch_preview_artifact_ids", [])]
    ))
    artifact_ids = artifact_ids[: settings.M5_MAX_OVERVIEWS_PER_PROMPT + settings.M5_MAX_PATCH_IMAGES_PER_PROMPT]
    if not artifact_ids:
        return []
    response = await _execute(
        lambda: get_supabase()
        .table("scene_artifacts")
        .select("id,scene_id,project_id,owner_id,kind,status,content_type,size_bytes,storage_key")
        .eq("owner_id", current_user.id)
        .eq("project_id", str(project_id))
        .eq("status", "available")
        .in_("id", artifact_ids)
        .execute(),
        "Evidence images are temporarily unavailable",
    )
    artifacts = {str(row["id"]): row for row in _rows(response)}
    images: list[dict[str, Any]] = []
    quadrants_added = False
    for artifact_id in artifact_ids:
        artifact = artifacts.get(artifact_id)
        if not artifact:
            continue
        if artifact.get("kind") not in _ARTIFACT_IMAGE_KINDS or not str(artifact.get("content_type") or "").startswith("image/"):
            continue
        size_bytes = artifact.get("size_bytes")
        if not isinstance(size_bytes, int) or size_bytes < 1 or size_bytes > settings.M5_MAX_IMAGE_BYTES:
            continue
        try:
            image_bytes = await run_in_threadpool(
                lambda artifact=artifact: get_object_storage().read_range(str(artifact["storage_key"]), 0, int(artifact["size_bytes"]) - 1)
            )
        except (ObjectStorageError, RuntimeError, ValueError):
            logger.info("Skipping unavailable authorized RAG image", exc_info=True)
            continue
        if len(image_bytes) > settings.M5_MAX_IMAGE_BYTES:
            continue
        encoded = base64.b64encode(image_bytes).decode("ascii")
        images.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{artifact['content_type']};base64,{encoded}"},
            }
        )
        if include_scene_quadrants and not quadrants_added and artifact.get("kind") == "overview":
            images.extend(_overview_quadrant_images(image_bytes))
            quadrants_added = True
    return images


async def _conversation_history(conversation_id: UUID, current_user: CurrentUser) -> list[dict[str, str]]:
    response = await _execute(
        lambda: get_supabase()
        .table("messages")
        .select("role,content")
        .eq("conversation_id", str(conversation_id))
        .eq("owner_id", current_user.id)
        .in_("status", ["complete", "streaming"])
        .order("created_at", desc=True)
        .limit(settings.M5_MAX_HISTORY_MESSAGES)
        .execute(),
        "Conversation data is temporarily unavailable",
    )
    history: list[dict[str, str]] = []
    budget = settings.M5_MAX_HISTORY_CHARS
    for row in reversed(_rows(response)):
        role = str(row.get("role") or "")
        content = _bounded_text(row.get("content"), min(2000, budget))
        if role not in {"user", "assistant"} or not content or budget <= 0:
            continue
        history.append({"role": role, "content": content})
        budget -= len(content)
    return history


async def _insert_message(
    *,
    conversation_id: UUID,
    project_id: UUID,
    scene_id: UUID | None,
    current_user: CurrentUser,
    role: str,
    content: str,
    mode: str | None,
    status_value: str,
    citations: list[dict[str, Any]] | None = None,
    error_detail: str | None = None,
) -> dict[str, Any] | None:
    payload = {
        "conversation_id": str(conversation_id),
        "owner_id": current_user.id,
        "project_id": str(project_id),
        "scene_id": str(scene_id) if scene_id else None,
        "role": role,
        "content": content,
        "mode": mode,
        "status": status_value,
        "sources": citations or [],
        "error_detail": error_detail,
    }
    response = await _execute(
        lambda: get_supabase().table("messages").insert(payload).execute(),
        "Conversation data is temporarily unavailable",
    )
    return _first_row(response)


@router.post("/conversations/{conversation_id}/stream")
async def stream_grounded_chat(
    conversation_id: UUID,
    request: ChatStreamRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> StreamingResponse:
    """Use the sole V1 stream protocol: newline-delimited JSON (NDJSON)."""
    _, project_id, selected_scene_id = await _authorized_conversation_scope(
        conversation_id=conversation_id,
        requested_scene_id=request.scene_id,
        current_user=current_user,
    )
    search_request = EvidenceSearchRequest(
        project_id=project_id,
        scene_id=selected_scene_id,
        query=request.query,
        limit=request.limit,
        filters=request.filters,
    )
    # A scene-scoped chat starts with the durable scene record.  This avoids
    # spending GPU/latency on a text-to-patch search for questions such as
    # "how many bridges" or "is there vegetation", where a retrieval hit is
    # neither the authoritative source nor a reliable classifier.
    preloaded_context: dict[str, Any] | None = None
    scene_intent: ChatIntent | None = None
    if selected_scene_id is not None:
        scoped_scenes = await _owned_search_scenes(
            project_id=project_id,
            selected_scene_id=selected_scene_id,
            filters=request.filters,
            current_user=current_user,
        )
        scenes_by_id = {str(scene["id"]): scene for scene in scoped_scenes}
        if scoped_scenes:
            scene_record_search = EvidenceSearchResponse(
                project_id=project_id,
                scene_id=selected_scene_id,
                query=request.query,
                filters=request.filters,
                retrieval_state="empty",
                message="Loaded the selected scene record before optional patch retrieval.",
            )
            preloaded_context = await _load_rag_context(
                search=scene_record_search,
                scenes_by_id=scenes_by_id,
                current_user=current_user,
            )
            selected_context = _selected_scene_context(preloaded_context, selected_scene_id) or {}
            detector_labels = [
                str(item.get("label"))
                for item in selected_context.get("validated_detector_facts", [])
                if isinstance(item, dict) and isinstance(item.get("label"), str)
            ]
            scene_intent = classify_scene_query(request.query, detector_labels)
            if scene_intent in {"visual_evidence", "detector_location"}:
                # Patch retrieval remains valuable for explicitly visual or
                # location-oriented requests, but augments the scene record.
                search, scenes_by_id = await _search_authorized(search_request, current_user)
                if scene_intent == "detector_location":
                    preloaded_context = await _load_rag_context(
                        search=search,
                        scenes_by_id=scenes_by_id,
                        current_user=current_user,
                    )
                else:
                    preloaded_context = None
            else:
                search = scene_record_search
        else:
            # Keep the existing scoped search behavior for a non-ready or
            # filtered-out scene, which produces the normal safe empty state.
            search, scenes_by_id = await _search_authorized(search_request, current_user)
    else:
        # Project-scoped chat has no single canonical scene record, so vector
        # retrieval still selects the scene(s) to discuss.
        search, scenes_by_id = await _search_authorized(search_request, current_user)
    user_message = await _insert_message(
        conversation_id=conversation_id,
        project_id=project_id,
        scene_id=selected_scene_id,
        current_user=current_user,
        role="user",
        content=request.query,
        mode="grounded",
        status_value="complete",
    )
    if user_message is None:
        raise HTTPException(status_code=503, detail="Conversation message could not be saved")

    async def event_stream() -> AsyncIterator[str]:
        citations = (
            preloaded_context.get("citations", [])
            if isinstance(preloaded_context, dict) and isinstance(preloaded_context.get("citations"), list)
            else [card.citation.model_dump(mode="json") for card in search.cards]
        )
        citations_emitted = False
        yield _ndjson("conversation", {"id": str(conversation_id), "project_id": str(project_id), "scene_id": str(selected_scene_id) if selected_scene_id else None})
        yield _ndjson("status", {
            "state": "scene_record" if preloaded_context is not None else "retrieved",
            "retrieval_state": search.retrieval_state,
            "message": search.message,
        })
        direct_answer = (
            _scene_first_answer(
                intent=scene_intent,
                query=request.query,
                context=preloaded_context,
                scene_id=selected_scene_id,
            )
            if selected_scene_id is not None and scene_intent is not None and preloaded_context is not None
            else None
        )
        if direct_answer:
            assistant = await _insert_message(
                conversation_id=conversation_id,
                project_id=project_id,
                scene_id=selected_scene_id,
                current_user=current_user,
                role="assistant",
                content=direct_answer,
                mode=f"scene_record_{scene_intent}",
                status_value="complete",
                citations=citations,
            )
            yield _ndjson("citations", citations)
            yield _ndjson("text", direct_answer)
            yield _ndjson("done", {"status": "complete", "message_id": str(assistant["id"]) if assistant else None})
            return

        # A selected scene can still be described from its overview and scene
        # record even when no semantic patch matched the wording. Only an
        # explicitly patch-oriented request is gated by retrieval quality.
        retrieval_required = selected_scene_id is None or scene_intent == "visual_evidence"
        if retrieval_required and search.retrieval_state in {"empty", "weak"}:
            insufficient = (
                "There is insufficient authorized evidence to provide a confident answer. "
                "Try a more specific query, select a processed scene, or review the available evidence cards."
            )
            assistant = await _insert_message(
                conversation_id=conversation_id,
                project_id=project_id,
                scene_id=selected_scene_id,
                current_user=current_user,
                role="assistant",
                content=insufficient,
                mode="insufficient_evidence",
                status_value="complete",
                citations=citations,
            )
            yield _ndjson("citations", citations)
            citations_emitted = True
            yield _ndjson("text", insufficient)
            yield _ndjson("done", {"status": "complete", "message_id": str(assistant["id"]) if assistant else None})
            return

        full_response = ""
        assistant_status = "complete"
        assistant_mode = "grounded_rag"
        context: dict[str, Any] = {}
        try:
            context = preloaded_context or await _load_rag_context(
                search=search,
                scenes_by_id=scenes_by_id,
                current_user=current_user,
            )
            citations = context.get("citations") if isinstance(context.get("citations"), list) else citations
            yield _ndjson("citations", citations)
            citations_emitted = True
            yield _ndjson("status", {"state": "generating"})
            history = await _conversation_history(conversation_id, current_user)
            # The current user turn was persisted before opening the stream so
            # a reload cannot lose it. Do not pass it twice to the model.
            if history and history[-1].get("role") == "user" and history[-1].get("content") == request.query:
                history.pop()
            images = await _load_authorized_context_images(
                context=context,
                project_id=project_id,
                current_user=current_user,
                include_scene_quadrants=selected_scene_id is not None and scene_intent == "scene_description",
            )
            system = (
                "You are SARChat, the final SAR scene narrator. The authorized scene record is the primary source; "
                "then use the full overview and its quadrant samples for scene-level visual observations. Retrieved patches are optional "
                "supporting visual evidence only. SARCLIP selected them by embedding similarity and is never an object detector. "
                "Keep three evidence classes distinct: (1) conservative scene context, including the land/water heuristic; "
                "(2) detector-backed object candidates, which may only come from explicitly supplied validated detector facts; and "
                "(3) uncertain observations, such as bright points, elongated returns, linear structures, texture differences, or wake-like patterns. "
                "Never turn a caption, visual impression, bright return, model observation, or SARCLIP retrieval result into a verified object, "
                "a bounding box, a count, land-cover class, activity, intent, temporal change, vessel type, or anomaly label. "
                "Do not claim fishing, loitering, military activity, vegetation, buildings, ships, aircraft, vehicles, bridges, ports, tanks, "
                "or any other object unless the supplied detector facts explicitly support that claim. "
                "For 'describe', 'explain', or 'what is happening' questions, answer with the compact sections 'Scene context', "
                "'Detector-backed objects', and 'Uncertain observations'. State that a land/water estimate is heuristic, "
                "say when no detector facts are available, and make no claim beyond a single acquisition. "
                "For a narrow question, answer it first and then name the relevant evidence class. Refer to scene names or patch IDs when useful."
            )
            user_content: list[dict[str, Any]] = [
                {
                    "type": "text",
                    "text": (
                        f"Question: {request.query}\n\n{_bounded_context_text(context)}\n\n"
                        "When present, visual inputs are ordered as: full overview; north-west, north-east, "
                        "south-west, and south-east overview quadrants; then optional retrieved patch previews."
                    ),
                },
                *images,
            ]
            messages: list[dict[str, Any]] = [{"role": "system", "content": system}, *history, {"role": "user", "content": user_content}]
            stream = await asyncio.wait_for(
                _chat_client.chat.completions.create(
                    model=settings.SARCHAT_MODEL_ID,
                    messages=messages,
                    stream=True,
                    max_tokens=settings.M5_OUTPUT_MAX_TOKENS,
                    temperature=0.2,
                ),
                timeout=settings.M5_GENERATION_TIMEOUT_SECONDS,
            )
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    delta = chunk.choices[0].delta.content
                    full_response += delta
                    yield _ndjson("text", delta)
        except asyncio.CancelledError:
            assistant_status = "cancelled"
            raise
        except Exception:
            logger.info("M5 grounded generation failed", exc_info=True)
            assistant_status = "failed"
            assistant_mode = "grounded_rag_failed"
            if not full_response:
                full_response = _safe_message_error()
            if not citations_emitted:
                # Even an error/partial answer carries an explicit source set
                # (possibly empty) so the client never has to infer evidence.
                yield _ndjson("citations", citations)
            yield _ndjson("error", {"code": "generation_unavailable", "message": _safe_message_error()})
        finally:
            try:
                assistant = await _insert_message(
                    conversation_id=conversation_id,
                    project_id=project_id,
                    scene_id=selected_scene_id,
                    current_user=current_user,
                    role="assistant",
                    content=full_response or _safe_message_error(),
                    mode=assistant_mode,
                    status_value=assistant_status,
                    citations=citations,
                    error_detail="generation_unavailable" if assistant_status == "failed" else None,
                )
            except Exception:
                logger.exception("Unable to persist M5 assistant message")
                assistant = None
            if assistant_status != "cancelled":
                yield _ndjson("done", {"status": assistant_status, "message_id": str(assistant["id"]) if assistant else None})

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
