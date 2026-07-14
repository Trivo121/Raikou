import os
import tempfile
import json
from fastapi import APIRouter, HTTPException, BackgroundTasks
import rasterio

from app.services.processing.patch_pipeline import extract_and_preprocess_patches, estimate_patch_count, generate_overview
from app.services.models.sarclip_encoder import encode_patch_stream, EncodedPatch
from app.services.storage.qdrant import QdrantStore
from app.services.session_cache import touch_session

import logging
import requests
import base64

router = APIRouter()
logger = logging.getLogger(__name__)

def generate_cached_caption_sync(session_id: str):
    """
    Generates a generic caption for the scene overview(s) using a synchronous HTTP call to vLLM.
    Fails silently so it doesn't block ingestion.
    """
    temp_dir = tempfile.gettempdir()
    session_dir = os.path.join(temp_dir, f"raikou_session_{session_id}")
    metadata_path = os.path.join(session_dir, "metadata.json")
    
    try:
        if not os.path.exists(metadata_path):
            return
            
        with open(metadata_path, 'r') as f:
            metadata = json.load(f)
            
        overviews = metadata.get("overviews", {})
        if not overviews:
            return
            
        b64_images = []
        for filename in overviews.keys():
            img_path = os.path.join(session_dir, filename)
            if os.path.exists(img_path):
                with open(img_path, "rb") as img_file:
                    b64_str = base64.b64encode(img_file.read()).decode('utf-8')
                    b64_images.append(b64_str)
                    
        if not b64_images:
            return
            
        prompt_text = "Describe the broad structure, geographic features, and overall context of this SAR scene."
        content = [{"type": "text", "text": prompt_text}]
        for b64 in b64_images:
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"}
            })
            
        payload = {
            "model": "/models/SARChat-Phi-3.5-vision-instruct",
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 512,
            "temperature": 0.2
        }
        
        resp = requests.post("http://localhost:8001/v1/chat/completions", json=payload, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        
        caption = result.get("choices", [])[0].get("message", {}).get("content", "")
        if caption:
            metadata["cached_caption"] = caption
            with open(metadata_path, 'w') as f:
                json.dump(metadata, f, indent=2)
            
    except Exception as e:
        logger.error(f"Failed to generate cached caption for session {session_id}: {e}")

def process_session_background(session_id: str, vrt_path: str, metadata: dict, width: int, height: int):
    # Phase 1: Generate scene overviews and optional caption BEFORE patch encoding
    generate_overview(vrt_path, session_id)
    generate_cached_caption_sync(session_id)
    
    qdrant_store = QdrantStore.get_instance()
    collection_name = "sar_patches"
    qdrant_store.initialize_collection(collection_name)
    
    patch_iterator = extract_and_preprocess_patches(vrt_path, session_id, metadata)
    
    # Use encode_patch_stream to handle batching and model inference
    encoding_stream = encode_patch_stream(
        patches=patch_iterator,
        session_id=session_id,
        scene_width=width,
        scene_height=height,
        batch_size=64
    )
    
    payloads = []
    
    for event in encoding_stream:
        if isinstance(event, EncodedPatch):
            payload = {
                "session_id": session_id,
                "row_start": event.row_start,
                "col_start": event.col_start,
                "sensor": event.scene_metadata.get("sensor", "Unknown"),
                "acquisition_date": event.scene_metadata.get("acquisition_date", "Unknown"),
                "polarization": event.scene_metadata.get("polarization", [])
            }
            payloads.append({
                "id": event.patch_id,
                "vector": event.embedding,
                "payload": payload
            })
            
            # Upsert in batches of ~64
            if len(payloads) >= 64:
                qdrant_store.upsert_vectors(collection_name, payloads)
                payloads = []
                touch_session(session_id)
                
    if payloads:
        qdrant_store.upsert_vectors(collection_name, payloads)

    # Signal completion by removing status.json, but keep the TIFFs/VRT for querying
    status_path = os.path.join(os.path.dirname(vrt_path), "status.json")
    if os.path.exists(status_path):
        os.remove(status_path)

@router.post("/{session_id}")
async def start_processing(session_id: str, background_tasks: BackgroundTasks):
    temp_dir = tempfile.gettempdir()
    session_dir = os.path.join(temp_dir, f"raikou_session_{session_id}")
    vrt_path = os.path.join(session_dir, "stacked.vrt")
    
    if not os.path.exists(vrt_path):
        raise HTTPException(status_code=404, detail="Session or VRT file not found.")
        
    with rasterio.open(vrt_path) as src:
        width, height = src.width, src.height
        
    plan = estimate_patch_count(width, height)
    
    metadata_path = os.path.join(session_dir, "metadata.json")
    metadata = {}
    if os.path.exists(metadata_path):
        with open(metadata_path, 'r') as f:
            metadata = json.load(f)
            
    metadata["scene_width"] = width
    metadata["scene_height"] = height
    with open(metadata_path, 'w') as f:
        json.dump(metadata, f, indent=2)
            
    status_path = os.path.join(session_dir, "status.json")
    with open(status_path, "w") as f:
        json.dump({"estimated_patches": plan.estimated_total_patches}, f)
            
    background_tasks.add_task(process_session_background, session_id, vrt_path, metadata, width, height)
    
    return {
        "message": "Processing started in background",
        "estimated_patches": plan.estimated_total_patches
    }

@router.get("/status/{session_id}")
async def get_processing_status(session_id: str):
    touch_session(session_id)
    qdrant_store = QdrantStore.get_instance()
    count = qdrant_store.count_vectors_by_session("sar_patches", session_id)
    
    temp_dir = tempfile.gettempdir()
    session_dir = os.path.join(temp_dir, f"raikou_session_{session_id}")
    
    estimated = count
    status = "completed"
    
    status_path = os.path.join(session_dir, "status.json")
    if os.path.exists(status_path):
        status = "processing"
        try:
            with open(status_path, "r") as f:
                data = json.load(f)
                estimated = data.get("estimated_patches", count)
        except Exception:
            pass
            
    return {
        "session_id": session_id,
        "encoded_patches": count,
        "estimated_patches": estimated,
        "status": status
    }
