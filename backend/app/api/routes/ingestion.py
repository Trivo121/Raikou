from fastapi import APIRouter, UploadFile, File, HTTPException
from app.services.ingestion.file_ingestion import process_grd_file

router = APIRouter()

@router.post("/upload")
async def upload_sar_file(file: UploadFile = File(...)):
    try:
        session_data = await process_grd_file(file)
        return {
            "filename": file.filename,
            "status": "Uploaded successfully",
            "session_data": session_data
        }
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=str(e))
