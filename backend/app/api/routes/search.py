import os
import io
import json
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from typing import Optional
from PIL import Image
import rasterio
from rasterio.windows import Window

from app.services.models.sarclip_encoder import SARCLIPEncoder
from app.services.storage.qdrant import QdrantStore
from app.services.processing.patch_pipeline import preprocess_patch, PATCH_SIZE, get_base64_patches, get_spatial_label
from app.services.processing.query_router import (
    stage_1_keyword_pass,
    stage_2_score_spread,
    resolve_routing,
    check_cached_macro
)
from app.services.session_cache import get_session_dir, touch_session
from app.services.database import get_supabase
from app.api.deps import (
    CurrentUser,
    get_current_user,
    resolve_owned_conversation,
    resolve_owned_scene,
)
import base64
from openai import AsyncOpenAI
from app.core.config import settings, vllm_settings

logger = logging.getLogger(__name__)
client = AsyncOpenAI(base_url=settings.VLLM_BASE_URL, api_key="sk-no-key")

router = APIRouter()

class SearchQuery(BaseModel):
    query: str
    scene_id: UUID
    limit: int = 10

@router.post("/")
async def search_patches(
    request: SearchQuery,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Search patches only within a scene owned by the verified user.

    The old global collection search was unsafe once vectors became shared
    between users. M1 makes scope mandatory even while this legacy route is
    kept for local compatibility.
    """
    if not request.query:
        raise HTTPException(status_code=400, detail="Query cannot be empty")

    scene = await resolve_owned_scene(request.scene_id, current_user)
        
    encoder = SARCLIPEncoder.get()
    try:
        query_vector = encoder.encode_text(request.query)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to encode text query: {e}")
        
    store = QdrantStore.get_instance()
    try:
        results = store.search_scoped_vectors(
            collection_name=settings.QDRANT_COLLECTION,
            query_vector=query_vector,
            limit=request.limit,
            owner_id=current_user.id,
            project_id=str(scene["project_id"]),
            scene_id=str(request.scene_id),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to query Qdrant: {e}")
        
    return {"results": results}

@router.get("/patch/{session_id}")
async def get_patch_image(session_id: str, row: int, col: int):
    session_dir = get_session_dir(session_id)
    vrt_path = os.path.join(session_dir, "stacked.vrt")
    
    if not os.path.exists(vrt_path):
        raise HTTPException(status_code=404, detail="Session or VRT file not found.")
        
    touch_session(session_id)
        
    try:
        with rasterio.open(vrt_path) as dataset:
            window = Window(col, row, PATCH_SIZE, PATCH_SIZE)
            raw_patch = dataset.read(window=window)
            
        processed = preprocess_patch(raw_patch)
        if processed is None:
            raise HTTPException(status_code=404, detail="Patch is mostly nodata")
            
        image = Image.fromarray(processed)
        buf = io.BytesIO()
        image.save(buf, format="JPEG")
        return Response(content=buf.getvalue(), media_type="image/jpeg")
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to extract patch: {e}")

class RagQuery(BaseModel):
    query: str
    session_id: str
    limit: int = 3
    history: list = []
    conversation_id: Optional[UUID] = None

@router.post("/rag/chat")
async def rag_chat(
    request: RagQuery,
    current_user: CurrentUser = Depends(get_current_user),
):
    if not request.query:
        raise HTTPException(status_code=400, detail="Query cannot be empty")

    supabase = get_supabase()

    # Handle Conversation ID
    conversation_id = request.conversation_id
    if not conversation_id:
        # Create new conversation
        title = request.query[:50] + "..." if len(request.query) > 50 else request.query
        conv_resp = supabase.table("conversations").insert({
            "user_id": current_user.id,
            "owner_id": current_user.id,
            "title": title
        }).execute()
        conversation_id = conv_resp.data[0]["id"]
    else:
        await resolve_owned_conversation(conversation_id, current_user)

    conversation_id = str(conversation_id)
    
    # Cap the query length to prevent excessive token bloat (giving plenty of headroom)
    request.query = request.query[:1000]
        
    touch_session(request.session_id)

    # Write User Message to DB
    try:
        supabase.table("messages").insert({
            "conversation_id": conversation_id,
            "role": "user",
            "content": request.query,
            "session_id": request.session_id
        }).execute()
    except Exception as e:
        logger.error(f"Failed to write user message: {e}")
    
    # Stage 1: Keyword Classification
    stage_1 = stage_1_keyword_pass(request.query)
    
    qdrant_store = QdrantStore.get_instance()
    
    run_retrieval = stage_1 in ["micro", "hybrid"]
    raw_results = []
    
    session_dir = get_session_dir(request.session_id)
    metadata_path = os.path.join(session_dir, "metadata.json")
    
    metadata = {}
    if os.path.exists(metadata_path):
        try:
            with open(metadata_path, 'r') as f:
                metadata = json.load(f)
        except Exception:
            pass
            
    overviews_meta = metadata.get("overviews", {})
    cached_caption = metadata.get("cached_caption")
    scene_width = metadata.get("scene_width", 0)
    scene_height = metadata.get("scene_height", 0)
    
    stage_1 = stage_1_keyword_pass(request.query)
    
    missing_macro = not overviews_meta
    
    run_retrieval = True
    if stage_1 == "macro" and not missing_macro:
        run_retrieval = False
        
    effective_limit = min(request.limit, vllm_settings.MAX_PATCHES_PER_PROMPT)
        
    results = []
    scores = []
    if run_retrieval:
        encoder = SARCLIPEncoder.get()
        try:
            query_vector = encoder.encode_text(request.query)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to encode text query: {e}")
            
        store = QdrantStore.get_instance()
        try:
            raw_results = store.search_vectors(
                collection_name=settings.QDRANT_COLLECTION,
                query_vector=query_vector,
                limit=max(10, effective_limit),
                session_id=request.session_id
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to query Qdrant: {e}")
            
        scores = [r.get("score", 0.0) for r in raw_results]
        results = raw_results[:effective_limit]
        
    if not run_retrieval:
        final_mode = "macro"
        stage_2 = None
    else:
        stage_2 = stage_2_score_spread(scores)
        final_mode = resolve_routing(stage_1, stage_2)
        
    if final_mode in ["macro", "hybrid"] and missing_macro:
        final_mode = "micro"
        
    macro_cached = False
    if final_mode == "macro":
        if check_cached_macro(request.query) and cached_caption:
            macro_cached = True
            final_mode = "macro_cached"
        else:
            final_mode = "macro_live"
            
    logger.info(f"RAG Query: '{request.query}' | Stage1: {stage_1} | Stage2: {stage_2} | Final Mode: {final_mode} | Scores: {scores}")

    raw_patches_meta = {}
    coords = []
    
    if final_mode in ["micro", "hybrid"]:
        for r in results:
            payload = r.get("payload") or {}
            row = payload.get("row_start")
            col = payload.get("col_start")
            if row is not None and col is not None:
                coords.append((row, col))
                raw_patches_meta[(row, col)] = {
                    "id": r.get("id"),
                    "score": r.get("score"),
                    "row": row,
                    "col": col,
                    "scene": payload.get("scene_name", "Unknown"),
                    "spatial_label": get_spatial_label(row, col, PATCH_SIZE, scene_width, scene_height)
                }

    extracted_patches = []
    if coords:
        try:
            extracted_patches = get_base64_patches(request.session_id, coords)
        except Exception as e:
            logger.error(f"Failed to extract base64 patches: {e}")
            
    patches_meta = []
    base64_patches = []
    for ep in extracted_patches:
        r, c = ep["row"], ep["col"]
        if (r, c) in raw_patches_meta:
            meta = raw_patches_meta[(r, c)]
            patches_meta.append(meta)
            base64_patches.append({
                "spatial_label": meta["spatial_label"],
                "base64": ep["base64"]
            })
            
    patches_meta = patches_meta[:vllm_settings.MAX_PATCHES_PER_PROMPT]
    base64_patches = base64_patches[:vllm_settings.MAX_PATCHES_PER_PROMPT]
        
    base64_overviews = []
    if final_mode in ["macro_live", "hybrid"]:
        needed_quadrants = set()
        if final_mode == "hybrid":
            for ep in base64_patches:
                lbl = ep["spatial_label"].lower()
                if "northwest" in lbl: needed_quadrants.add("NW")
                if "northeast" in lbl: needed_quadrants.add("NE")
                if "southwest" in lbl: needed_quadrants.add("SW")
                if "southeast" in lbl: needed_quadrants.add("SE")

        for filename, info in overviews_meta.items():
            label = info.get("label", "single")
            
            # If hybrid and grid-split, only include relevant quadrants
            if final_mode == "hybrid" and label != "single":
                if label not in needed_quadrants:
                    continue
                    
            overview_path = os.path.join(session_dir, filename)
            if os.path.exists(overview_path):
                try:
                    with open(overview_path, "rb") as img_file:
                        b64_str = base64.b64encode(img_file.read()).decode('utf-8')
                        base64_overviews.append({
                            "label": label,
                            "base64": b64_str
                        })
                except Exception as e:
                    logger.error(f"Failed to read overview image {filename}: {e}")
                    
        base64_overviews = base64_overviews[:vllm_settings.MAX_OVERVIEWS_PER_PROMPT]
                    
    async def event_generator():
        has_patches = bool(base64_patches)
        has_overviews = bool(base64_overviews)
        
        if final_mode == "micro" and not has_patches:
            yield json.dumps({"type": "error", "data": "Sources unavailable."}) + "\n"
            return
            
        yield json.dumps({"type": "conversation_id", "data": conversation_id}) + "\n"
        yield json.dumps({"type": "sources", "mode": final_mode, "data": patches_meta}) + "\n"

        full_assistant_response = ""
        status = "complete"

        if macro_cached:
            yield json.dumps({"type": "text", "data": cached_caption}) + "\n"
            full_assistant_response = cached_caption
            try:
                supabase.table("messages").insert({
                    "conversation_id": conversation_id,
                    "role": "assistant",
                    "content": full_assistant_response,
                    "mode": "macro_cached",
                    "sources": patches_meta,
                    "session_id": request.session_id,
                    "status": status
                }).execute()
            except Exception as e:
                logger.error(f"Failed to write assistant message: {e}")
            return

        system_prompt = (
            "You are an expert Synthetic Aperture Radar (SAR) image analyst. "
            "Analyze the provided SAR image(s) and provide a detailed, conversational response answering the user's query. "
            "Each image provided below is labeled with its specific location in the overall scene. Use these spatial coordinates to understand the physical relationship between objects across different patches. "
            "If you detect relevant objects, provide their bounding box coordinates, but you MUST also explain what you observe in clear, natural language."
        )
        
        messages = [{"role": "system", "content": system_prompt}]
        
        # Add conversation history
        for msg in request.history:
            role = msg.get("role", "user")
            text_content = msg.get("content", "")
            messages.append({"role": role, "content": text_content})
        
        content = [{"type": "text", "text": f"User Query: {request.query}"}]
        
        image_idx = 1
        for ov in base64_overviews:
            label = ov["label"]
            if label == "single":
                ov_text = "Overview Map: full scene overview"
            else:
                ov_text = f"Overview Map: scene overview, {label} quadrant"
            content.append({"type": "text", "text": f"{ov_text}\n<|image_{image_idx}|>"})
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{ov['base64']}"}})
            image_idx += 1
            
        for p in base64_patches:
            if final_mode == "hybrid":
                p_text = f"Zoomed-in detail of the {p['spatial_label']}"
            else:
                p_text = f"Patch Location: {p['spatial_label']}"
                
            content.append({"type": "text", "text": f"{p_text}\n<|image_{image_idx}|>"})
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{p['base64']}"}})
            image_idx += 1
            
        messages.append({"role": "user", "content": content})

        try:
            stream = await client.chat.completions.create(
                model=settings.SARCHAT_MODEL_ID,
                messages=messages,
                stream=True,
                max_tokens=vllm_settings.OUTPUT_MAX_TOKENS,
                temperature=0.2
            )
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    text_chunk = chunk.choices[0].delta.content
                    full_assistant_response += text_chunk
                    yield json.dumps({"type": "text", "data": text_chunk}) + "\n"
        except Exception as e:
            logger.error(f"vLLM completion error: {e}")
            # ``interrupted`` was a pre-M1 free-form value. Persist the
            # normalized lifecycle value required by message_status instead.
            status = "failed"
            yield json.dumps({"type": "error", "data": str(e)}) + "\n"
        finally:
            try:
                supabase.table("messages").insert({
                    "conversation_id": conversation_id,
                    "role": "assistant",
                    "content": full_assistant_response,
                    "mode": final_mode,
                    "sources": patches_meta,
                    "session_id": request.session_id,
                    "status": status
                }).execute()
            except Exception as e:
                logger.error(f"Failed to write assistant message: {e}")

    return StreamingResponse(event_generator(), media_type="application/x-ndjson")


