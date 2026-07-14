import os
import tempfile
import io
import json
import logging
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
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
from app.services.session_cache import touch_session
import base64
from openai import AsyncOpenAI
from app.core.config import vllm_settings

logger = logging.getLogger(__name__)
client = AsyncOpenAI(base_url="http://localhost:8001/v1", api_key="sk-no-key")

router = APIRouter()

class SearchQuery(BaseModel):
    query: str
    limit: int = 10

@router.post("/")
async def search_patches(request: SearchQuery):
    if not request.query:
        raise HTTPException(status_code=400, detail="Query cannot be empty")
        
    encoder = SARCLIPEncoder.get()
    try:
        query_vector = encoder.encode_text(request.query)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to encode text query: {e}")
        
    store = QdrantStore.get_instance()
    try:
        results = store.search_vectors(
            collection_name="sar_patches",
            query_vector=query_vector,
            limit=request.limit
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to query Qdrant: {e}")
        
    return {"results": results}

@router.get("/patch/{session_id}")
async def get_patch_image(session_id: str, row: int, col: int):
    temp_dir = tempfile.gettempdir()
    session_dir = os.path.join(temp_dir, f"raikou_session_{session_id}")
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

@router.post("/rag/chat")
async def rag_chat(request: RagQuery):
    if not request.query:
        raise HTTPException(status_code=400, detail="Query cannot be empty")
        
    touch_session(request.session_id)
    
    temp_dir = tempfile.gettempdir()
    session_dir = os.path.join(temp_dir, f"raikou_session_{request.session_id}")
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
                collection_name="sar_patches",
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
                    
    async def event_generator():
        has_patches = bool(base64_patches)
        has_overviews = bool(base64_overviews)
        
        if final_mode == "micro" and not has_patches:
            yield json.dumps({"type": "error", "data": "Sources unavailable."}) + "\n"
            return
            
        yield json.dumps({"type": "sources", "mode": final_mode, "data": patches_meta}) + "\n"

        if macro_cached:
            yield json.dumps({"type": "text", "data": cached_caption}) + "\n"
            return

        prompt_text = (
            "You are an expert Synthetic Aperture Radar (SAR) image analyst. "
            "Analyze the provided SAR image(s) and provide a detailed, conversational response answering the following query. "
            "Each image provided below is labeled with its specific location in the overall scene. Use these spatial coordinates to understand the physical relationship between objects across different patches. "
            "If you detect relevant objects, provide their bounding box coordinates, but you MUST also explain what you observe in clear, natural language.\n\n"
            f"User Query: {request.query}"
        )
        
        content = [{"type": "text", "text": prompt_text}]
        
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
            
        messages = [{"role": "user", "content": content}]

        try:
            stream = await client.chat.completions.create(
                model="/models/SARChat-Phi-3.5-vision-instruct",
                messages=messages,
                stream=True,
                max_tokens=vllm_settings.OUTPUT_MAX_TOKENS,
                temperature=0.2
            )
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield json.dumps({"type": "text", "data": chunk.choices[0].delta.content}) + "\n"
        except Exception as e:
            logger.error(f"vLLM completion error: {e}")
            yield json.dumps({"type": "error", "data": str(e)}) + "\n"

    return StreamingResponse(event_generator(), media_type="application/x-ndjson")
