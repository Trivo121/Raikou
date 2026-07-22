FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Rasterio requires GDAL's shared libraries at runtime. Production GPU images
# may extend this image with CUDA/PyTorch matching the target host driver.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gdal-bin libgdal-dev libgl1 libglib2.0-0 build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . ./
