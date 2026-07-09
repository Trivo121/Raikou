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
        payload = r.payload or {}
        row = payload.get("row_start")
        col = payload.get("col_start")
        if row is not None and col is not None:
            coords.append((row, col))
            patches_meta.append({
                "id": r.id,
                "score": r.score,
                "row": row,
                "col": col,
                "scene": payload.get("scene_name", "Unknown")
            })

    try:
        base64_images = get_base64_patches(request.session_id, coords)
    except Exception as e:
        logger.error(f"Failed to extract base64 patches: {e}")
        base64_images = []

    async def event_generator():
        yield json.dumps({"type": "sources", "data": patches_meta}) + "\n"

        content = [
            {"type": "text", "text": f"Based on these SAR image patches, answer the user query: {request.query}"}
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
