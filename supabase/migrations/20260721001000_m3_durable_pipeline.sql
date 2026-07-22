-- M3: durable, restart-safe SAR processing tasks and cleanup.
-- PostgreSQL is the source of truth. Redis Streams only wake workers and can
-- therefore be replayed without duplicating scene work.

do $$
begin
  if not exists (select 1 from pg_type where typname = 'processing_job_kind' and typnamespace = 'public'::regnamespace) then
    create type public.processing_job_kind as enum ('process_scene', 'cleanup_scene');
  end if;
  if not exists (select 1 from pg_type where typname = 'processing_task_status' and typnamespace = 'public'::regnamespace) then
    create type public.processing_task_status as enum (
      'queued', 'leased', 'retry_scheduled', 'succeeded', 'failed', 'cancelled'
    );
  end if;
  if not exists (select 1 from pg_type where typname = 'processing_execution_class' and typnamespace = 'public'::regnamespace) then
    create type public.processing_execution_class as enum ('cpu', 'gpu');
  end if;
  if not exists (select 1 from pg_type where typname = 'processing_task_dispatch_status' and typnamespace = 'public'::regnamespace) then
    create type public.processing_task_dispatch_status as enum (
      'pending', 'publishing', 'published', 'retry_scheduled', 'failed', 'cancelled'
    );
  end if;
end
$$;

alter table public.processing_jobs
  add column if not exists kind public.processing_job_kind not null default 'process_scene',
  add column if not exists cancel_requested_at timestamptz,
  add column if not exists cancel_requested_by uuid references auth.users (id) on delete set null,
  add column if not exists lease_generation integer not null default 0,
  add column if not exists retry_after timestamptz;

-- M2's running/succeeded values remain readable for historic jobs, but M3
-- workers only write queued/validating/processing/ready/failed/cancelled.
update public.processing_jobs
set status = case status::text
  when 'running' then 'processing'::public.processing_job_status
  when 'succeeded' then 'ready'::public.processing_job_status
  else status
end
where status::text in ('running', 'succeeded');

alter table public.scene_artifacts
  add column if not exists logical_key text,
  add column if not exists producer_job_id uuid references public.processing_jobs (id) on delete set null;

create unique index if not exists scene_artifacts_scene_logical_key_unique
  on public.scene_artifacts (scene_id, logical_key)
  where logical_key is not null;

alter table public.patches
  add column if not exists patch_key text,
  add column if not exists embedding_artifact_id uuid;

create unique index if not exists patches_scene_patch_key_unique
  on public.patches (scene_id, patch_key)
  where patch_key is not null;

create table if not exists public.processing_job_tasks (
  id uuid primary key default gen_random_uuid(),
  processing_job_id uuid not null,
  owner_id uuid not null,
  project_id uuid not null,
  scene_id uuid not null,
  stage public.processing_job_stage not null,
  execution_class public.processing_execution_class not null,
  status public.processing_task_status not null default 'queued',
  payload jsonb not null default '{}'::jsonb,
  result jsonb not null default '{}'::jsonb,
  attempt integer not null default 0,
  max_attempts integer not null default 5,
  available_at timestamptz not null default now(),
  locked_at timestamptz,
  locked_by text,
  started_at timestamptz,
  finished_at timestamptz,
  error_code text,
  error_detail text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint processing_job_tasks_scope_fkey foreign key (processing_job_id, scene_id, project_id, owner_id)
    references public.processing_jobs (id, scene_id, project_id, owner_id) on delete cascade,
  constraint processing_job_tasks_job_stage_key unique (processing_job_id, stage),
  constraint processing_job_tasks_payload_object_ck check (jsonb_typeof(payload) = 'object'),
  constraint processing_job_tasks_result_object_ck check (jsonb_typeof(result) = 'object'),
  constraint processing_job_tasks_attempt_ck check (attempt >= 0 and max_attempts >= 1 and attempt <= max_attempts),
  constraint processing_job_tasks_progress_time_ck check (finished_at is null or started_at is null or finished_at >= started_at),
  constraint processing_job_tasks_lock_ck check (
    locked_by is null or (char_length(locked_by) between 1 and 255 and locked_by !~ '[[:cntrl:]]')
  )
);

create table if not exists public.processing_task_dispatches (
  id uuid primary key default gen_random_uuid(),
  task_id uuid not null unique references public.processing_job_tasks (id) on delete cascade,
  processing_job_id uuid not null,
  owner_id uuid not null,
  project_id uuid not null,
  scene_id uuid not null,
  execution_class public.processing_execution_class not null,
  status public.processing_task_dispatch_status not null default 'pending',
  payload jsonb not null default '{}'::jsonb,
  attempt_count integer not null default 0,
  max_attempts integer not null default 20,
  available_at timestamptz not null default now(),
  locked_at timestamptz,
  locked_by text,
  published_at timestamptz,
  last_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint processing_task_dispatches_scope_fkey foreign key (processing_job_id, scene_id, project_id, owner_id)
    references public.processing_jobs (id, scene_id, project_id, owner_id) on delete cascade,
  constraint processing_task_dispatches_payload_object_ck check (jsonb_typeof(payload) = 'object'),
  constraint processing_task_dispatches_attempt_ck check (
    attempt_count >= 0 and max_attempts >= 1 and attempt_count <= max_attempts
  ),
  constraint processing_task_dispatches_lock_ck check (
    locked_by is null or (char_length(locked_by) between 1 and 255 and locked_by !~ '[[:cntrl:]]')
  )
);

create table if not exists public.processing_job_events (
  id bigint generated always as identity primary key,
  processing_job_id uuid not null references public.processing_jobs (id) on delete cascade,
  task_id uuid references public.processing_job_tasks (id) on delete set null,
  owner_id uuid not null,
  project_id uuid not null,
  scene_id uuid not null,
  status public.processing_job_status not null,
  stage public.processing_job_stage not null,
  progress smallint not null,
  attempt integer not null,
  event_type text not null,
  error_code text,
  detail jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  constraint processing_job_events_scope_fkey foreign key (processing_job_id, scene_id, project_id, owner_id)
    references public.processing_jobs (id, scene_id, project_id, owner_id) on delete cascade,
  constraint processing_job_events_progress_ck check (progress between 0 and 100),
  constraint processing_job_events_detail_object_ck check (jsonb_typeof(detail) = 'object'),
  constraint processing_job_events_event_type_ck check (char_length(btrim(event_type)) between 1 and 80)
);

create index if not exists processing_job_tasks_ready_idx
  on public.processing_job_tasks (available_at, created_at)
  where status in ('queued'::public.processing_task_status, 'retry_scheduled'::public.processing_task_status);
create index if not exists processing_job_tasks_job_idx
  on public.processing_job_tasks (processing_job_id, created_at);
create index if not exists processing_task_dispatches_ready_idx
  on public.processing_task_dispatches (available_at, created_at)
  where status in ('pending'::public.processing_task_dispatch_status, 'retry_scheduled'::public.processing_task_dispatch_status);
create index if not exists processing_job_events_job_idx
  on public.processing_job_events (processing_job_id, created_at);

drop trigger if exists processing_job_tasks_set_updated_at on public.processing_job_tasks;
create trigger processing_job_tasks_set_updated_at before update on public.processing_job_tasks
for each row execute function public.set_updated_at();
drop trigger if exists processing_task_dispatches_set_updated_at on public.processing_task_dispatches;
create trigger processing_task_dispatches_set_updated_at before update on public.processing_task_dispatches
for each row execute function public.set_updated_at();

create or replace function public.m3_enqueue_task(
  p_processing_job_id uuid,
  p_stage public.processing_job_stage,
  p_execution_class public.processing_execution_class,
  p_payload jsonb default '{}'::jsonb,
  p_max_attempts integer default 5
)
returns uuid
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  job_row public.processing_jobs%rowtype;
  task_id uuid;
begin
  if jsonb_typeof(p_payload) is distinct from 'object' then
    raise exception 'M3 task payload must be a JSON object';
  end if;
  if p_max_attempts < 1 or p_max_attempts > 20 then
    raise exception 'M3 task max attempts must be between 1 and 20';
  end if;

  select * into job_row
  from public.processing_jobs
  where id = p_processing_job_id
  for update;
  if not found then
    raise exception 'Processing job % does not exist', p_processing_job_id;
  end if;

  insert into public.processing_job_tasks (
    processing_job_id, owner_id, project_id, scene_id, stage, execution_class,
    payload, max_attempts
  ) values (
    job_row.id, job_row.owner_id, job_row.project_id, job_row.scene_id,
    p_stage, p_execution_class, p_payload, p_max_attempts
  )
  on conflict (processing_job_id, stage) do update
    set payload = public.processing_job_tasks.payload
  returning id into task_id;

  insert into public.processing_task_dispatches (
    task_id, processing_job_id, owner_id, project_id, scene_id,
    execution_class, payload
  ) values (
    task_id, job_row.id, job_row.owner_id, job_row.project_id, job_row.scene_id,
    p_execution_class,
    jsonb_build_object('task_id', task_id, 'job_id', job_row.id, 'schema', 'raikou.m3.task.v1')
  )
  on conflict (task_id) do nothing;

  return task_id;
end
$$;

create or replace function public.m3_request_job_cancellation(
  p_owner_id uuid,
  p_job_id uuid
)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  job_row public.processing_jobs%rowtype;
begin
  select * into job_row
  from public.processing_jobs
  where id = p_job_id and owner_id = p_owner_id
  for update;
  if not found then
    return null;
  end if;
  if job_row.status in ('ready'::public.processing_job_status, 'failed'::public.processing_job_status, 'cancelled'::public.processing_job_status) then
    return jsonb_build_object('job_id', job_row.id, 'accepted', false, 'status', job_row.status);
  end if;

  update public.processing_jobs
  set cancel_requested_at = coalesce(cancel_requested_at, now()),
      cancel_requested_by = coalesce(cancel_requested_by, p_owner_id)
  where id = job_row.id;

  insert into public.processing_job_events (
    processing_job_id, owner_id, project_id, scene_id, status, stage, progress,
    attempt, event_type, detail
  ) values (
    job_row.id, job_row.owner_id, job_row.project_id, job_row.scene_id,
    job_row.status, job_row.stage, job_row.progress, job_row.attempt,
    'cancellation_requested', '{}'::jsonb
  );

  return jsonb_build_object('job_id', job_row.id, 'accepted', true, 'status', job_row.status);
end
$$;

create or replace function public.m3_request_scene_cleanup(
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
  cleanup_job_id uuid;
begin
  select * into scene_row
  from public.scenes
  where id = p_scene_id and owner_id = p_owner_id
  for update;
  if not found then
    return null;
  end if;

  update public.scenes set status = 'deleting'::public.scene_status where id = scene_row.id;
  update public.processing_jobs
  set cancel_requested_at = coalesce(cancel_requested_at, now()),
      cancel_requested_by = coalesce(cancel_requested_by, p_owner_id)
  where scene_id = scene_row.id
    and owner_id = p_owner_id
    and kind = 'process_scene'::public.processing_job_kind
    and status not in ('ready'::public.processing_job_status, 'failed'::public.processing_job_status, 'cancelled'::public.processing_job_status);

  select id into cleanup_job_id
  from public.processing_jobs
  where scene_id = scene_row.id and owner_id = p_owner_id
    and kind = 'cleanup_scene'::public.processing_job_kind
    and status not in ('ready'::public.processing_job_status, 'failed'::public.processing_job_status, 'cancelled'::public.processing_job_status)
  order by created_at desc
  limit 1;

  if cleanup_job_id is null then
    insert into public.processing_jobs (
      owner_id, project_id, scene_id, kind, stage, status, progress, max_attempts
    ) values (
      scene_row.owner_id, scene_row.project_id, scene_row.id,
      'cleanup_scene'::public.processing_job_kind,
      'cleanup'::public.processing_job_stage,
      'queued'::public.processing_job_status, 0, 10
    ) returning id into cleanup_job_id;
    perform public.m3_enqueue_task(
      cleanup_job_id,
      'cleanup'::public.processing_job_stage,
      'cpu'::public.processing_execution_class,
      jsonb_build_object('delete_scene', true),
      10
    );
  end if;

  return jsonb_build_object('scene_id', scene_row.id, 'cleanup_job_id', cleanup_job_id);
end
$$;

-- The worker is the only M3 writer. Reject invalid forward/backward job
-- transitions even if a future worker implementation is buggy.
create or replace function public.m3_validate_job_transition()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  if new.status = old.status then
    return new;
  end if;
  if old.status in ('ready'::public.processing_job_status, 'failed'::public.processing_job_status, 'cancelled'::public.processing_job_status) then
    raise exception 'M3 terminal processing jobs cannot transition';
  end if;
  if old.status = 'queued'::public.processing_job_status
    and new.status not in ('validating'::public.processing_job_status, 'failed'::public.processing_job_status, 'cancelled'::public.processing_job_status)
    and not (new.kind = 'cleanup_scene'::public.processing_job_kind and new.status = 'processing'::public.processing_job_status) then
    raise exception 'Invalid M3 transition from queued to %', new.status;
  end if;
  if old.status = 'validating'::public.processing_job_status
    and new.status not in ('processing'::public.processing_job_status, 'failed'::public.processing_job_status, 'cancelled'::public.processing_job_status) then
    raise exception 'Invalid M3 transition from validating to %', new.status;
  end if;
  if old.status = 'processing'::public.processing_job_status
    and new.status not in ('ready'::public.processing_job_status, 'failed'::public.processing_job_status, 'cancelled'::public.processing_job_status) then
    raise exception 'Invalid M3 transition from processing to %', new.status;
  end if;
  return new;
end
$$;

drop trigger if exists processing_jobs_m3_validate_transition on public.processing_jobs;
create trigger processing_jobs_m3_validate_transition
before update of status on public.processing_jobs
for each row execute function public.m3_validate_job_transition();

alter table public.processing_job_tasks enable row level security;
alter table public.processing_task_dispatches enable row level security;
alter table public.processing_job_events enable row level security;

drop policy if exists processing_job_tasks_owner_access on public.processing_job_tasks;
create policy processing_job_tasks_owner_access on public.processing_job_tasks
for all to authenticated
using (owner_id = (select auth.uid()))
with check (owner_id = (select auth.uid()));
drop policy if exists processing_task_dispatches_owner_access on public.processing_task_dispatches;
create policy processing_task_dispatches_owner_access on public.processing_task_dispatches
for all to authenticated
using (owner_id = (select auth.uid()))
with check (owner_id = (select auth.uid()));
drop policy if exists processing_job_events_owner_access on public.processing_job_events;
create policy processing_job_events_owner_access on public.processing_job_events
for select to authenticated using (owner_id = (select auth.uid()));

revoke all on table public.processing_job_tasks, public.processing_task_dispatches, public.processing_job_events
from public, anon, authenticated;
grant all privileges on table public.processing_job_tasks, public.processing_task_dispatches, public.processing_job_events
to service_role;
grant execute on function public.m3_enqueue_task(uuid, public.processing_job_stage, public.processing_execution_class, jsonb, integer) to service_role;
grant execute on function public.m3_request_job_cancellation(uuid, uuid) to service_role;
grant execute on function public.m3_request_scene_cleanup(uuid, uuid) to service_role;

create or replace function public.m3_pipeline_schema_ready()
returns boolean
language sql
stable
set search_path = public
as $$
  select
    to_regclass('public.processing_job_tasks') is not null
    and to_regclass('public.processing_task_dispatches') is not null
    and to_regclass('public.processing_job_events') is not null
    and to_regprocedure('public.m3_enqueue_task(uuid,public.processing_job_stage,public.processing_execution_class,jsonb,integer)') is not null
    and to_regprocedure('public.m3_request_job_cancellation(uuid,uuid)') is not null
    and to_regprocedure('public.m3_request_scene_cleanup(uuid,uuid)') is not null
$$;

grant execute on function public.m3_pipeline_schema_ready() to anon, authenticated, service_role;

comment on table public.processing_job_tasks is
  'M3 durable stage tasks. A Redis message is only a wake-up for one of these PostgreSQL rows.';
comment on table public.processing_task_dispatches is
  'Transactional outbox for CPU/GPU task streams. Payload contains no credentials, signed URLs, or user content.';
