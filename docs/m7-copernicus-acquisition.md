# M7 Copernicus (CDSE) acquisition

M7 adds a second way to get a scene source into Raikou. The first is unchanged:
create a project, create a scene, upload a Sentinel-1 ZIP you downloaded
yourself. The second names the scene, then asks the server to fetch the product
from the Copernicus Data Space Ecosystem directly.

Two things change for the user. The ~1 GB manual round trip disappears, and the
wrong-product failure mode becomes structurally impossible for anything fetched
this way.

## Why the product type is locked

The processing pipeline reads a specific product layout, and it fails quietly
when it does not get one:

- `stages.py` orders measurement bands with `0 if "vv" in name.lower() else 1`,
  so it needs exactly one VV and one VH band.
- `scattering_map.py` hardcodes `ground_sampling_m = block * 10`, which is
  GRDH's 10 m spacing, and never verifies it. A GRDM product is a silent 4×
  error.
- The scattering block, mechanism map, and land cover each need both
  polarisations plus the calibration **and** thermal-noise LUTs.

A GRDM, single-pol, or SLC-adjacent product currently processes to `ready` with
those blocks missing and **no error at all**. So the catalogue query locks
`productType = IW_GRDH_1S` and `polarisationChannels = VV&VH` server-side, and
this is a pipeline requirement rather than a user preference. It is enforced in
four places:

1. The OData filter, which cannot return anything else.
2. A result-side check in `search_products`, in case the provider changes.
3. Accept-time re-verification in `POST /acquisitions`, which re-reads the
   product from CDSE and trusts nothing the browser sent.
4. `scene_acquisitions` CHECK constraints, which survive a code change, plus
   `assert_iw_grdh_dual_pol_layout` in the worker, which reads the archive.

## The AOI is a filter, not a crop

Sentinel-1 GRD ships as whole ~250×170 km frames. CDSE has no API that returns
"just my AOI" as a SAFE product, and cropping would destroy the SAFE layout the
pipeline reads. The drawn box selects *which scene*, and the scene covers the
box. `GET /acquisitions/providers` reports `aoi_is_crop: false` so the UI can
say this plainly rather than implying a crop.

## Configuration

Create the OAuth client yourself in the CDSE dashboard under
**User settings → OAuth clients**, then set the id and secret in
`backend/.env`. The integration uses `grant_type=client_credentials` only — no
account password is ever handled by the application.

```bash
COPERNICUS_ENABLED=true
COPERNICUS_CLIENT_ID=...
COPERNICUS_CLIENT_SECRET=...
```

Everything else has a working default; see the `COPERNICUS_*` block in
`.env.example`. No `docker-compose.yml` change is needed: `api`, `dispatcher`,
`worker-cpu`, and `worker-gpu` all already load `env_file: ./backend/.env`. No
frontend environment change is needed either — availability comes from the API,
and no credential or upstream URL ever reaches the browser.

**Quota.** Full-product OData downloads draw on the CDSE *data* quota — 12 TB
per month rolling, 4 concurrent connections, 20 MB/s each. At ~1 GB per scene
that is thousands of scenes a month. This is **not** the Sentinel Hub
processing-unit quota shown on the dashboard, which this feature does not use.

### Deliberately not a readiness gate

`startup_issues()` does not know about Copernicus, and `/readyz` neither probes
CDSE nor gates on `m7_acquisition_schema_ready()`.

This diverges from the M2–M5 precedent, where each milestone's schema probe is
a hard readiness gate. For those the schema *was* the product, so failing
closed was right. For an optional add-on, a missing credential or a forgotten
migration must degrade this one feature, not refuse to boot the API. Operators
get the probe through `GET /acquisitions/providers`, which reports
`{"enabled": false, "reason": "not_configured" | "schema_not_applied" | "disabled"}`,
and the UI hides the option. Flag this inconsistency in review if you disagree.

## API

| Route | Purpose |
|---|---|
| `GET /api/v1/acquisitions/providers` | Availability and limits. No upstream call. |
| `POST /api/v1/acquisitions/search` | Server-side catalogue proxy. |
| `POST /api/v1/acquisitions` | Re-verifies the product, then starts the fetch. |
| `GET /api/v1/acquisitions/{id}` | Owner-scoped read. |

Search takes a **bounding box, not a free polygon**. That is the endpoint's key
security property: four validated floats are formatted with `f"{v:.6f}"` into
the OData filter, so no user-supplied string ever enters the filter expression,
and the vertex count is four by construction. Accepting WKT or GeoJSON would
mean either injecting a string into an OData `geography''` literal or writing
this validator anyway.

Requests are rate-limited by `ACQUISITION_RATE_LIMIT_PER_MINUTE` (default 20),
separately from the general control-plane limit, because each one can trigger
an upstream call or a gigabyte of server-side transfer.

### One encoding bug to never reintroduce

The filter is passed through httpx `params=`, never a hand-built query string.
The literal `&` in `VV&VH` **must** be percent-encoded as `%26`; otherwise the
filter silently truncates at that character and every polarisation comes back,
including the single-pol products the pipeline cannot read. This is the classic
CDSE integration bug and
`tests/test_copernicus_client.py::test_search_percent_encodes_the_polarisation_ampersand`
exists solely to keep it fixed.

## The `fetch_source` worker stage

`fetch_source` runs before `validate_upload` and is the only new stage. It
leaves behind exactly the `source_archive` artifact that `validate_upload`
head-checks next, so **every stage from `validate_upload` onward is untouched**
and cannot tell how the bytes arrived.

```text
fetch_source -> validate_upload -> extract_metadata -> build_vrt -> ...
```

Sequence: load the acquisition → re-check the product name → `head_object` the
deterministic key → mark downloading → stream with a lease heartbeat → size
check → shared archive validation → SAFE layout assertion → upload to S3 →
upsert the artifact → point the scene at it → mark downloaded.

### Idempotency uses both mechanisms

**A deterministic, acquisition-scoped key** covers retries and reprocesses:

```text
acquisitions/{owner_id}/{project_id}/{scene_id}/{acquisition_id}/{product_name}.zip
```

It is deliberately *not* built with `_artifact_key`, which keys by
`processing_job_id`: a reprocess would then mint a new key and orphan the
gigabyte already in the bucket. At stage start the worker calls `head_object`
on this key and, if the size matches, skips the network entirely — a retry
after an S3 blip costs zero provider quota.

**Per-attempt HTTP Range resume** covers drops *within* one attempt. It cannot
span attempts, because the runner wipes the scratch directory after every
settled attempt; the task retry budget covers that case. If the server answers
a Range request with `200` rather than `206`, the worker restarts from zero —
appending would silently corrupt the archive.

### Surviving the 300 s lease

`M3_TASK_LEASE_SECONDS` is 300 and nothing else heartbeats. At 20 MB/s a 1 GB
product takes ~50 s, but throttled to 1 MB/s it takes ~1000 s and another
worker would steal the task mid-download. A background thread renews the lease
every `COPERNICUS_LEASE_HEARTBEAT_SECONDS` (60); if the renewal reports the
lease was taken, the download aborts immediately rather than finishing work
`complete_task` would discard.

Another worker can still reclaim the *Redis stream entry* at 300 s, call
`claim_task`, get `None`, and `xack` it away. That is harmless and intentional:
on success the next stage enqueues a fresh dispatch, and on failure
`retry_or_fail_task` resets the dispatch to `retry_scheduled`.

### Progress without a new endpoint

`processing_jobs.progress` only moves at stage boundaries, so a naive build
shows 0 % for the whole download. The same heartbeat thread writes a
`processing_job_events` row about once a minute
(`event_type='fetch_progress'`, capped by `COPERNICUS_MAX_PROGRESS_EVENTS` so a
throttled download cannot flood the table). The workspace already polls
`GET /jobs/{id}/events` every 5 s and already renders `event.message`, so this
gives live progress with no new endpoint and no frontend change.

### Failure codes

Permanent (shown to the user, no retry): `COPERNICUS_AUTH_FAILED`,
`COPERNICUS_PRODUCT_NOT_FOUND`, `COPERNICUS_PRODUCT_OFFLINE`,
`COPERNICUS_REDIRECT_REJECTED`, `SOURCE_ARCHIVE_INVALID`,
`SOURCE_PRODUCT_UNSUPPORTED`, `ACQUISITION_MISSING`.

Retryable: provider 5xx/429/timeouts, connection resets after the resume budget
is spent, size mismatch, S3 failures, and a lost lease.

On a permanent failure — and on a retryable one that exhausts the attempt
budget — the acquisition row is marked `failed`. That matters: leaving it
`queued`/`downloading` would make both the RPC guard and the
one-open-per-scene unique index refuse every future fetch for that scene.

## Token handling

The bearer token lives in a process-local cache with a `threading.Lock` and a
refresh margin. It is **never** written to Redis: a bearer token in a shared
cache is a credential at rest for no benefit, and re-minting is cheap.

Redirects are followed **manually** with `follow_redirects=False`. httpx
forwards `Authorization` across hosts when it follows redirects itself and
offers no hook to prevent it, which is exactly the leak to avoid. The token is
re-attached only when the target host passes an exact-or-dotted-suffix
allowlist (`COPERNICUS_ALLOWED_REDIRECT_HOSTS`) and is stripped otherwise. The
suffix must be dotted: a plain `endswith` check would accept
`dataspace.copernicus.eu.attacker.com`.

## Frontend

`npm i maplibre-gl` (v6, which ships **named ESM exports only** — there is no
default export, and `Map` must be aliased because it shadows the global).

No React wrapper. `react-map-gl` would add a dependency and a version coupling
for what is one `useEffect` driving an imperative API, so `AoiMapPicker.jsx`
owns the map instance directly and nothing else. Drawing is hand-rolled from
`mousedown`/`mousemove`/`mouseup` with `dragPan` disabled mid-draw; a draw
plugin would need a compat shim and is far heavier than the ~50 lines here.

`CopernicusSearchPanel` is **lazy-loaded**, which is verifiable in the build
output: maplibre lands entirely in the split chunk (~250 kB gzipped) and the
main bundle contains zero maplibre bytes. Only a user who picks the Copernicus
path pays for it.

Basemap is CARTO dark-matter (`basemaps.cartocdn.com`), free and token-free,
matching the `bg-[#09090b]` palette without a filter hack. Attribution
(`© OpenStreetMap contributors © CARTO`) is required and is rendered through an
`AttributionControl`.

### The coexistence property

`SceneUploadPanel` and `CopernicusSearchPanel` are **never mounted at the same
time** and share no storage. The branch is one ternary on `needsSource`, which
is `!isReady && !activeJob` — so both panels disappear the moment a job exists.
The upload panel's sessionStorage recovery keys are read only in its own
initialisers, and the Copernicus panel needs no sessionStorage at all because
its idempotency key is durable in `scene_acquisitions.client_request_id`.

`createClientRequestId` was moved out of `SceneUploadPanel.jsx` into
`utils/helpers.js` so both paths share one implementation. That is a pure move
— the function body is unchanged and the upload path still calls it in exactly
one place.

### Degrading when the feature is off

`GET /acquisitions/providers` is fetched once with `staleTime: Infinity` and
`retry: false`, under a cache key shared by the workspace and the panel, so
opening the modal and then the panel costs one request. When it reports
`enabled: false` — or 404s against an older API — the "Fetch from Copernicus"
card in the Add-a-scene modal is disabled with a plain-language note and
nothing else about the workspace changes.

## Testing without spending quota

```bash
pytest backend/tests/test_copernicus_client.py
pytest backend/tests/test_acquisition_archive_layout.py
pytest backend/tests/test_upload_archive_validation.py
pytest backend/tests/test_job_stage_enum_parity.py
```

All four are fully offline. The client suite drives an `httpx.MockTransport`
through the `_http_client` seam against fixtures in `backend/tests/fixtures/`.

Two checks worth doing by hand before trusting this in production:

1. **Synthetic end to end.** Build a small ZIP with the real SAFE layout, serve
   it from a local HTTP server, and point `COPERNICUS_DOWNLOAD_URL` at it —
   that setting exists precisely so this is possible. Run the fetch twice and
   assert the second reports `reused_existing_object: true`.
2. **One real product, once.** Download a real ~1 GB IW GRDH 1SDV by hand, park
   it behind local nginx or MinIO, and replay the worker against it to exercise
   the resume loop, the heartbeat, and scratch sizing — without touching quota
   again.

## Operational notes

- **Worker scratch.** `M3_WORKER_SCRATCH_ROOT` defaults to
  `/tmp/raikou-workers`, and `_prepare_vrt` re-downloads the archive for *every*
  subsequent stage. Budget roughly 2× product size per concurrent task on a
  real volume, not the container overlay. This is pre-existing behaviour for
  uploads; Copernicus makes 1 GB routine.
- **Cleanup needs no new code.** `scene_acquisitions` cascades from `scenes`,
  and `artifacts_for_cleanup(include_sources=True)` already deletes the fetched
  object during scene cleanup.

## Known gaps

- Long-term-archive products are shown greyed out and are unselectable. There
  is no ordering flow; a user must order them in the Copernicus Browser.
- Search areas crossing the antimeridian are rejected with an explicit message.
  Search each side separately.
- `scattering_map.py:205` still hardcodes `ground_sampling_m = block * 10`.
  Correct for IW GRDH, so acquisitions are safe, but it remains a silent 4×
  error for a hand-uploaded GRDM. Separate ticket.
- `uploads.py` `_ACTIVE_JOB_STATUSES` omits `validating`/`processing`. Inert
  today because the scene-status guard covers it. Separate ticket.
