# M5 scoped evidence search and grounded chat

M5 completes the analyst loop inside the protected project workspace. It keeps
retrieval and generation inside the authenticated owner/project boundary and
uses newline-delimited JSON (NDJSON) as the sole V1 streaming protocol.

## API surface

| Route | Purpose |
| --- | --- |
| `POST /api/v1/search` | Searches one required owned project and an optional owned scene. Metadata filters are resolved in PostgreSQL before the scoped Qdrant search. |
| `POST /api/v1/conversations` | Creates an immutable project- or scene-scoped conversation. |
| `GET /api/v1/projects/{project_id}/conversations` | Returns the caller's durable project conversation history. |
| `GET /api/v1/conversations/{conversation_id}/messages` | Returns only owned messages from a project-scoped conversation. |
| `POST /api/v1/conversations/{conversation_id}/stream` | Persists a user turn and streams a grounded assistant turn as NDJSON. |

The search handler validates the project, selected scene, and metadata-scoped
scenes before it reads Redis or contacts Qdrant. Qdrant always receives the
mandatory `owner_id` and `project_id` filter, plus the selected/narrowed scene
filter. Returned point IDs are resolved through owned PostgreSQL patch and
artifact rows before an evidence card is returned.

## Cache contract

M5 uses the M1 `RedisKeyspace.tenant_query_cache_key` helper only after the
scope is validated. Values are derived and bounded:

| Value | TTL |
| --- | --- |
| Query embedding | 60 minutes |
| Qdrant IDs and scores | 5 minutes |
| Authorized RAG context projection | 2 minutes |

Keys contain the owner/project/optional-scene scope, digest-only normalized
query and filters, SARCLIP/SARChat and Qdrant index versions, and a project
cache generation. They never include raw query text, tokens, signed URLs,
uploads, object bytes, database rows, or stream frames.

Every scene lifecycle or evidence-affecting mutation advances that project's
cache generation and clears its indexed derived keys. A request after that
change uses a different cache key even if an old Redis entry remains until its
TTL; PostgreSQL, S3, and Qdrant remain authoritative.

## Grounding and citations

Scene-scoped chat is **scene-record first**. Before any optional Qdrant search,
it reads the authorized durable scene record, including conservative land/water
context, detector provenance, detector-backed facts, and coarse spatial groups.
This means count and presence questions (for example, “how many bridges?” or
“are there any ships?”) are answered from the detector record, not from a
retrieved embedding patch. If no detector covers a requested class, the answer
says that the class cannot be confirmed; it never treats a missing detection as
proof of absence.

Environmental questions such as vegetation, forest, agriculture, or flooding
are also kept out of object retrieval. Unless a calibrated segmentation product
is present, Raikou reports the limitation and may expose only the scene's
explicitly labelled land/water backscatter heuristic. A SARCLIP hit cannot
create a land-cover claim.

Qdrant/SARCLIP remains useful for an explicitly visual or location-oriented
request—finding a supporting patch, similar scene, or visible feature. It
augments the scene record and never establishes an object class, count, or
coordinate authority. Broad scene narration receives the full overview plus
transient north-west, north-east, south-west, and south-east samples derived
from that already-authorized overview; this changes no processing workflow or
stored artifact.

The RAG prompt is bounded by settings and prohibits model observations from
becoming detections or new/generated bounding boxes. It keeps scene context,
detector-backed object candidates, and uncertain visual observations separate;
it also prohibits claims about activity, intent, temporal change, or vessel
type from one acquisition.

Every answer stream emits a `citations` NDJSON event, including an explicit
empty citation set for a safe error response. Each citation states its source
type/ID, scene, patch bounds or overview artifact, retrieval score where
applicable, and why it was supplied. The workspace makes patch/overview cards
open the authorized source and makes metadata/model/detector citations open
their scene context.

## Required configuration

Set the existing M1-M3 dependencies plus:

```dotenv
REDIS_URL=rediss://...             # ElastiCache TLS endpoint in production
VLLM_BASE_URL=http://host:8001/v1
SARCHAT_MODEL_ID=/models/SARChat-Phi-3.5-vision-instruct
M5_QDRANT_INDEX_VERSION=v1
```

`REDIS_URL` is optional only for a local cache-less development run. When it
is configured, `/readyz` requires it to be reachable.

## Migration and verification

`20260721003000_m5_scoped_evidence_chat.sql` adds M5 query indexes and the
`m5_chat_schema_ready()` probe. It is applied to the Raikou Supabase project
and recorded in migration history.

For a deployed acceptance check, use two users and two projects:

1. Create a ready scene and vectors for each user/project.
2. Call `POST /api/v1/search` with User A's bearer token and User B's project
   or scene ID; it must return `404` before Qdrant is queried.
3. Reprocess, cancel, update, or delete User A's scene, then repeat an
   authorized search. Confirm its derived Redis generation changed and the
   response is rebuilt from the current PostgreSQL/Qdrant state.
4. Create one project-scoped and one scene-scoped conversation. Reload the
   workspace after streaming a turn; both persisted messages and their source
   cards must be returned through the history API.
