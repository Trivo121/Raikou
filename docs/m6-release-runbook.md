# M6 release runbook

This is the production procedure for the Raikou V1 control plane (M1–M5) and
the M6 operational layer. Keep Supabase and S3 managed; do not run privileged
database or object storage credentials in the browser.

## Preflight

1. Assign a stable DNS name and an Elastic IP (or load balancer). Point the DNS
   record at the gateway before enabling Caddy automatic TLS.
2. Create a least-privilege EC2 role for the private S3 bucket. It needs the
   multipart/object actions used by M2, `s3:ListBucket`, and no public ACL
   permissions. Keep Block Public Access enabled.
3. Set the production environment file to mode `0600`. Required values include
   Supabase URL/service key, the **Supabase session-pooler** DB URL, Redis,
   Qdrant, bucket/region, SARVLM checkpoint, SARChat vLLM path, exact HTTPS
   `CORS_ORIGINS`, and a random `METRICS_TOKEN`.
4. Do not include `localhost`, wildcard origins, AWS keys, or Supabase service
   credentials in React build variables. CORS must name only the deployed
   React origin.

## Deploy order

1. Back up the existing `.env`, gateway config, and application release.
2. Apply Supabase migrations in lexical order from `supabase/migrations/`.
   Verify `supabase_migrations.schema_migrations` contains every version.
3. Fetch model artefacts to the GPU host and verify their SHA-256 checksums.
   SARVLM is the retrieval encoder; SARChat is the generation model.
4. Build the frontend with a same-origin API base, then bring up Compose:

   ```bash
   docker compose --profile gpu --profile release up -d --build
   ```

   `PUBLIC_HOST` must be a real DNS name for Caddy TLS. On the current
   systemd-based host, install the equivalent Nginx configuration and keep
   Uvicorn/vLLM on loopback only until the Compose cutover is scheduled.
5. Start dispatcher, one CPU worker, and exactly one GPU worker per physical
   GPU. Never raise GPU concurrency before VRAM profiling.
6. Confirm `/healthz` and `/readyz`; `/readyz` must be `200` with no issues.
   Scrape `/metrics` only from the private network using `X-Metrics-Token`.

## Acceptance and rollback

Run the canonical, non-sensitive sample scene in a dedicated test project:

1. Upload a supported GeoTIFF, Sentinel-1 SAFE archive, or supported HDF5
   product through the React UI.
2. Confirm an M3 job reaches `ready`, scoped search returns only that project’s
   patches, and cited M5 chat emits grounded evidence.
3. Test cancellation and scene deletion. Verify jobs are cancelled, vectors and
   S3 artefacts are removed, then the durable database scope is deleted last.

If a release fails, stop new gateway traffic, restore the prior application
release and `.env`, restart API/dispatcher/workers, and re-run `/readyz`.
Never roll back applied database migrations destructively; use a forward repair
migration. Preserve failed-job IDs and request IDs for investigation.

## Monitoring and incidents

Alert on non-200 readiness, worker restarts, Redis stream pending/lagged
entries, dispatcher outbox backlog, failed/retried jobs, S3 upload failures,
Qdrant/model errors, and terminated chat streams. Correlate API and worker logs
with `request_id`, project, scene, and job identifiers; do not log tokens,
presigned URLs, prompts, or object contents.

For an unavailable dependency: pause intake if needed, keep PostgreSQL task
records authoritative, repair the dependency, then let Redis stream consumers
reclaim pending entries. For suspected credential exposure: revoke the key or
role session, rotate Supabase service/DB credentials, S3 access, metrics token,
and model-service credentials; update the encrypted deployment secret and
restart affected services.

## Backup and lifecycle

Enable Supabase daily backups/PITR and test a restore at least quarterly. Keep
S3 versioning and a lifecycle rule that aborts stale multipart uploads; retain
production source artefacts according to policy, then expire derived previews
and temporary processing artefacts. Back up Qdrant snapshots before index
schema changes. A backup is not accepted until a restore drill succeeds.
