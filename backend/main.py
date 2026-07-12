import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.api.routes import projects, ingestion, processing, search
from app.services.models.sarclip_encoder import SARCLIPEncoder

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json"
)

import asyncio
from app.services.session_cache import start_cleanup_loop

@app.on_event("startup")
async def _startup():
    SARCLIPEncoder.load_singleton()
    asyncio.create_task(start_cleanup_loop(ttl_hours=2))

# Set all CORS enabled origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Adjust in production
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(projects.router, prefix=f"{settings.API_V1_STR}/projects", tags=["projects"])
app.include_router(ingestion.router, prefix=f"{settings.API_V1_STR}/ingestion", tags=["ingestion"])
app.include_router(processing.router, prefix=f"{settings.API_V1_STR}/processing", tags=["processing"])
app.include_router(search.router, prefix=f"{settings.API_V1_STR}/search", tags=["search"])

@app.get("/")
def root():
    return {"message": "SAR Pipeline API is running."}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
