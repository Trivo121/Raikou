# M4 project and scene workspace

M4 exposes the durable M1-M3 records through one protected route:

```text
/projects/:projectId
```

The workspace has **Overview**, **Scenes**, **Evidence Search**, and **Ask**
panels. Overview, Scenes, and Evidence Search use only FastAPI APIs backed by
PostgreSQL and private object storage. Ask remains an explicit M5 placeholder;
it must not call the legacy session API because that API has no project-scoped
ownership contract or conversation-history parity.

## API surface

| Route | Use |
| --- | --- |
| `GET /api/v1/projects/{project_id}/workspace` | Project lifecycle counts and batched scene summaries. |
| `GET /api/v1/scenes/{scene_id}/workspace` | Selected scene, jobs, artifacts, overview reference, patches, and evidence status. |
| `GET /api/v1/scenes/{scene_id}/evidence-record` | Explicit metadata, land/water, model-observation, and validated-detector evidence. |
| `GET /api/v1/jobs/{job_id}/events` | Durable, browser-safe job history. |
| `POST /api/v1/scenes/{scene_id}/reprocess` | New M3 job for an owned failed/cancelled scene with retained source data. |
| `POST /api/v1/artifacts/{artifact_id}/preview` | Short-lived private image preview grant. |
| `GET /api/v1/patches/{patch_id}` | Owned patch bounds, provenance, and preview reference. |

Every route resolves ownership before reading records or issuing a preview.
Normal artifact responses never contain a bucket, object key, or public URL.

## Preview security

- `ARTIFACT_PREVIEW_TTL_SECONDS` defaults to 90 seconds and is bounded to
  30–300 seconds.
- Only available `overview`, `thumbnail`, and `patch_preview` image artifacts
  can receive a preview grant.
- The browser requests a grant only when opening a preview. It keeps the URL
  only in component state, uses `referrerPolicy=no-referrer`, and clears it on
  close or expiry.
- The response carries `Cache-Control: no-store`.
- The S3 bucket remains private. Its CORS configuration must allow `GET` and
  `HEAD` from the exact local/production React origins in addition to M2's
  multipart upload methods.

## Evidence labelling

M4 never presents a generated caption as a detection:

- **Metadata** is source/raster-derived technical context.
- **Land/water estimate** is a low-backscatter heuristic with `review_required`.
- **Model observation** is SARChat/VLM output and is marked non-verified.
- **Validated detector evidence** contains only facts from the approved
  provenance-bearing detector sidecar.

## Migration and readiness

Apply `20260721002000_m4_workspace_retry.sql` after M3. It:

1. replaces M1's legacy active-job index with one covering `queued`,
   `validating`, `processing`, and retained `running` jobs;
2. adds `m4_request_scene_reprocess`; and
3. adds the `m4_workspace_schema_ready()` readiness probe.

The migration intentionally aborts before replacing the index if duplicate
active process jobs already exist. Resolve that operational inconsistency
before retrying the migration; do not drop the index manually.

## Verification checklist

1. `GET /readyz` reports M1–M4 schema readiness after the migration.
2. User A receives `404` for User B's project, scene, job, artifact preview,
   evidence record, and patch IDs.
3. A `ready` job stops client polling.
4. A failed/cancelled scene with a retained source creates one new queued job;
   an active or deleting scene returns `409`.
5. A fresh browser reload reconstructs the same scene state without mock or
   local-storage product data.
6. An evidence or patch card opens an authorized overview/patch preview and
   no artifact response contains a storage key.
