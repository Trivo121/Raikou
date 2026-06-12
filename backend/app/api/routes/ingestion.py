from fastapi import APIRouter, UploadFile, File

router = APIRouter()

@router.post("/upload")
async def upload_sar_file(file: UploadFile = File(...)):
    return {"filename": file.filename, "status": "Uploaded successfully"}
