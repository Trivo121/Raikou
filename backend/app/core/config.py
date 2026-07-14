from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    PROJECT_NAME: str = "SAR Pipeline API"
    API_V1_STR: str = "/api/v1"
    
    QDRANT_URL: str = ""
    QDRANT_API_KEY: str = ""
    
    AWS_ACCESS_KEY_ID: str | None = None
    AWS_SECRET_ACCESS_KEY: str | None = None
    AWS_S3_BUCKET: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

class VLLMConfig(BaseSettings):
    MAX_PATCHES_PER_PROMPT: int = 5
    MAX_OVERVIEWS_PER_PROMPT: int = 4
    OUTPUT_MAX_TOKENS: int = 1024
    
    # These will be updated after measurement
    NUM_CROPS: int = 4 
    MAX_MODEL_LEN: int = 4096
    MAX_NUM_SEQS: int = 2
    LIMIT_MM_PER_PROMPT: str = "image=9"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_prefix="VLLM_"
    )

settings = Settings()
vllm_settings = VLLMConfig()
