-- M1: identity, ownership, and durable V1 domain data.
--
-- This migration is intentionally additive for the legacy conversations/messages
-- tables.  The old chat flow can keep writing user_id/session_id/mode/sources while
-- M1 product routes use owner_id/project_id/scene_id.

create extension if not exists pgcrypto;

-- Lifecycle values are explicit so workers and clients cannot invent ambiguous
-- string states. Add a later migration when a new state is genuinely required.
do $$
begin
  if not exists (select 1 from pg_type where typname = 'scene_status' and typnamespace = 'public'::regnamespace) then
    create type public.scene_status as enum (
      'draft', 'uploading', 'uploaded', 'queued', 'processing',
      'ready', 'failed', 'cancelled', 'archived'
    );
  end if;

  if not exists (select 1 from pg_type where typname = 'artifact_kind' and typnamespace = 'public'::regnamespace) then
    create type public.artifact_kind as enum (
      'source_archive', 'source_raster', 'metadata', 'vrt', 'overview',
      'thumbnail', 'patch_preview', 'evidence', 'other'
    );
  end if;

  if not exists (select 1 from pg_type where typname = 'artifact_status' and typnamespace = 'public'::regnamespace) then
    create type public.artifact_status as enum ('pending', 'available', 'failed', 'deleted');
  end if;

  if not exists (select 1 from pg_type where typname = 'processing_job_stage' and typnamespace = 'public'::regnamespace) then
    create type public.processing_job_stage as enum (
      'validate_upload', 'extract_metadata', 'build_overview', 'tile_patches',
      'embed_patches', 'index_vectors', 'build_evidence', 'finalize'
    );
  end if;

  if not exists (select 1 from pg_type where typname = 'processing_job_status' and typnamespace = 'public'::regnamespace) then
    create type public.processing_job_status as enum ('queued', 'running', 'succeeded', 'failed', 'cancelled');
  end if;

  if not exists (select 1 from pg_type where typname = 'patch_status' and typnamespace = 'public'::regnamespace) then
    create type public.patch_status as enum ('pending', 'ready', 'failed', 'deleted');
  end if;

  if not exists (select 1 from pg_type where typname = 'evidence_record_status' and typnamespace = 'public'::regnamespace) then
    create type public.evidence_record_status as enum ('pending', 'ready', 'failed', 'superseded');
  end if;

  if not exists (select 1 from pg_type where typname = 'conversation_status' and typnamespace = 'public'::regnamespace) then
    create type public.conversation_status as enum ('active', 'archived');
  end if;

  if not exists (select 1 from pg_type where typname = 'message_role' and typnamespace = 'public'::regnamespace) then
    create type public.message_role as enum ('system', 'user', 'assistant');
  end if;

  if not exists (select 1 from pg_type where typname = 'message_status' and typnamespace = 'public'::regnamespace) then
    create type public.message_status as enum ('pending', 'streaming', 'complete', 'failed', 'cancelled');
  end if;
end
$$;

create or replace function public.set_updated_at()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create table if not exists public.profiles (
  id uuid primary key references auth.users (id) on delete cascade,
  display_name text,
  avatar_url text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint profiles_display_name_length_ck check (display_name is null or char_length(display_name) <= 120),
  constraint profiles_avatar_url_length_ck check (avatar_url is null or char_length(avatar_url) <= 2048)
);

-- The legacy application may already have created a minimal profile table.
-- Bring it up to the M1 shape before functions and policies refer to these columns.
alter table public.profiles
  add column if not exists display_name text,
  add column if not exists avatar_url text,
  add column if not exists created_at timestamptz not null default now(),
  add column if not exists updated_at timestamptz not null default now();

create table if not exists public.projects (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null default auth.uid() references auth.users (id) on delete cascade,
  name text not null,
  description text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint projects_name_not_blank_ck check (char_length(btrim(name)) between 1 and 160),
  constraint projects_description_length_ck check (char_length(description) <= 5000),
  constraint projects_id_owner_key unique (id, owner_id)
);

create table if not exists public.scenes (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null default auth.uid(),
  project_id uuid not null,
  name text not null,
  status public.scene_status not null default 'draft',
  metadata jsonb not null default '{}'::jsonb,
  sensor text,
  acquisition_time timestamptz,
  polarizations text[] not null default '{}'::text[],
  source_artifact_id uuid,
  failure_code text,
  failure_detail text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint scenes_name_not_blank_ck check (char_length(btrim(name)) between 1 and 160),
  constraint scenes_project_owner_fkey foreign key (project_id, owner_id)
    references public.projects (id, owner_id) on delete cascade,
  constraint scenes_scope_key unique (id, project_id, owner_id)
);

create table if not exists public.scene_artifacts (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null default auth.uid(),
  project_id uuid not null,
  scene_id uuid not null,
  kind public.artifact_kind not null,
  status public.artifact_status not null default 'pending',
  storage_bucket text not null default 'sar-scenes',
  storage_key text not null,
  content_type text,
  size_bytes bigint,
  checksum_sha256 text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint scene_artifacts_scope_fkey foreign key (scene_id, project_id, owner_id)
    references public.scenes (id, project_id, owner_id) on delete cascade,
  constraint scene_artifacts_scope_key unique (id, scene_id, project_id, owner_id),
  constraint scene_artifacts_bucket_key_unique unique (storage_bucket, storage_key),
  constraint scene_artifacts_storage_key_not_blank_ck check (char_length(btrim(storage_key)) > 0),
  constraint scene_artifacts_size_nonnegative_ck check (size_bytes is null or size_bytes >= 0),
  constraint scene_artifacts_checksum_format_ck check (
    checksum_sha256 is null or checksum_sha256 ~ '^[0-9A-Fa-f]{64}$'
  )
);

-- Keep the scene's source pointer scoped to its own artifacts. The column list on
-- SET NULL preserves the scene scope when a source artifact is deliberately removed.
do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conname = 'scenes_source_artifact_scope_fkey'
      and conrelid = 'public.scenes'::regclass
  ) then
    alter table public.scenes
      add constraint scenes_source_artifact_scope_fkey
      foreign key (source_artifact_id, id, project_id, owner_id)
      references public.scene_artifacts (id, scene_id, project_id, owner_id)
      on delete set null (source_artifact_id);
  end if;
end
$$;

create table if not exists public.processing_jobs (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null default auth.uid(),
  project_id uuid not null,
  scene_id uuid not null,
  stage public.processing_job_stage not null default 'validate_upload',
  status public.processing_job_status not null default 'queued',
  progress smallint not null default 0,
  attempt integer not null default 0,
  max_attempts integer not null default 3,
  worker_job_id text,
  error_code text,
  error_detail text,
  started_at timestamptz,
  finished_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint processing_jobs_scope_fkey foreign key (scene_id, project_id, owner_id)
    references public.scenes (id, project_id, owner_id) on delete cascade,
  constraint processing_jobs_progress_ck check (progress between 0 and 100),
  constraint processing_jobs_attempt_ck check (attempt >= 0 and max_attempts >= 1 and attempt <= max_attempts),
  constraint processing_jobs_finished_after_started_ck check (
    finished_at is null or started_at is null or finished_at >= started_at
  )
);

create table if not exists public.patches (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null default auth.uid(),
  project_id uuid not null,
  scene_id uuid not null,
  qdrant_point_id uuid not null default gen_random_uuid(),
  source_artifact_id uuid,
  preview_artifact_id uuid,
  row_start integer not null,
  row_end integer not null,
  col_start integer not null,
  col_end integer not null,
  patch_size integer not null,
  status public.patch_status not null default 'pending',
  quality jsonb not null default '{}'::jsonb,
  model_name text,
  model_version text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint patches_scope_fkey foreign key (scene_id, project_id, owner_id)
    references public.scenes (id, project_id, owner_id) on delete cascade,
  constraint patches_source_artifact_scope_fkey foreign key (source_artifact_id, scene_id, project_id, owner_id)
    references public.scene_artifacts (id, scene_id, project_id, owner_id)
    on delete set null (source_artifact_id),
  constraint patches_preview_artifact_scope_fkey foreign key (preview_artifact_id, scene_id, project_id, owner_id)
    references public.scene_artifacts (id, scene_id, project_id, owner_id)
    on delete set null (preview_artifact_id),
  constraint patches_qdrant_point_id_key unique (qdrant_point_id),
  constraint patches_bounds_ck check (
    row_start >= 0 and col_start >= 0
    and row_end > row_start and col_end > col_start
    and patch_size > 0
  ),
  -- Pending patches may not have provenance yet. A ready/searchable patch must
  -- always retain the source and model details that its Qdrant payload needs.
  constraint patches_ready_provenance_ck check (
    status <> 'ready'::public.patch_status
    or (source_artifact_id is not null and model_name is not null and model_version is not null)
  )
);

create table if not exists public.scene_evidence_records (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null default auth.uid(),
  project_id uuid not null,
  scene_id uuid not null,
  record_version integer not null default 1,
  status public.evidence_record_status not null default 'pending',
  is_current boolean not null default true,
  summary text,
  facts jsonb not null default '[]'::jsonb,
  metadata jsonb not null default '{}'::jsonb,
  model_name text,
  model_version text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint scene_evidence_records_scope_fkey foreign key (scene_id, project_id, owner_id)
    references public.scenes (id, project_id, owner_id) on delete cascade,
  constraint scene_evidence_records_version_ck check (record_version >= 1),
  constraint scene_evidence_records_scene_version_key unique (scene_id, record_version)
);

-- conversations and messages retain legacy fields for the existing SAR chat UI.
-- New code should supply owner_id/project_id/scene_id, while legacy code may supply
-- user_id and omit project_id/scene_id until M5 moves it behind FastAPI.
create table if not exists public.conversations (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null default auth.uid() references auth.users (id) on delete cascade,
  user_id uuid not null references auth.users (id) on delete cascade,
  project_id uuid,
  scene_id uuid,
  title text not null default 'Untitled conversation',
  status public.conversation_status not null default 'active',
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint conversations_owner_user_match_ck check (owner_id = user_id),
  constraint conversations_project_owner_fkey foreign key (project_id, owner_id)
    references public.projects (id, owner_id) on delete cascade,
  constraint conversations_scene_scope_fkey foreign key (scene_id, project_id, owner_id)
    references public.scenes (id, project_id, owner_id) on delete cascade,
  constraint conversations_id_owner_key unique (id, owner_id),
  constraint conversations_scene_requires_project_ck check (scene_id is null or project_id is not null)
);

create table if not exists public.messages (
  id uuid primary key default gen_random_uuid(),
  conversation_id uuid not null,
  owner_id uuid not null default auth.uid(),
  project_id uuid,
  scene_id uuid,
  role public.message_role not null default 'user',
  content text not null,
  session_id text,
  mode text,
  sources jsonb not null default '[]'::jsonb,
  status public.message_status not null default 'pending',
  error_detail text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint messages_conversation_owner_fkey foreign key (conversation_id, owner_id)
    references public.conversations (id, owner_id) on delete cascade,
  constraint messages_scene_requires_project_ck check (scene_id is null or project_id is not null)
);

-- Add M1 fields to legacy chat tables if those tables were created before migrations.
-- The existing prototype already uses UUID conversation/message IDs. Fail with a
-- clear migration error instead of applying UUID defaults to an incompatible
-- integer-key deployment.
do $$
declare
  conversation_id_type text;
  message_id_type text;
begin
  select c.data_type into conversation_id_type
  from information_schema.columns c
  where c.table_schema = 'public' and c.table_name = 'conversations' and c.column_name = 'id';

  select c.data_type into message_id_type
  from information_schema.columns c
  where c.table_schema = 'public' and c.table_name = 'messages' and c.column_name = 'id';

  if conversation_id_type is distinct from 'uuid' or message_id_type is distinct from 'uuid' then
    raise exception 'M1 requires UUID conversations/messages IDs; migrate legacy integer IDs before applying this migration';
  end if;
end
$$;

alter table public.conversations
  add column if not exists owner_id uuid,
  add column if not exists user_id uuid,
  add column if not exists project_id uuid,
  add column if not exists scene_id uuid,
  add column if not exists status public.conversation_status not null default 'active',
  add column if not exists metadata jsonb not null default '{}'::jsonb,
  add column if not exists created_at timestamptz not null default now(),
  add column if not exists updated_at timestamptz not null default now();

update public.conversations
set owner_id = user_id
where owner_id is null and user_id is not null;

update public.conversations
set user_id = owner_id
where user_id is null and owner_id is not null;

update public.conversations
set created_at = now()
where created_at is null;

update public.conversations
set updated_at = created_at
where updated_at is null;

do $$
declare
  current_type text;
begin
  select c.udt_name into current_type
  from information_schema.columns c
  where c.table_schema = 'public' and c.table_name = 'conversations' and c.column_name = 'status';

  if current_type is distinct from 'conversation_status' then
    alter table public.conversations alter column status drop default;
    alter table public.conversations
      alter column status type public.conversation_status
      using (
        case lower(coalesce(status::text, 'active'))
          when 'archived' then 'archived'::public.conversation_status
          else 'active'::public.conversation_status
        end
      );
    alter table public.conversations alter column status set default 'active';
  end if;

  if exists (select 1 from public.conversations where owner_id is null or user_id is null) then
    raise exception 'Cannot migrate conversations without a user_id/owner_id';
  end if;

  if exists (select 1 from public.conversations where owner_id <> user_id) then
    raise exception 'Cannot migrate conversations whose owner_id and user_id disagree';
  end if;

  alter table public.conversations alter column owner_id set not null;
  alter table public.conversations alter column user_id set not null;
  alter table public.conversations alter column owner_id set default auth.uid();
  alter table public.conversations alter column status set not null;
  alter table public.conversations alter column created_at set not null;
  alter table public.conversations alter column updated_at set not null;
  alter table public.conversations alter column id set default gen_random_uuid();
  alter table public.conversations alter column created_at set default now();
  alter table public.conversations alter column updated_at set default now();
end
$$;

alter table public.messages
  add column if not exists owner_id uuid,
  add column if not exists project_id uuid,
  add column if not exists scene_id uuid,
  add column if not exists session_id text,
  add column if not exists mode text,
  add column if not exists sources jsonb not null default '[]'::jsonb,
  add column if not exists status public.message_status not null default 'pending',
  add column if not exists error_detail text,
  add column if not exists created_at timestamptz not null default now(),
  add column if not exists updated_at timestamptz not null default now();

update public.messages as m
set
  owner_id = c.owner_id,
  project_id = c.project_id,
  scene_id = c.scene_id
from public.conversations as c
where c.id = m.conversation_id
  and (
    m.owner_id is distinct from c.owner_id
    or m.project_id is distinct from c.project_id
    or m.scene_id is distinct from c.scene_id
  );

update public.messages
set created_at = now()
where created_at is null;

update public.messages
set updated_at = created_at
where updated_at is null;

-- Legacy chat deployments constrained role/status as text values. Those
-- expressions become invalid once the columns are converted to enums.
alter table public.messages drop constraint if exists messages_role_check;
alter table public.messages drop constraint if exists messages_role_shape_check;
alter table public.messages drop constraint if exists messages_status_check;

alter table public.messages alter column role drop default;
alter table public.messages
  alter column role type public.message_role
  using (
    case
      when lower(coalesce(role::text, 'system'::text)) = 'user'::text
        then 'user'::public.message_role
      when lower(coalesce(role::text, 'system'::text)) = 'assistant'::text
        then 'assistant'::public.message_role
      else 'system'::public.message_role
    end
  );
alter table public.messages alter column role set default 'user'::public.message_role;

alter table public.messages alter column status drop default;
alter table public.messages
  alter column status type public.message_status
  using (
    case
      when lower(coalesce(status::text, 'pending'::text)) = 'pending'::text
        then 'pending'::public.message_status
      when lower(coalesce(status::text, 'pending'::text)) = 'streaming'::text
        then 'streaming'::public.message_status
      when lower(coalesce(status::text, 'pending'::text)) = 'complete'::text
        then 'complete'::public.message_status
      when lower(coalesce(status::text, 'pending'::text)) = 'completed'::text
        then 'complete'::public.message_status
      when lower(coalesce(status::text, 'pending'::text)) = 'failed'::text
        then 'failed'::public.message_status
      when lower(coalesce(status::text, 'pending'::text)) = 'error'::text
        then 'failed'::public.message_status
      when lower(coalesce(status::text, 'pending'::text)) = 'cancelled'::text
        then 'cancelled'::public.message_status
      when lower(coalesce(status::text, 'pending'::text)) = 'canceled'::text
        then 'cancelled'::public.message_status
      else 'failed'::public.message_status
    end
  );
alter table public.messages alter column status set default 'pending'::public.message_status;

do $$
begin
  if exists (select 1 from public.messages where owner_id is null) then
    raise exception 'Cannot migrate messages without a parent conversation owner';
  end if;

  alter table public.messages alter column owner_id set not null;
  alter table public.messages alter column owner_id set default auth.uid();
  alter table public.messages alter column role set not null;
  alter table public.messages alter column status set not null;
  alter table public.messages alter column created_at set not null;
  alter table public.messages alter column updated_at set not null;
  alter table public.messages alter column id set default gen_random_uuid();
  alter table public.messages alter column created_at set default now();
  alter table public.messages alter column updated_at set default now();
end
$$;

-- Constraints that may be missing only when legacy conversations/messages already
-- existed. Fresh tables received the same named constraints at CREATE TABLE time.
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'conversations_owner_id_fkey' and conrelid = 'public.conversations'::regclass) then
    alter table public.conversations add constraint conversations_owner_id_fkey
      foreign key (owner_id) references auth.users (id) on delete cascade;
  end if;

  if not exists (select 1 from pg_constraint where conname = 'conversations_user_id_fkey' and conrelid = 'public.conversations'::regclass) then
    alter table public.conversations add constraint conversations_user_id_fkey
      foreign key (user_id) references auth.users (id) on delete cascade;
  end if;

  if not exists (select 1 from pg_constraint where conname = 'conversations_owner_user_match_ck' and conrelid = 'public.conversations'::regclass) then
    alter table public.conversations add constraint conversations_owner_user_match_ck check (owner_id = user_id);
  end if;

  if not exists (select 1 from pg_constraint where conname = 'conversations_project_owner_fkey' and conrelid = 'public.conversations'::regclass) then
    alter table public.conversations add constraint conversations_project_owner_fkey
      foreign key (project_id, owner_id) references public.projects (id, owner_id) on delete cascade;
  end if;

  if not exists (select 1 from pg_constraint where conname = 'conversations_scene_scope_fkey' and conrelid = 'public.conversations'::regclass) then
    alter table public.conversations add constraint conversations_scene_scope_fkey
      foreign key (scene_id, project_id, owner_id) references public.scenes (id, project_id, owner_id) on delete cascade;
  end if;

  if not exists (select 1 from pg_constraint where conname = 'conversations_id_owner_key' and conrelid = 'public.conversations'::regclass) then
    alter table public.conversations add constraint conversations_id_owner_key unique (id, owner_id);
  end if;

  if not exists (select 1 from pg_constraint where conname = 'conversations_scene_requires_project_ck' and conrelid = 'public.conversations'::regclass) then
    alter table public.conversations add constraint conversations_scene_requires_project_ck check (scene_id is null or project_id is not null);
  end if;

  if not exists (select 1 from pg_constraint where conname = 'messages_conversation_owner_fkey' and conrelid = 'public.messages'::regclass) then
    alter table public.messages add constraint messages_conversation_owner_fkey
      foreign key (conversation_id, owner_id) references public.conversations (id, owner_id) on delete cascade;
  end if;

  if not exists (select 1 from pg_constraint where conname = 'messages_scene_requires_project_ck' and conrelid = 'public.messages'::regclass) then
    alter table public.messages add constraint messages_scene_requires_project_ck check (scene_id is null or project_id is not null);
  end if;
end
$$;

-- Keep legacy user_id and M1 owner_id identical. Messages always inherit their
-- owner and scope from the conversation, so a user cannot attach a message to a
-- different user's conversation through direct PostgREST access.
create or replace function public.sync_conversation_owner()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  if new.owner_id is null then
    new.owner_id = new.user_id;
  end if;

  if new.user_id is null then
    new.user_id = new.owner_id;
  end if;

  if new.owner_id is null or new.user_id is null or new.owner_id <> new.user_id then
    raise exception 'owner_id and user_id must identify the same user';
  end if;

  return new;
end;
$$;

create or replace function public.sync_message_scope()
returns trigger
language plpgsql
set search_path = public
as $$
declare
  parent_conversation public.conversations%rowtype;
begin
  select * into parent_conversation
  from public.conversations
  where id = new.conversation_id;

  if not found then
    raise exception 'conversation % is not accessible', new.conversation_id;
  end if;

  if new.owner_id is null then
    new.owner_id = parent_conversation.owner_id;
  elsif new.owner_id <> parent_conversation.owner_id then
    raise exception 'message owner must match its conversation owner';
  end if;

  if new.project_id is null then
    new.project_id = parent_conversation.project_id;
  elsif new.project_id is distinct from parent_conversation.project_id then
    raise exception 'message project must match its conversation project';
  end if;

  if new.scene_id is null then
    new.scene_id = parent_conversation.scene_id;
  elsif new.scene_id is distinct from parent_conversation.scene_id then
    raise exception 'message scene must match its conversation scene';
  end if;

  return new;
end;
$$;

create or replace function public.prevent_conversation_scope_change()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  if (new.owner_id, new.project_id, new.scene_id) is distinct from (old.owner_id, old.project_id, old.scene_id)
     and exists (select 1 from public.messages where conversation_id = old.id) then
    raise exception 'conversation ownership and scope cannot change after it has messages';
  end if;
  return new;
end;
$$;

drop trigger if exists conversations_sync_owner on public.conversations;
create trigger conversations_sync_owner
before insert or update of owner_id, user_id on public.conversations
for each row execute function public.sync_conversation_owner();

drop trigger if exists messages_sync_scope on public.messages;
create trigger messages_sync_scope
before insert or update of conversation_id, owner_id, project_id, scene_id on public.messages
for each row execute function public.sync_message_scope();

drop trigger if exists conversations_lock_scope_after_messages on public.conversations;
create trigger conversations_lock_scope_after_messages
before update of owner_id, project_id, scene_id on public.conversations
for each row execute function public.prevent_conversation_scope_change();

-- Existing auth users receive profiles immediately; future users are handled by the
-- auth.users trigger below. Profile edits are left to the authenticated owner.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.profiles (id, display_name, avatar_url)
  values (
    new.id,
    nullif(coalesce(new.raw_user_meta_data ->> 'full_name', new.raw_user_meta_data ->> 'name', split_part(new.email, '@', 1)), ''),
    nullif(new.raw_user_meta_data ->> 'avatar_url', '')
  )
  on conflict (id) do nothing;
  return new;
end;
$$;

insert into public.profiles (id, display_name, avatar_url)
select
  u.id,
  nullif(coalesce(u.raw_user_meta_data ->> 'full_name', u.raw_user_meta_data ->> 'name', split_part(u.email, '@', 1)), ''),
  nullif(u.raw_user_meta_data ->> 'avatar_url', '')
from auth.users as u
on conflict (id) do nothing;

drop trigger if exists on_auth_user_created_m1_profile on auth.users;
create trigger on_auth_user_created_m1_profile
after insert on auth.users
for each row execute function public.handle_new_user();

-- Timestamp maintenance is server-owned and applies to every mutable domain row.
drop trigger if exists profiles_set_updated_at on public.profiles;
create trigger profiles_set_updated_at before update on public.profiles
for each row execute function public.set_updated_at();

drop trigger if exists projects_set_updated_at on public.projects;
create trigger projects_set_updated_at before update on public.projects
for each row execute function public.set_updated_at();

drop trigger if exists scenes_set_updated_at on public.scenes;
create trigger scenes_set_updated_at before update on public.scenes
for each row execute function public.set_updated_at();

drop trigger if exists scene_artifacts_set_updated_at on public.scene_artifacts;
create trigger scene_artifacts_set_updated_at before update on public.scene_artifacts
for each row execute function public.set_updated_at();

drop trigger if exists processing_jobs_set_updated_at on public.processing_jobs;
create trigger processing_jobs_set_updated_at before update on public.processing_jobs
for each row execute function public.set_updated_at();

drop trigger if exists patches_set_updated_at on public.patches;
create trigger patches_set_updated_at before update on public.patches
for each row execute function public.set_updated_at();

drop trigger if exists scene_evidence_records_set_updated_at on public.scene_evidence_records;
create trigger scene_evidence_records_set_updated_at before update on public.scene_evidence_records
for each row execute function public.set_updated_at();

drop trigger if exists conversations_set_updated_at on public.conversations;
create trigger conversations_set_updated_at before update on public.conversations
for each row execute function public.set_updated_at();

drop trigger if exists messages_set_updated_at on public.messages;
create trigger messages_set_updated_at before update on public.messages
for each row execute function public.set_updated_at();

-- Query paths for the V1 API and worker. In particular, worker polling does not
-- require scanning scene data, and search never has to infer owner scope.
create index if not exists projects_owner_created_idx on public.projects (owner_id, created_at desc);
create index if not exists scenes_project_created_idx on public.scenes (project_id, created_at desc);
create index if not exists scenes_owner_status_updated_idx on public.scenes (owner_id, status, updated_at desc);
create index if not exists scenes_metadata_gin_idx on public.scenes using gin (metadata);
create index if not exists scene_artifacts_scene_kind_idx on public.scene_artifacts (scene_id, kind, created_at desc);
create index if not exists scene_artifacts_owner_project_idx on public.scene_artifacts (owner_id, project_id);
create index if not exists processing_jobs_scene_created_idx on public.processing_jobs (scene_id, created_at desc);
create index if not exists processing_jobs_status_created_idx on public.processing_jobs (status, created_at);
create unique index if not exists processing_jobs_one_active_per_scene_idx
  on public.processing_jobs (scene_id)
  where status in ('queued'::public.processing_job_status, 'running'::public.processing_job_status);
create index if not exists patches_scene_grid_idx on public.patches (scene_id, row_start, col_start);
create index if not exists patches_owner_project_scene_idx on public.patches (owner_id, project_id, scene_id);
create index if not exists scene_evidence_records_scene_current_idx
  on public.scene_evidence_records (scene_id, updated_at desc)
  where is_current;
create unique index if not exists scene_evidence_records_one_current_idx
  on public.scene_evidence_records (scene_id)
  where is_current;
create index if not exists conversations_owner_project_updated_idx on public.conversations (owner_id, project_id, updated_at desc);
create index if not exists messages_conversation_created_idx on public.messages (conversation_id, created_at);
create index if not exists messages_owner_project_created_idx on public.messages (owner_id, project_id, created_at);

-- Canonical Qdrant payload contract for the sar_patches collection:
-- owner_id, project_id, scene_id, patch bounds, source_artifact_id, and model
-- metadata must be copied from these columns on every upsert and used as filters
-- on every search. qdrant_point_id is the durable link back to this record.
comment on table public.patches is
  'Canonical relational record for sar_patches vectors. Qdrant payload must include owner_id, project_id, scene_id, bounds, source_artifact_id, model_name, and model_version.';
comment on column public.patches.qdrant_point_id is
  'Stable Qdrant point UUID for this patch; never reuse it across a different patch.';
comment on column public.patches.source_artifact_id is
  'Source raster/archive artifact used to derive this patch.';

-- Row Level Security is defense in depth for direct Supabase/PostgREST access.
-- FastAPI must still resolve ownership before it uses a service-role database client.
alter table public.profiles enable row level security;
alter table public.projects enable row level security;
alter table public.scenes enable row level security;
alter table public.scene_artifacts enable row level security;
alter table public.processing_jobs enable row level security;
alter table public.patches enable row level security;
alter table public.scene_evidence_records enable row level security;
alter table public.conversations enable row level security;
alter table public.messages enable row level security;

-- An older deployment may already have permissive policies on the legacy chat
-- tables. RLS policies combine with OR semantics, so replace every policy on
-- these V1 domain tables before declaring the owner-scoped policy set below.
do $$
declare
  existing_policy record;
begin
  for existing_policy in
    select policyname, tablename
    from pg_policies
    where schemaname = 'public'
      and tablename in (
        'profiles', 'projects', 'scenes', 'scene_artifacts', 'processing_jobs',
        'patches', 'scene_evidence_records', 'conversations', 'messages'
      )
  loop
    execute format('drop policy if exists %I on public.%I', existing_policy.policyname, existing_policy.tablename);
  end loop;
end
$$;

drop policy if exists profiles_select_own on public.profiles;
create policy profiles_select_own on public.profiles
for select to authenticated
using (id = (select auth.uid()));

drop policy if exists profiles_insert_own on public.profiles;
create policy profiles_insert_own on public.profiles
for insert to authenticated
with check (id = (select auth.uid()));

drop policy if exists profiles_update_own on public.profiles;
create policy profiles_update_own on public.profiles
for update to authenticated
using (id = (select auth.uid()))
with check (id = (select auth.uid()));

drop policy if exists projects_owner_access on public.projects;
create policy projects_owner_access on public.projects
for all to authenticated
using (owner_id = (select auth.uid()))
with check (owner_id = (select auth.uid()));

drop policy if exists scenes_owner_access on public.scenes;
create policy scenes_owner_access on public.scenes
for all to authenticated
using (owner_id = (select auth.uid()))
with check (owner_id = (select auth.uid()));

drop policy if exists scene_artifacts_owner_access on public.scene_artifacts;
create policy scene_artifacts_owner_access on public.scene_artifacts
for all to authenticated
using (owner_id = (select auth.uid()))
with check (owner_id = (select auth.uid()));

drop policy if exists processing_jobs_owner_access on public.processing_jobs;
create policy processing_jobs_owner_access on public.processing_jobs
for all to authenticated
using (owner_id = (select auth.uid()))
with check (owner_id = (select auth.uid()));

drop policy if exists patches_owner_access on public.patches;
create policy patches_owner_access on public.patches
for all to authenticated
using (owner_id = (select auth.uid()))
with check (owner_id = (select auth.uid()));

drop policy if exists scene_evidence_records_owner_access on public.scene_evidence_records;
create policy scene_evidence_records_owner_access on public.scene_evidence_records
for all to authenticated
using (owner_id = (select auth.uid()))
with check (owner_id = (select auth.uid()));

drop policy if exists conversations_owner_access on public.conversations;
create policy conversations_owner_access on public.conversations
for all to authenticated
using (owner_id = (select auth.uid()))
with check (owner_id = (select auth.uid()));

drop policy if exists messages_owner_access on public.messages;
create policy messages_owner_access on public.messages
for all to authenticated
using (owner_id = (select auth.uid()))
with check (owner_id = (select auth.uid()));

-- Product data is FastAPI-only in V1. The browser uses Supabase directly for
-- authentication, then sends its bearer token to FastAPI; it receives no
-- direct PostgREST CRUD grants for domain tables. RLS remains an additional
-- protection if a future internal tool is deliberately granted access.
revoke all on table
  public.profiles,
  public.projects,
  public.scenes,
  public.scene_artifacts,
  public.processing_jobs,
  public.patches,
  public.scene_evidence_records,
  public.conversations,
  public.messages
from public, anon, authenticated;

grant all privileges on table
  public.profiles,
  public.projects,
  public.scenes,
  public.scene_artifacts,
  public.processing_jobs,
  public.patches,
  public.scene_evidence_records,
  public.conversations,
  public.messages
to service_role;

grant usage on type
  public.scene_status,
  public.artifact_kind,
  public.artifact_status,
  public.processing_job_stage,
  public.processing_job_status,
  public.patch_status,
  public.evidence_record_status,
  public.conversation_status,
  public.message_role,
  public.message_status
to service_role;

revoke all on function public.handle_new_user() from public;
