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
from app.services.processing.patch_pipeline import preprocess_patch, PATCH_SIZE, get_base64_patches
from app.services.session_cache import touch_session
from openai import AsyncOpenAI

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

    coords = []
    patches_meta = []
    for r in results:
        payload = r.get("payload") or {}
        row = payload.get("row_start")
        col = payload.get("col_start")
        if row is not None and col is not None:
            coords.append((row, col))
            patches_meta.append({
                "id": r.get("id"),
                "score": r.get("score"),
                "row": row,
                "col": col,
                "scene": payload.get("scene_name", "Unknown")
            })

    touch_session(request.session_id)
    try:
        base64_images = get_base64_patches(request.session_id, coords)
    except Exception as e:
        logger.error(f"Failed to extract base64 patches: {e}")
        base64_images = []

    async def event_generator():
        if coords and not base64_images:
            yield json.dumps({"type": "error", "data": "Original image data not found. It may have been cleaned up."}) + "\n"
            return
            
        yield json.dumps({"type": "sources", "data": patches_meta}) + "\n"

        prompt_text = (
            "You are an expert Synthetic Aperture Radar (SAR) image analyst. "
            "Analyze the provided SAR image patches and provide a detailed, conversational response answering the following query. "
            "If you detect relevant objects, provide their bounding box coordinates, but you MUST also explain what you observe in clear, natural language.\n\n"
            f"User Query: {request.query}"
        )
        
        content = [
            {"type": "text", "text": prompt_text}
        ]
        for b64 in base64_images:
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"}
            })
            
        messages = [{"role": "user", "content": content}]

        try:
            stream = await client.chat.completions.create(
                model="/models/SARChat-Phi-3.5-vision-instruct",
                messages=messages,
                stream=True,
                max_tokens=512,
                temperature=0.2
            )
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield json.dumps({"type": "text", "data": chunk.choices[0].delta.content}) + "\n"
        except Exception as e:
            logger.error(f"vLLM completion error: {e}")
            yield json.dumps({"type": "error", "data": str(e)}) + "\n"

    return StreamingResponse(event_generator(), media_type="application/x-ndjson")
