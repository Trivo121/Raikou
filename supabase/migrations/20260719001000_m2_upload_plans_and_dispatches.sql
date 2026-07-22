-- M2: durable direct-to-object-storage upload plans and transactional job dispatch.
--
-- This migration follows M1.  It does not store presigned URLs, browser tokens, or
-- object-storage credentials.  Those are deliberately short-lived API responses,
-- while this schema stores only the authorization scope and immutable upload plan.

do $$
begin
  if not exists (select 1 from pg_type where typname = 'upload_plan_status' and typnamespace = 'public'::regnamespace) then
    create type public.upload_plan_status as enum (
      'initiated', 'uploading', 'completing', 'completed', 'aborted', 'expired', 'failed'
    );
  end if;

  if not exists (select 1 from pg_type where typname = 'upload_plan_file_status' and typnamespace = 'public'::regnamespace) then
    create type public.upload_plan_file_status as enum (
      'planned', 'uploading', 'uploaded', 'completed', 'aborted', 'expired', 'failed'
    );
  end if;

  if not exists (select 1 from pg_type where typname = 'upload_plan_file_kind' and typnamespace = 'public'::regnamespace) then
    create type public.upload_plan_file_kind as enum (
      'source_archive', 'source_raster', 'metadata_sidecar'
    );
  end if;

  if not exists (select 1 from pg_type where typname = 'multipart_checksum_mode' and typnamespace = 'public'::regnamespace) then
    create type public.multipart_checksum_mode as enum ('sha256', 'server_verified');
  end if;

  if not exists (select 1 from pg_type where typname = 'processing_job_dispatch_status' and typnamespace = 'public'::regnamespace) then
    create type public.processing_job_dispatch_status as enum (
      'pending', 'publishing', 'published', 'retry_scheduled', 'failed', 'cancelled'
    );
  end if;
end
$$;

create table if not exists public.upload_plans (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null default auth.uid(),
  project_id uuid not null,
  scene_id uuid not null,
  status public.upload_plan_status not null default 'initiated',
  expected_file_count smallint not null,
  expires_at timestamptz not null,
  completed_at timestamptz,
  aborted_at timestamptz,
  failure_code text,
  failure_detail text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint upload_plans_scene_scope_fkey foreign key (scene_id, project_id, owner_id)
    references public.scenes (id, project_id, owner_id) on delete cascade,
  constraint upload_plans_scope_key unique (id, scene_id, project_id, owner_id),
  constraint upload_plans_expected_file_count_ck check (expected_file_count between 1 and 3),
  constraint upload_plans_expiry_after_create_ck check (expires_at > created_at),
  constraint upload_plans_completed_at_ck check (status <> 'completed' or completed_at is not null),
  constraint upload_plans_aborted_at_ck check (status <> 'aborted' or aborted_at is not null)
);

create table if not exists public.upload_plan_files (
  id uuid primary key default gen_random_uuid(),
  upload_plan_id uuid not null,
  owner_id uuid not null default auth.uid(),
  project_id uuid not null,
  scene_id uuid not null,
  file_number smallint not null,
  file_kind public.upload_plan_file_kind not null,
  status public.upload_plan_file_status not null default 'planned',
  original_filename text not null,
  content_type text not null,
  storage_bucket text not null,
  storage_key text not null,
  multipart_upload_id text not null,
  multipart_checksum_mode public.multipart_checksum_mode not null,
  expected_size_bytes bigint not null,
  -- Optional because a browser may not be able to hash a large file before upload.
  -- When present, this is standard padded base64 encoding of a 32-byte SHA-256.
  expected_checksum_sha256 text,
  part_size_bytes bigint not null,
  part_count integer not null,
  verified_size_bytes bigint,
  verified_checksum_sha256 text,
  verified_content_type text,
  object_etag text,
  object_version_id text,
  completed_at timestamptz,
  failure_code text,
  failure_detail text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint upload_plan_files_plan_scope_fkey foreign key (upload_plan_id, scene_id, project_id, owner_id)
    references public.upload_plans (id, scene_id, project_id, owner_id) on delete cascade,
  constraint upload_plan_files_plan_number_key unique (upload_plan_id, file_number),
  constraint upload_plan_files_bucket_key_unique unique (storage_bucket, storage_key),
  constraint upload_plan_files_bucket_multipart_key unique (storage_bucket, multipart_upload_id),
  constraint upload_plan_files_file_number_ck check (file_number between 1 and 3),
  constraint upload_plan_files_original_filename_ck check (
    char_length(original_filename) between 1 and 255
    and original_filename = btrim(original_filename)
    and position('/' in original_filename) = 0
    and position(chr(92) in original_filename) = 0
    and original_filename not in ('.', '..')
    and original_filename !~ '[[:cntrl:]]'
  ),
  constraint upload_plan_files_content_type_ck check (
    char_length(content_type) between 1 and 255
    and content_type = btrim(content_type)
    and content_type !~ '[[:cntrl:]]'
  ),
  constraint upload_plan_files_storage_bucket_ck check (
    char_length(storage_bucket) between 1 and 255
    and storage_bucket = btrim(storage_bucket)
    and storage_bucket !~ '[[:cntrl:]]'
  ),
  constraint upload_plan_files_storage_key_ck check (
    char_length(storage_key) between 1 and 1024
    and storage_key = btrim(storage_key)
    and left(storage_key, 1) <> '/'
    and position(chr(92) in storage_key) = 0
    and position('//' in storage_key) = 0
    and coalesce(array_position(string_to_array(storage_key, '/'), '.'), 0) = 0
    and coalesce(array_position(string_to_array(storage_key, '/'), '..'), 0) = 0
    and storage_key !~ '[[:cntrl:]]'
  ),
  constraint upload_plan_files_multipart_upload_id_ck check (
    char_length(multipart_upload_id) between 1 and 2048
    and multipart_upload_id = btrim(multipart_upload_id)
    and multipart_upload_id !~ '[[:cntrl:]]'
  ),
  constraint upload_plan_files_expected_size_ck check (expected_size_bytes > 0),
  constraint upload_plan_files_part_size_ck check (
    part_size_bytes between 5242880 and 5368709120
  ),
  constraint upload_plan_files_part_count_ck check (part_count between 1 and 10000),
  constraint upload_plan_files_part_layout_ck check (
    part_count = ((expected_size_bytes + part_size_bytes - 1) / part_size_bytes)
  ),
  constraint upload_plan_files_expected_checksum_ck check (
    expected_checksum_sha256 is null
    or expected_checksum_sha256 ~ '^[A-Za-z0-9+/]{43}=$'
  ),
  constraint upload_plan_files_verified_size_ck check (
    verified_size_bytes is null or verified_size_bytes > 0
  ),
  constraint upload_plan_files_verified_checksum_ck check (
    verified_checksum_sha256 is null
    or verified_checksum_sha256 ~ '^[A-Za-z0-9+/]{43}=$'
  ),
  constraint upload_plan_files_verified_content_type_ck check (
    verified_content_type is null
    or (
      char_length(verified_content_type) between 1 and 255
      and verified_content_type = btrim(verified_content_type)
      and verified_content_type !~ '[[:cntrl:]]'
    )
  ),
  constraint upload_plan_files_etag_ck check (
    object_etag is null
    or (char_length(object_etag) between 1 and 1024 and object_etag !~ '[[:cntrl:]]')
  ),
  constraint upload_plan_files_version_id_ck check (
    object_version_id is null
    or (char_length(object_version_id) between 1 and 1024 and object_version_id !~ '[[:cntrl:]]')
  ),
  constraint upload_plan_files_completed_fields_ck check (
    status <> 'completed'
    or (
      completed_at is not null
      and verified_size_bytes is not null
      and verified_checksum_sha256 is not null
    )
  )
);

-- M1 has a primary job ID, but this scoped key lets the outbox keep a real
-- ownership FK rather than trusting duplicated UUIDs supplied by a caller.
do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conname = 'processing_jobs_scope_key'
      and conrelid = 'public.processing_jobs'::regclass
  ) then
    alter table public.processing_jobs
      add constraint processing_jobs_scope_key unique (id, scene_id, project_id, owner_id);
  end if;
end
$$;

-- Retain a direct, scoped link from the M2-created job to its immutable
-- upload plan. This makes a POST /complete retry safe even if the database
-- committed but its response was lost on the network: FastAPI can reload the
-- exact durable job rather than guessing from a scene's job history.
alter table public.processing_jobs
  add column if not exists upload_plan_id uuid;

do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conname = 'processing_jobs_upload_plan_scope_fkey'
      and conrelid = 'public.processing_jobs'::regclass
  ) then
    alter table public.processing_jobs
      add constraint processing_jobs_upload_plan_scope_fkey
      foreign key (upload_plan_id, scene_id, project_id, owner_id)
      references public.upload_plans (id, scene_id, project_id, owner_id)
      on delete restrict;
  end if;
end
$$;

create unique index if not exists processing_jobs_upload_plan_key
  on public.processing_jobs (upload_plan_id)
  where upload_plan_id is not null;

create table if not exists public.processing_job_dispatches (
  id uuid primary key default gen_random_uuid(),
  processing_job_id uuid not null,
  owner_id uuid not null default auth.uid(),
  project_id uuid not null,
  scene_id uuid not null,
  status public.processing_job_dispatch_status not null default 'pending',
  message_type text not null default 'process_scene',
  payload jsonb not null default '{}'::jsonb,
  attempt_count integer not null default 0,
  max_attempts integer not null default 10,
  available_at timestamptz not null default now(),
  last_attempt_at timestamptz,
  locked_at timestamptz,
  locked_by text,
  published_at timestamptz,
  last_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint processing_job_dispatches_job_scope_fkey foreign key (processing_job_id, scene_id, project_id, owner_id)
    references public.processing_jobs (id, scene_id, project_id, owner_id) on delete cascade,
  constraint processing_job_dispatches_job_key unique (processing_job_id),
  constraint processing_job_dispatches_message_type_ck check (message_type = 'process_scene'),
  constraint processing_job_dispatches_payload_object_ck check (jsonb_typeof(payload) = 'object'),
  constraint processing_job_dispatches_attempts_ck check (
    attempt_count >= 0 and max_attempts >= 1 and attempt_count <= max_attempts
  ),
  constraint processing_job_dispatches_published_at_ck check (
    status <> 'published' or published_at is not null
  ),
  constraint processing_job_dispatches_locked_by_ck check (
    locked_by is null or (char_length(locked_by) between 1 and 255 and locked_by !~ '[[:cntrl:]]')
  )
);

drop trigger if exists upload_plans_set_updated_at on public.upload_plans;
create trigger upload_plans_set_updated_at
before update on public.upload_plans
for each row execute function public.set_updated_at();

drop trigger if exists upload_plan_files_set_updated_at on public.upload_plan_files;
create trigger upload_plan_files_set_updated_at
before update on public.upload_plan_files
for each row execute function public.set_updated_at();

drop trigger if exists processing_job_dispatches_set_updated_at on public.processing_job_dispatches;
create trigger processing_job_dispatches_set_updated_at
before update on public.processing_job_dispatches
for each row execute function public.set_updated_at();

create index if not exists upload_plans_owner_project_created_idx
  on public.upload_plans (owner_id, project_id, created_at desc);
create index if not exists upload_plans_scene_expiry_idx
  on public.upload_plans (scene_id, expires_at);
create index if not exists upload_plans_open_expiry_idx
  on public.upload_plans (expires_at)
  where status in (
    'initiated'::public.upload_plan_status,
    'uploading'::public.upload_plan_status,
    'completing'::public.upload_plan_status
  );
create unique index if not exists upload_plans_one_open_per_scene_idx
  on public.upload_plans (scene_id)
  where status in (
    'initiated'::public.upload_plan_status,
    'uploading'::public.upload_plan_status,
    'completing'::public.upload_plan_status
  );
create index if not exists upload_plan_files_plan_number_idx
  on public.upload_plan_files (upload_plan_id, file_number);
create index if not exists upload_plan_files_owner_scene_status_idx
  on public.upload_plan_files (owner_id, scene_id, status);
create index if not exists processing_job_dispatches_ready_idx
  on public.processing_job_dispatches (available_at, created_at)
  where status in (
    'pending'::public.processing_job_dispatch_status,
    'retry_scheduled'::public.processing_job_dispatch_status
  );
create index if not exists processing_job_dispatches_owner_scene_idx
  on public.processing_job_dispatches (owner_id, project_id, scene_id, created_at desc);
create index if not exists processing_jobs_upload_plan_lookup_idx
  on public.processing_jobs (owner_id, upload_plan_id)
  where upload_plan_id is not null;

comment on column public.upload_plan_files.expected_checksum_sha256 is
  'Optional full-file SHA-256 in standard padded base64. The API may omit it for large browser uploads.';
comment on column public.upload_plan_files.verified_checksum_sha256 is
  'Required on completion: server-verified full-file SHA-256 in standard padded base64.';
comment on table public.processing_job_dispatches is
  'Transactional outbox: persist one process_scene message intent before publishing it to Redis. Payload must never contain credentials, tokens, or signed URLs.';

-- RLS mirrors M1. Composite FKs ensure a caller cannot attach an owned child row
-- to another user's scene/job; policies then make the owner boundary explicit.
alter table public.upload_plans enable row level security;
alter table public.upload_plan_files enable row level security;
alter table public.processing_job_dispatches enable row level security;

drop policy if exists upload_plans_owner_access on public.upload_plans;
create policy upload_plans_owner_access on public.upload_plans
for all to authenticated
using (owner_id = (select auth.uid()))
with check (owner_id = (select auth.uid()));

drop policy if exists upload_plan_files_owner_access on public.upload_plan_files;
create policy upload_plan_files_owner_access on public.upload_plan_files
for all to authenticated
using (owner_id = (select auth.uid()))
with check (owner_id = (select auth.uid()));

drop policy if exists processing_job_dispatches_owner_access on public.processing_job_dispatches;
create policy processing_job_dispatches_owner_access on public.processing_job_dispatches
for all to authenticated
using (owner_id = (select auth.uid()))
with check (owner_id = (select auth.uid()));

revoke all on table
  public.upload_plans,
  public.upload_plan_files,
  public.processing_job_dispatches
from public, anon, authenticated;
-- Product upload state is FastAPI-only. The browser receives signed object-store
-- URLs, never direct table access.

grant all privileges on table
  public.upload_plans,
  public.upload_plan_files,
  public.processing_job_dispatches
to service_role;

grant usage on type
  public.upload_plan_status,
  public.upload_plan_file_status,
  public.upload_plan_file_kind,
  public.multipart_checksum_mode,
  public.processing_job_dispatch_status
to service_role;

-- Finalize the object-store-verified plan in one database transaction. The caller
-- provides only values obtained from a trusted object-store HEAD/verification step.
-- M1 scene_artifacts stores SHA-256 in lowercase hex, so this function converts the
-- M2 base64 result only at the relational artifact boundary.
create or replace function public.finalize_upload_plan(
  p_owner_id uuid,
  p_upload_plan_id uuid,
  p_verified_files jsonb,
  p_dispatch_payload jsonb default '{}'::jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  plan_row public.upload_plans%rowtype;
  primary_source_artifact_id uuid;
  created_job public.processing_jobs%rowtype;
  created_dispatch public.processing_job_dispatches%rowtype;
  completed_scene record;
  artifact_rows jsonb := '[]'::jsonb;
  submitted_file_count integer;
  planned_file_count integer;
begin
  if p_owner_id is null or p_upload_plan_id is null then
    raise exception 'owner_id and upload_plan_id are required';
  end if;

  if jsonb_typeof(p_verified_files) is distinct from 'array' then
    raise exception 'verified_files must be a JSON array';
  end if;

  if coalesce(jsonb_typeof(p_dispatch_payload), 'object') is distinct from 'object' then
    raise exception 'dispatch_payload must be a JSON object';
  end if;

  select * into plan_row
  from public.upload_plans
  where id = p_upload_plan_id and owner_id = p_owner_id
  for update;

  if not found then
    raise exception 'upload plan not found for owner' using errcode = 'P0002';
  end if;

  -- The API moves an initiated plan to completing before it asks object storage
  -- to complete multipart uploads. Only that leased state may cross the durable
  -- database boundary, preventing duplicate completion calls from creating jobs.
  if plan_row.status <> 'completing'::public.upload_plan_status then
    raise exception 'upload plan % cannot be finalized from status %', plan_row.id, plan_row.status;
  end if;

  if plan_row.expires_at <= now() then
    raise exception 'upload plan % has expired', plan_row.id;
  end if;

  select count(*) into submitted_file_count
  from jsonb_to_recordset(p_verified_files) as verified(
    upload_plan_file_id uuid,
    size_bytes bigint,
    checksum_sha256 text,
    etag text,
    version_id text,
    content_type text
  );

  select count(*) into planned_file_count
  from public.upload_plan_files
  where upload_plan_id = plan_row.id;

  if planned_file_count <> plan_row.expected_file_count then
    raise exception 'upload plan file count does not match its immutable expected_file_count';
  end if;

  if submitted_file_count <> plan_row.expected_file_count then
    raise exception 'verified file count does not match upload plan';
  end if;

  if exists (
    select 1
    from jsonb_to_recordset(p_verified_files) as verified(
      upload_plan_file_id uuid,
      size_bytes bigint,
      checksum_sha256 text,
      etag text,
      version_id text,
      content_type text
    )
    group by upload_plan_file_id
    having count(*) > 1
  ) then
    raise exception 'verified_files contains duplicate upload_plan_file_id values';
  end if;

  if exists (
    select 1
    from jsonb_to_recordset(p_verified_files) as verified(
      upload_plan_file_id uuid,
      size_bytes bigint,
      checksum_sha256 text,
      etag text,
      version_id text,
      content_type text
    )
    left join public.upload_plan_files as file_row
      on file_row.id = verified.upload_plan_file_id
      and file_row.upload_plan_id = plan_row.id
      and file_row.owner_id = plan_row.owner_id
      and file_row.project_id = plan_row.project_id
      and file_row.scene_id = plan_row.scene_id
    where file_row.id is null
  ) then
    raise exception 'verified_files contains a file outside this upload plan';
  end if;

  if exists (
    select 1
    from public.upload_plan_files as file_row
    where file_row.upload_plan_id = plan_row.id
      and not exists (
        select 1
        from jsonb_to_recordset(p_verified_files) as verified(upload_plan_file_id uuid)
        where verified.upload_plan_file_id = file_row.id
      )
  ) then
    raise exception 'verified_files does not contain every planned upload file';
  end if;

  if exists (
    select 1
    from public.upload_plan_files as file_row
    join jsonb_to_recordset(p_verified_files) as verified(
      upload_plan_file_id uuid,
      size_bytes bigint,
      checksum_sha256 text,
      etag text,
      version_id text,
      content_type text
    ) on verified.upload_plan_file_id = file_row.id
    where file_row.upload_plan_id = plan_row.id
      and file_row.status not in (
        'planned'::public.upload_plan_file_status,
        'uploading'::public.upload_plan_file_status,
        'uploaded'::public.upload_plan_file_status
      )
  ) then
    raise exception 'one or more upload plan files are not finalizable';
  end if;

  if exists (
    select 1
    from public.upload_plan_files as file_row
    join jsonb_to_recordset(p_verified_files) as verified(
      upload_plan_file_id uuid,
      size_bytes bigint,
      checksum_sha256 text,
      etag text,
      version_id text,
      content_type text
    ) on verified.upload_plan_file_id = file_row.id
    where file_row.upload_plan_id = plan_row.id
      and (
        verified.size_bytes is null
        or verified.size_bytes <> file_row.expected_size_bytes
        or verified.checksum_sha256 is null
        or verified.checksum_sha256 !~ '^[A-Za-z0-9+/]{43}=$'
        or (
          file_row.expected_checksum_sha256 is not null
          and file_row.expected_checksum_sha256 <> verified.checksum_sha256
        )
      )
  ) then
    raise exception 'verified upload size or checksum does not match its upload plan';
  end if;

  if not exists (
    select 1
    from public.upload_plan_files
    where upload_plan_id = plan_row.id
      and file_kind in (
        'source_archive'::public.upload_plan_file_kind,
        'source_raster'::public.upload_plan_file_kind
      )
  ) then
    raise exception 'an upload plan must contain at least one source scene file';
  end if;

  update public.upload_plan_files as file_row
  set
    status = 'completed',
    verified_size_bytes = verified.size_bytes,
    verified_checksum_sha256 = verified.checksum_sha256,
    verified_content_type = coalesce(nullif(btrim(verified.content_type), ''), file_row.content_type),
    object_etag = nullif(btrim(verified.etag), ''),
    object_version_id = nullif(btrim(verified.version_id), ''),
    completed_at = now(),
    failure_code = null,
    failure_detail = null
  from jsonb_to_recordset(p_verified_files) as verified(
    upload_plan_file_id uuid,
    size_bytes bigint,
    checksum_sha256 text,
    etag text,
    version_id text,
    content_type text
  )
  where file_row.id = verified.upload_plan_file_id
    and file_row.upload_plan_id = plan_row.id;

  with inserted_artifacts as (
    insert into public.scene_artifacts (
      owner_id,
      project_id,
      scene_id,
      kind,
      status,
      storage_bucket,
      storage_key,
      content_type,
      size_bytes,
      checksum_sha256,
      metadata
    )
    select
      file_row.owner_id,
      file_row.project_id,
      file_row.scene_id,
      case file_row.file_kind
        when 'source_archive'::public.upload_plan_file_kind then 'source_archive'::public.artifact_kind
        when 'source_raster'::public.upload_plan_file_kind then 'source_raster'::public.artifact_kind
        else 'metadata'::public.artifact_kind
      end,
      'available'::public.artifact_status,
      file_row.storage_bucket,
      file_row.storage_key,
      coalesce(file_row.verified_content_type, file_row.content_type),
      file_row.verified_size_bytes,
      encode(decode(file_row.verified_checksum_sha256, 'base64'), 'hex'),
      jsonb_build_object(
        'upload_plan_id', plan_row.id,
        'upload_plan_file_id', file_row.id,
        'original_filename', file_row.original_filename,
        'object_etag', file_row.object_etag,
        'object_version_id', file_row.object_version_id,
        'checksum_encoding', 'base64'
      )
    from public.upload_plan_files as file_row
    where file_row.upload_plan_id = plan_row.id
    returning id, kind, storage_bucket, storage_key, content_type, size_bytes, checksum_sha256
  )
  select coalesce(jsonb_agg(to_jsonb(inserted_artifacts)), '[]'::jsonb)
  into artifact_rows
  from inserted_artifacts;

  select artifact.id into primary_source_artifact_id
  from public.scene_artifacts as artifact
  join public.upload_plan_files as file_row
    on file_row.storage_bucket = artifact.storage_bucket
    and file_row.storage_key = artifact.storage_key
  where file_row.upload_plan_id = plan_row.id
    and file_row.file_kind in (
      'source_archive'::public.upload_plan_file_kind,
      'source_raster'::public.upload_plan_file_kind
    )
  order by
    case file_row.file_kind
      when 'source_archive'::public.upload_plan_file_kind then 0
      else 1
    end,
    file_row.file_number
  limit 1;

  if primary_source_artifact_id is null then
    raise exception 'source artifact creation failed';
  end if;

  update public.scenes
  set
    source_artifact_id = primary_source_artifact_id,
    status = 'queued'::public.scene_status,
    failure_code = null,
    failure_detail = null
  where id = plan_row.scene_id
    and project_id = plan_row.project_id
    and owner_id = plan_row.owner_id
  returning id, project_id, owner_id, status, source_artifact_id into completed_scene;

  if not found then
    raise exception 'upload plan scene no longer exists';
  end if;

  insert into public.processing_jobs (
    upload_plan_id,
    owner_id,
    project_id,
    scene_id,
    stage,
    status,
    progress,
    attempt,
    max_attempts
  )
  values (
    plan_row.id,
    plan_row.owner_id,
    plan_row.project_id,
    plan_row.scene_id,
    'validate_upload'::public.processing_job_stage,
    'queued'::public.processing_job_status,
    0,
    0,
    3
  )
  returning * into created_job;

  insert into public.processing_job_dispatches (
    processing_job_id,
    owner_id,
    project_id,
    scene_id,
    status,
    message_type,
    payload,
    attempt_count,
    max_attempts,
    available_at
  )
  values (
    created_job.id,
    plan_row.owner_id,
    plan_row.project_id,
    plan_row.scene_id,
    'pending'::public.processing_job_dispatch_status,
    'process_scene',
    coalesce(p_dispatch_payload, '{}'::jsonb) || jsonb_build_object(
      'dispatch_id', gen_random_uuid(),
      'processing_job_id', created_job.id,
      'upload_plan_id', plan_row.id,
      'owner_id', plan_row.owner_id,
      'project_id', plan_row.project_id,
      'scene_id', plan_row.scene_id
    ),
    0,
    10,
    now()
  )
  returning * into created_dispatch;

  -- Make the generated dispatch row ID authoritative in the JSON message identity.
  update public.processing_job_dispatches
  set payload = payload || jsonb_build_object('dispatch_id', created_dispatch.id)
  where id = created_dispatch.id;

  update public.upload_plans
  set
    status = 'completed',
    completed_at = now(),
    failure_code = null,
    failure_detail = null
  where id = plan_row.id;

  return jsonb_build_object(
    'upload_plan', jsonb_build_object(
      'id', plan_row.id,
      'status', 'completed',
      'completed_at', now()
    ),
    'scene', jsonb_build_object(
      'id', completed_scene.id,
      'project_id', completed_scene.project_id,
      'owner_id', completed_scene.owner_id,
      'status', completed_scene.status,
      'source_artifact_id', completed_scene.source_artifact_id
    ),
    'processing_job', jsonb_build_object(
      'id', created_job.id,
      'status', created_job.status,
      'stage', created_job.stage
    ),
    'dispatch', jsonb_build_object(
      'id', created_dispatch.id,
      'status', created_dispatch.status,
      'processing_job_id', created_dispatch.processing_job_id
    ),
    'artifacts', artifact_rows
  );
end;
$$;

revoke all on function public.finalize_upload_plan(uuid, uuid, jsonb, jsonb)
from public, anon, authenticated;
grant execute on function public.finalize_upload_plan(uuid, uuid, jsonb, jsonb)
to service_role;

-- Every non-success terminal transition changes the plan, its files, and the
-- scene in one transaction. Without this boundary a delayed expiry/failure
-- handler for plan A could overwrite plan B's newly-uploading scene state.
create or replace function public.transition_upload_plan_terminal(
  p_owner_id uuid,
  p_upload_plan_id uuid,
  p_expected_statuses text[],
  p_target_status text,
  p_require_expired boolean,
  p_failure_code text default null,
  p_failure_detail text default null
)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  plan_row public.upload_plans%rowtype;
  target_plan_status public.upload_plan_status;
  target_file_status public.upload_plan_file_status;
  target_scene_status public.scene_status;
begin
  if p_owner_id is null or p_upload_plan_id is null then
    raise exception 'owner_id and upload_plan_id are required';
  end if;
  if coalesce(array_length(p_expected_statuses, 1), 0) = 0 then
    raise exception 'expected upload plan statuses are required';
  end if;
  if p_target_status not in ('aborted', 'expired', 'failed') then
    raise exception 'unsupported terminal upload plan status: %', p_target_status;
  end if;

  select * into plan_row
  from public.upload_plans
  where id = p_upload_plan_id and owner_id = p_owner_id
  for update;

  if not found then
    return null;
  end if;
  if not (plan_row.status::text = any(p_expected_statuses)) then
    return null;
  end if;
  if p_require_expired and plan_row.expires_at > now() then
    return null;
  end if;

  target_plan_status := p_target_status::public.upload_plan_status;
  target_file_status := p_target_status::public.upload_plan_file_status;
  target_scene_status := case
    when p_target_status = 'failed' then 'failed'::public.scene_status
    else 'draft'::public.scene_status
  end;

  update public.upload_plans
  set
    status = target_plan_status,
    aborted_at = case when target_plan_status = 'aborted'::public.upload_plan_status then now() else aborted_at end,
    failure_code = case when target_plan_status = 'failed'::public.upload_plan_status then nullif(btrim(p_failure_code), '') else null end,
    failure_detail = case when target_plan_status = 'failed'::public.upload_plan_status then nullif(left(btrim(p_failure_detail), 500), '') else null end
  where id = plan_row.id;

  update public.upload_plan_files
  set
    status = target_file_status,
    failure_code = case when target_file_status = 'failed'::public.upload_plan_file_status then nullif(btrim(p_failure_code), '') else null end,
    failure_detail = case when target_file_status = 'failed'::public.upload_plan_file_status then nullif(left(btrim(p_failure_detail), 500), '') else null end
  where upload_plan_id = plan_row.id
    and status in (
      'planned'::public.upload_plan_file_status,
      'uploading'::public.upload_plan_file_status,
      'uploaded'::public.upload_plan_file_status
    );

  update public.scenes
  set
    status = target_scene_status,
    failure_code = case when target_scene_status = 'failed'::public.scene_status then nullif(btrim(p_failure_code), '') else null end,
    failure_detail = case when target_scene_status = 'failed'::public.scene_status then nullif(left(btrim(p_failure_detail), 500), '') else null end
  where id = plan_row.scene_id
    and project_id = plan_row.project_id
    and owner_id = plan_row.owner_id
    and status = 'uploading'::public.scene_status;

  return jsonb_build_object(
    'id', plan_row.id,
    'status', target_plan_status,
    'scene_id', plan_row.scene_id,
    'project_id', plan_row.project_id
  );
end;
$$;

revoke all on function public.transition_upload_plan_terminal(uuid, uuid, text[], text, boolean, text, text)
from public, anon, authenticated;
grant execute on function public.transition_upload_plan_terminal(uuid, uuid, text[], text, boolean, text, text)
to service_role;

-- Scene/project deletion must not cascade away an active upload plan while a
-- request is completing its multipart object outside PostgreSQL. Lock the
-- parent scope, reject active plans/jobs, and only then allow the cascade.
create or replace function public.delete_scene_if_idle(
  p_owner_id uuid,
  p_scene_id uuid
)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  scene_row public.scenes%rowtype;
begin
  select * into scene_row
  from public.scenes
  where id = p_scene_id and owner_id = p_owner_id
  for update;
  if not found then
    return null;
  end if;
  if exists (
    select 1 from public.upload_plans
    where scene_id = scene_row.id and owner_id = p_owner_id
      and status in (
        'initiated'::public.upload_plan_status,
        'uploading'::public.upload_plan_status,
        'completing'::public.upload_plan_status
      )
  ) then
    return jsonb_build_object('deleted', false, 'reason', 'upload_in_progress');
  end if;
  -- Terminal plans may still own an object whose best-effort cleanup is
  -- retried on the next upload. Preserve those keys until M3 owns durable
  -- storage deletion rather than cascading away their only metadata.
  if exists (
    select 1 from public.upload_plans
    where scene_id = scene_row.id and owner_id = p_owner_id
  ) then
    return jsonb_build_object('deleted', false, 'reason', 'upload_cleanup_pending');
  end if;
  if exists (
    select 1 from public.processing_jobs
    where scene_id = scene_row.id and owner_id = p_owner_id
      and status in ('queued'::public.processing_job_status, 'running'::public.processing_job_status)
  ) then
    return jsonb_build_object('deleted', false, 'reason', 'job_in_progress');
  end if;
  if exists (
    select 1 from public.scene_artifacts
    where scene_id = scene_row.id and owner_id = p_owner_id
      and status <> 'deleted'::public.artifact_status
  ) then
    return jsonb_build_object('deleted', false, 'reason', 'artifacts_require_cleanup');
  end if;
  delete from public.scenes where id = scene_row.id and owner_id = p_owner_id;
  return jsonb_build_object('deleted', true);
end;
$$;

create or replace function public.delete_project_if_idle(
  p_owner_id uuid,
  p_project_id uuid
)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  project_row public.projects%rowtype;
begin
  select * into project_row
  from public.projects
  where id = p_project_id and owner_id = p_owner_id
  for update;
  if not found then
    return null;
  end if;
  -- Serialize against plan creation/finalization, both of which are scoped
  -- through a project scene. The parent project lock alone is insufficient.
  perform 1
  from public.scenes
  where project_id = project_row.id and owner_id = p_owner_id
  for update;
  if exists (
    select 1 from public.upload_plans
    where project_id = project_row.id and owner_id = p_owner_id
      and status in (
        'initiated'::public.upload_plan_status,
        'uploading'::public.upload_plan_status,
        'completing'::public.upload_plan_status
      )
  ) then
    return jsonb_build_object('deleted', false, 'reason', 'upload_in_progress');
  end if;
  if exists (
    select 1 from public.upload_plans
    where project_id = project_row.id and owner_id = p_owner_id
  ) then
    return jsonb_build_object('deleted', false, 'reason', 'upload_cleanup_pending');
  end if;
  if exists (
    select 1 from public.processing_jobs
    where project_id = project_row.id and owner_id = p_owner_id
      and status in ('queued'::public.processing_job_status, 'running'::public.processing_job_status)
  ) then
    return jsonb_build_object('deleted', false, 'reason', 'job_in_progress');
  end if;
  if exists (
    select 1 from public.scene_artifacts
    where project_id = project_row.id and owner_id = p_owner_id
      and status <> 'deleted'::public.artifact_status
  ) then
    return jsonb_build_object('deleted', false, 'reason', 'artifacts_require_cleanup');
  end if;
  delete from public.projects where id = project_row.id and owner_id = p_owner_id;
  return jsonb_build_object('deleted', true);
end;
$$;

revoke all on function public.delete_scene_if_idle(uuid, uuid)
from public, anon, authenticated;
revoke all on function public.delete_project_if_idle(uuid, uuid)
from public, anon, authenticated;
grant execute on function public.delete_scene_if_idle(uuid, uuid)
to service_role;
grant execute on function public.delete_project_if_idle(uuid, uuid)
to service_role;

-- A dispatch retry budget failure has to move its outbox row, queued job, and
-- queued scene in one database transaction. Keeping these updates together
-- prevents a process crash from leaving a job failed while its scene remains
-- queued (or vice versa), which would block a safe retry indefinitely.
create or replace function public.fail_exhausted_job_dispatch(
  p_owner_id uuid,
  p_dispatch_id uuid
)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  dispatch_row public.processing_job_dispatches%rowtype;
  job_row public.processing_jobs%rowtype;
  scene_row public.scenes%rowtype;
  transition_queued_pair boolean := false;
  repair_failed_scene boolean := false;
  failure_code constant text := 'dispatch_publish_exhausted';
  failure_detail constant text := 'The processing job could not be queued after repeated Redis publication attempts.';
begin
  if p_owner_id is null or p_dispatch_id is null then
    raise exception 'owner_id and dispatch_id are required';
  end if;

  select * into dispatch_row
  from public.processing_job_dispatches
  where id = p_dispatch_id and owner_id = p_owner_id
  for update;
  if not found then
    return null;
  end if;

  -- Do not mutate the outbox until the job and scene have both been locked
  -- and proven to be a compatible queued pair. A Redis XADD can succeed just
  -- before its HTTP acknowledgement is lost; in that case a worker may have
  -- legitimately moved the job to running while this outbox row still looks
  -- retryable. That work must win over an exhaustion transition.
  if dispatch_row.status in (
    'pending'::public.processing_job_dispatch_status,
    'retry_scheduled'::public.processing_job_dispatch_status
  ) then
    if dispatch_row.attempt_count < dispatch_row.max_attempts then
      return null;
    end if;
  elsif dispatch_row.status <> 'failed'::public.processing_job_dispatch_status then
    -- A current publisher owns this row, or it was already published. Never
    -- turn an in-flight/processed job into a dispatch-exhaustion failure.
    return null;
  end if;

  select * into job_row
  from public.processing_jobs
  where id = dispatch_row.processing_job_id
    and owner_id = dispatch_row.owner_id
    and project_id = dispatch_row.project_id
    and scene_id = dispatch_row.scene_id
  for update;
  if not found then
    return null;
  end if;

  select * into scene_row
  from public.scenes
  where id = dispatch_row.scene_id
    and project_id = dispatch_row.project_id
    and owner_id = p_owner_id
  for update;
  if not found then
    return null;
  end if;

  if job_row.status = 'queued'::public.processing_job_status
    and scene_row.status = 'queued'::public.scene_status then
    transition_queued_pair := true;
  elsif dispatch_row.status = 'failed'::public.processing_job_dispatch_status
    and job_row.status = 'failed'::public.processing_job_status
    and job_row.error_code = failure_code
    and scene_row.status = 'queued'::public.scene_status then
    -- Repair a legacy partial transition, but only for the exact failure we
    -- own. New calls change all three rows together below.
    repair_failed_scene := true;
  elsif dispatch_row.status = 'failed'::public.processing_job_dispatch_status
    and job_row.status = 'failed'::public.processing_job_status
    and job_row.error_code = failure_code
    and scene_row.status = 'failed'::public.scene_status then
    return jsonb_build_object(
      'id', dispatch_row.id,
      'processing_job_id', dispatch_row.processing_job_id,
      'scene_id', dispatch_row.scene_id,
      'status', 'failed'
    );
  else
    return null;
  end if;

  if dispatch_row.status in (
    'pending'::public.processing_job_dispatch_status,
    'retry_scheduled'::public.processing_job_dispatch_status
  ) then
    update public.processing_job_dispatches
    set
      status = 'failed'::public.processing_job_dispatch_status,
      last_error = 'Maximum Redis publication attempts were exhausted.',
      locked_at = null,
      locked_by = null
    where id = dispatch_row.id and owner_id = p_owner_id;
  end if;

  if transition_queued_pair then
    update public.processing_jobs
    set
      status = 'failed'::public.processing_job_status,
      error_code = failure_code,
      error_detail = failure_detail,
      finished_at = now()
    where id = job_row.id
      and owner_id = p_owner_id
      and status = 'queued'::public.processing_job_status;
  end if;

  if transition_queued_pair or repair_failed_scene then
    update public.scenes
    set
      status = 'failed'::public.scene_status,
      failure_code = failure_code,
      failure_detail = failure_detail
    where id = dispatch_row.scene_id
      and project_id = dispatch_row.project_id
      and owner_id = p_owner_id
      and status = 'queued'::public.scene_status;
  end if;

  return jsonb_build_object(
    'id', dispatch_row.id,
    'processing_job_id', dispatch_row.processing_job_id,
    'scene_id', dispatch_row.scene_id,
    'status', 'failed'
  );
end;
$$;

revoke all on function public.fail_exhausted_job_dispatch(uuid, uuid)
from public, anon, authenticated;
grant execute on function public.fail_exhausted_job_dispatch(uuid, uuid)
to service_role;

-- A side-effect-free readiness probe lets the API distinguish an M1-only
-- database from one with every M2 upload table and the finalization RPC.
create or replace function public.m2_upload_schema_ready()
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select
    to_regclass('public.upload_plans') is not null
    and to_regclass('public.upload_plan_files') is not null
    and to_regclass('public.processing_job_dispatches') is not null
    and to_regprocedure('public.finalize_upload_plan(uuid,uuid,jsonb,jsonb)') is not null
    and to_regprocedure('public.transition_upload_plan_terminal(uuid,uuid,text[],text,boolean,text,text)') is not null
    and to_regprocedure('public.delete_scene_if_idle(uuid,uuid)') is not null
    and to_regprocedure('public.delete_project_if_idle(uuid,uuid)') is not null
    and to_regprocedure('public.fail_exhausted_job_dispatch(uuid,uuid)') is not null;
$$;

revoke all on function public.m2_upload_schema_ready()
from public, anon, authenticated;
grant execute on function public.m2_upload_schema_ready()
to service_role;
