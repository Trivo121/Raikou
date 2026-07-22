# M2 direct upload operations

M2 keeps all selected file bytes off the FastAPI request path. The browser only
receives short-lived, single-part S3-compatible URLs after FastAPI verifies the
Supabase user, project, scene, filename shape, and requested size. FastAPI
never returns AWS credentials to the browser.

## Supported input shape

- One Sentinel-1 GRD `.zip` archive, optionally with one `.json` sidecar; or
- One or two `.tif` / `.tiff` files, optionally with one `.json` sidecar.

The API repeats the browser's validation. It rejects unsafe names and paths,
wrong MIME declarations, oversized files, archive traversal, encrypted ZIPs,
unsafe links, excessive entry counts, compression bombs, and archives without
a Sentinel SAFE manifest.

## Upload API flow

1. `POST /api/v1/uploads/initiate` carries a browser-generated
   `client_request_id`, creates a scoped, expiring plan, and assigns one
   server-generated object key per file. Replaying the exact request ID returns
   the same active plan; reusing it for different files or scope is rejected.
   The plan row, all file rows, and the scene's `uploading` transition commit
   through one PostgreSQL transaction, so a retry never observes a partial
   plan or races destructive cleanup.
2. The browser hashes a chunk, calls `POST /api/v1/uploads/{plan_id}/parts/sign`,
   and uploads that chunk directly to the returned S3/MinIO URL.
3. `POST /api/v1/uploads/{plan_id}/complete` verifies provider object metadata,
   server-verifies the raw full-file checksum, validates the file shape, and
   atomically writes artifacts, scene state, a queued job, and its PostgreSQL
   outbox entry.
4. The API publishes the minimal job ID to Redis Streams. If Redis is down,
   the outbox remains durable with `retry_scheduled` status for a later
   retry. Job-status polling safely nudges the outbox with a short lease and
   bounded exponential backoff; M3 will add the always-on dispatcher. No job
   is silently discarded.

`DELETE /api/v1/uploads/{plan_id}` revokes an unfinished plan and aborts its
multipart uploads. Browser cancellation calls it only before completion begins:
once `complete` is sent, a lost response may still represent a committed job,
so the client reconciles through durable plan/job state instead of deleting
potential source artifacts. `GET /api/v1/uploads/initiation/{client_request_id}`
recovers an interrupted setup plan, while `GET /api/v1/uploads/{plan_id}/status`
returns the authoritative plan lifecycle and only the job linked to that exact
plan. A stale completion lease is marked failed, its scene is released, and its
unreferenced objects are cleaned up before another upload may begin. If a
completion request never reached FastAPI, the browser checks that authoritative
state before it offers the user a safe plan-release action.

Deletion is intentionally conservative in M2. A scene or project with an
active plan is rejected immediately, and one with a terminal plan that still
has temporary storage cleanup pending returns `409 upload_cleanup_pending`.
M3 will own a durable artifact/terminal-plan cleanup workflow; until then,
M2 preserves the metadata rather than risk orphaning a private object.

The ZIP validator reads and bounds the central directory before using Python's
ZIP parser. Keep `UPLOAD_MAX_ZIP_CENTRAL_DIRECTORY_BYTES` appropriate for the
allowed entry limit; the default is 32 MiB for at most 20,000 entries.

## Development with MinIO

Set the backend environment values below (the project `.env.example` contains
the same shape):

```dotenv
STORAGE_BACKEND=minio
STORAGE_BUCKET=raikou-dev
STORAGE_ENDPOINT_URL=http://localhost:9000
STORAGE_REGION=us-east-1
STORAGE_FORCE_PATH_STYLE=true
STORAGE_ACCESS_KEY_ID=<minio-access-key>
STORAGE_SECRET_ACCESS_KEY=<minio-secret-key>
STORAGE_MULTIPART_CHECKSUM_MODE=auto
```

Create the bucket before starting FastAPI. `auto` uses native per-part SHA-256
when supported and safely falls back to `server_verified` for an older MinIO
server. The endpoint must be reachable from the browser, not merely from the
API container.

The outbox retry controls default to a 10-second base delay capped at five
minutes:

```dotenv
JOB_DISPATCH_LEASE_SECONDS=60
JOB_DISPATCH_RETRY_BASE_SECONDS=10
JOB_DISPATCH_RETRY_MAX_SECONDS=300
```

## AWS S3 production requirements

Set `STORAGE_BACKEND=s3`, `STORAGE_BUCKET=<private-bucket>`, and leave
`STORAGE_ENDPOINT_URL` blank. Prefer an EC2 instance profile with only the
required object-prefix permissions rather than long-lived access keys. The
current EC2 instance has no IAM role attached, so attach a narrowly scoped
instance profile before attempting a production upload deployment.

Configure bucket CORS for the real React origin. It must allow `PUT`, the
`x-amz-checksum-sha256` request header, and expose `ETag`; do not use `*` as a
production origin.

```json
[
  {
    "AllowedOrigins": ["https://your-react-origin.example"],
    "AllowedMethods": ["PUT"],
    "AllowedHeaders": ["content-type", "x-amz-checksum-sha256"],
    "ExposeHeaders": ["ETag", "x-amz-checksum-sha256"],
    "MaxAgeSeconds": 300
  }
]
```

Enable a bucket lifecycle rule that aborts incomplete multipart uploads after
one day as a backstop for browser crashes and network loss. Keep the bucket
private and never add an object-read wildcard merely to support these uploads.

`/readyz` checks the M1/M2 Supabase schema probe (including the upload
idempotency migration), Redis when configured, Qdrant, and an authenticated
bucket `HeadBucket` request. It should return ready before exposing the upload
flow to users.
