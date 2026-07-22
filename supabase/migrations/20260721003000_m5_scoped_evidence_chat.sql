-- M5: indexes and readiness contract for project-scoped evidence retrieval
-- and durable, evidence-cited conversations. Existing M1 tables retain their
-- legacy nullable scope fields; M5 routes only create/read project-scoped rows.

create index if not exists conversations_owner_project_scene_updated_idx
  on public.conversations (owner_id, project_id, scene_id, updated_at desc)
  where project_id is not null;

create index if not exists messages_owner_conversation_created_idx
  on public.messages (owner_id, conversation_id, created_at asc);

create index if not exists patches_search_scope_ready_idx
  on public.patches (owner_id, project_id, scene_id, qdrant_point_id)
  where status = 'ready'::public.patch_status;

create index if not exists scene_evidence_records_current_scope_idx
  on public.scene_evidence_records (owner_id, project_id, scene_id, updated_at desc)
  where is_current and status = 'ready'::public.evidence_record_status;

create index if not exists scene_artifacts_preview_scope_idx
  on public.scene_artifacts (owner_id, project_id, scene_id, id)
  where status = 'available'::public.artifact_status
    and kind in ('overview'::public.artifact_kind, 'thumbnail'::public.artifact_kind, 'patch_preview'::public.artifact_kind);

create or replace function public.m5_chat_schema_ready()
returns boolean
language sql
stable
set search_path = public
as $$
  select
    to_regclass('public.conversations') is not null
    and to_regclass('public.messages') is not null
    and to_regclass('public.patches') is not null
    and to_regclass('public.scene_evidence_records') is not null
    and to_regclass('public.conversations_owner_project_scene_updated_idx') is not null
    and to_regclass('public.messages_owner_conversation_created_idx') is not null
    and to_regclass('public.patches_search_scope_ready_idx') is not null
    and to_regclass('public.scene_evidence_records_current_scope_idx') is not null
    and to_regclass('public.scene_artifacts_preview_scope_idx') is not null
$$;

grant execute on function public.m5_chat_schema_ready() to anon, authenticated, service_role;

comment on function public.m5_chat_schema_ready() is
  'M5 read-only readiness probe for scoped evidence search and grounded conversation persistence.';
