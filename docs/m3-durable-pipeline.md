# M3 durable SAR pipeline

M3 treats PostgreSQL as the source of truth for every processing task. Redis
Streams wake workers only; stream redelivery is safe because a worker must
lease and settle a PostgreSQL task before it acknowledges the message.

## Required runtime configuration

- `SUPABASE_DB_URL`: direct PostgreSQL URL for the dispatcher and workers.
- `REDIS_URL`: durable Redis endpoint. Use a dedicated production instance;
  do not configure an eviction policy that can remove Stream keys.
- S3 settings from M2: `STORAGE_BACKEND=s3`, `S3_BUCKET_NAME`, `AWS_REGION`.
- Qdrant URL/credentials and a 768-dimensional `QDRANT_COLLECTION`.
- SARCLIP checkpoint available on GPU workers only.

Do not set static AWS access keys on EC2. Attach an IAM role with private
bucket object read/write/delete and multipart permissions instead.

## Process topology

```text
raikou-api                 HTTP/auth only; no worker lifecycle
raikou-outbox-dispatcher   PostgreSQL task outbox -> Redis Stream
raikou-worker-cpu@1        validation, artifacts, evidence, Qdrant, cleanup
raikou-worker-gpu@0        SARCLIP embedding for CUDA device 0
```

Install and start the services after the application and environment file are
deployed:

```bash
sudo systemctl enable --now raikou-outbox-dispatcher
sudo systemctl enable --now raikou-worker-cpu@1
sudo systemctl enable --now raikou-worker-gpu@0
```

Use exactly one GPU worker instance for each physical GPU. Start with
`M3_GPU_INFERENCE_CONCURRENCY=1`; run `backend/scripts/profile_sarclip_vram.py`
on the target GPU before increasing it.

## Local runtime

Provide Supabase variables in `backend/.env`, then run:

```bash
docker compose up --build
docker compose --profile gpu up --build worker-gpu
```

Compose starts Redis, MinIO, Qdrant, the API, dispatcher, and CPU worker. The
GPU profile requires Docker NVIDIA runtime support. Apply Supabase migrations
before starting the application because `/readyz` verifies M3 schema readiness.

## Operational checks

- `GET /readyz` confirms the M1/M2/M3 schema, object store, Qdrant, and Redis.
- `POST /api/v1/jobs/{job_id}/cancel` records a cancellation request. The
  worker removes vectors and derived artifacts before marking the job cancelled.
- `DELETE /api/v1/scenes/{scene_id}` returns a cleanup job ID. It first cancels
  active processing, then deletes vectors, objects, and database scope last.

The stream names are `raikou:v1:stream:processing:cpu` and
`raikou:v1:stream:processing:gpu` by default. They are intentionally separate
from all cache keys.
