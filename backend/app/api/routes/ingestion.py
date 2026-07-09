from fastapi import APIRouter, UploadFile, File, HTTPException
from typing import List
from app.services.ingestion.file_ingestion import process_uploaded_files

router = APIRouter()

@router.post("/upload")
async def upload_sar_file(files: List[UploadFile] = File(...)):
    try:
        session_data = await process_uploaded_files(files)
        return {
            "filenames": [f.filename for f in files],
            "status": "Uploaded successfully",
            "session_data": session_data
        }
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=str(e))
