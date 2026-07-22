-- M2 recovery follow-on: make direct-upload plan setup visible atomically.
--
-- A plan must never be readable by an idempotent retry until all of its file
-- rows exist and its scene has transitioned to `uploading`. Otherwise a lost
-- response from one of those writes could make cleanup delete a plan that a
-- retry has already adopted.

create or replace function public.create_upload_plan_atomically(
  p_owner_id uuid,
  p_plan jsonb,
  p_files jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  plan_id uuid;
  plan_owner_id uuid;
  plan_project_id uuid;
  plan_scene_id uuid;
  plan_client_request_id uuid;
  plan_expected_file_count smallint;
  plan_expires_at timestamptz;
  plan_fingerprint text;
  scene_row public.scenes%rowtype;
  existing_plan public.upload_plans%rowtype;
  supplied_file_count integer;
  inserted_file_count integer;
  updated_scene_id uuid;
begin
  if p_owner_id is null then
    raise exception 'owner_id is required';
  end if;
  if jsonb_typeof(p_plan) is distinct from 'object' then
    raise exception 'plan must be a JSON object';
  end if;
  if jsonb_typeof(p_files) is distinct from 'array' then
    raise exception 'files must be a JSON array';
  end if;

  plan_id := nullif(btrim(p_plan ->> 'id'), '')::uuid;
  plan_owner_id := nullif(btrim(p_plan ->> 'owner_id'), '')::uuid;
  plan_project_id := nullif(btrim(p_plan ->> 'project_id'), '')::uuid;
  plan_scene_id := nullif(btrim(p_plan ->> 'scene_id'), '')::uuid;
  plan_client_request_id := nullif(btrim(p_plan ->> 'client_request_id'), '')::uuid;
  plan_expected_file_count := nullif(btrim(p_plan ->> 'expected_file_count'), '')::smallint;
  plan_expires_at := nullif(btrim(p_plan ->> 'expires_at'), '')::timestamptz;
  plan_fingerprint := nullif(btrim(p_plan ->> 'request_fingerprint'), '');

  if plan_id is null
    or plan_owner_id is null
    or plan_project_id is null
    or plan_scene_id is null
    or plan_client_request_id is null
    or plan_expected_file_count is null
    or plan_expires_at is null
    or plan_fingerprint is null
  then
    raise exception 'plan is missing a required immutable field';
  end if;
  if plan_owner_id <> p_owner_id then
    raise exception 'plan owner does not match request owner';
  end if;
  if p_plan ->> 'status' <> 'initiated' then
    raise exception 'new upload plan status must be initiated';
  end if;
  if plan_expected_file_count not between 1 and 3 then
    raise exception 'plan expected file count is outside the supported range';
  end if;
  if plan_expires_at <= now() then
    raise exception 'plan expiry must be in the future';
  end if;
  if plan_fingerprint !~ '^[0-9a-f]{64}$' then
    raise exception 'plan request fingerprint is invalid';
  end if;

  select count(*) into supplied_file_count
  from jsonb_array_elements(p_files);
  if supplied_file_count <> plan_expected_file_count then
    raise exception 'plan file count does not match expected file count';
  end if;

  -- Serializing on the scene makes same-scene creates, retries, expiry
  -- reclamation, and completion observe one coherent lifecycle boundary.
  select * into scene_row
  from public.scenes
  where id = plan_scene_id
    and project_id = plan_project_id
    and owner_id = p_owner_id
  for update;
  if not found then
    return jsonb_build_object('outcome', 'scene_not_found');
  end if;

  -- The per-owner unique key is the durable initiate idempotency boundary.
  -- Check it while holding the target scene lock so same-scene retries never
  -- observe a partially initialized plan.
  select * into existing_plan
  from public.upload_plans
  where owner_id = p_owner_id
    and client_request_id = plan_client_request_id
  for update;
  if found then
    return jsonb_build_object(
      'outcome', 'existing',
      'upload_plan_id', existing_plan.id,
      'status', existing_plan.status
    );
  end if;

  if scene_row.status not in (
    'draft'::public.scene_status,
    'failed'::public.scene_status,
    'cancelled'::public.scene_status
  ) then
    return jsonb_build_object('outcome', 'scene_not_uploadable');
  end if;

  if exists (
    select 1
    from public.upload_plans
    where scene_id = plan_scene_id
      and project_id = plan_project_id
      and owner_id = p_owner_id
      and status in (
        'initiated'::public.upload_plan_status,
        'uploading'::public.upload_plan_status,
        'completing'::public.upload_plan_status
      )
  ) then
    return jsonb_build_object('outcome', 'scene_busy');
  end if;

  if exists (
    select 1
    from public.processing_jobs
    where scene_id = plan_scene_id
      and project_id = plan_project_id
      and owner_id = p_owner_id
      and status in (
        'queued'::public.processing_job_status,
        'running'::public.processing_job_status
      )
  ) then
    return jsonb_build_object('outcome', 'active_job');
  end if;

  insert into public.upload_plans (
    id,
    owner_id,
    project_id,
    scene_id,
    status,
    expected_file_count,
    expires_at,
    client_request_id,
    request_fingerprint
  )
  values (
    plan_id,
    p_owner_id,
    plan_project_id,
    plan_scene_id,
    'initiated'::public.upload_plan_status,
    plan_expected_file_count,
    plan_expires_at,
    plan_client_request_id,
    plan_fingerprint
  );

  insert into public.upload_plan_files (
    id,
    upload_plan_id,
    owner_id,
    project_id,
    scene_id,
    file_number,
    file_kind,
    status,
    original_filename,
    content_type,
    storage_bucket,
    storage_key,
    multipart_upload_id,
    multipart_checksum_mode,
    expected_size_bytes,
    expected_checksum_sha256,
    part_size_bytes,
    part_count
  )
  select
    file_row.id,
    plan_id,
    p_owner_id,
    plan_project_id,
    plan_scene_id,
    file_row.file_number,
    file_row.file_kind::public.upload_plan_file_kind,
    'planned'::public.upload_plan_file_status,
    file_row.original_filename,
    file_row.content_type,
    file_row.storage_bucket,
    file_row.storage_key,
    file_row.multipart_upload_id,
    file_row.multipart_checksum_mode::public.multipart_checksum_mode,
    file_row.expected_size_bytes,
    file_row.expected_checksum_sha256,
    file_row.part_size_bytes,
    file_row.part_count
  from jsonb_to_recordset(p_files) as file_row(
    id uuid,
    file_number smallint,
    file_kind text,
    original_filename text,
    content_type text,
    storage_bucket text,
    storage_key text,
    multipart_upload_id text,
    multipart_checksum_mode text,
    expected_size_bytes bigint,
    expected_checksum_sha256 text,
    part_size_bytes bigint,
    part_count integer
  );
  get diagnostics inserted_file_count = row_count;
  if inserted_file_count <> plan_expected_file_count then
    raise exception 'upload plan file insertion was incomplete';
  end if;

  update public.scenes
  set
    status = 'uploading'::public.scene_status,
    failure_code = null,
    failure_detail = null
  where id = plan_scene_id
    and project_id = plan_project_id
    and owner_id = p_owner_id
    and status in (
      'draft'::public.scene_status,
      'failed'::public.scene_status,
      'cancelled'::public.scene_status
    )
  returning id into updated_scene_id;
  if updated_scene_id is null then
    raise exception 'scene state changed while creating upload plan';
  end if;

  return jsonb_build_object(
    'outcome', 'created',
    'upload_plan_id', plan_id,
    'scene_id', plan_scene_id,
    'project_id', plan_project_id
  );
end;
$$;

revoke all on function public.create_upload_plan_atomically(uuid, jsonb, jsonb)
from public, anon, authenticated;
grant execute on function public.create_upload_plan_atomically(uuid, jsonb, jsonb)
to service_role;

-- Require this atomic creation boundary before readiness declares M2 usable.
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
    and to_regclass('public.upload_plans_owner_client_request_id_key') is not null
    and exists (
      select 1
      from pg_attribute
      where attrelid = to_regclass('public.upload_plans')
        and attname = 'client_request_id'
        and not attisdropped
    )
    and exists (
      select 1
      from pg_attribute
      where attrelid = to_regclass('public.upload_plans')
        and attname = 'request_fingerprint'
        and not attisdropped
    )
    and to_regprocedure('public.create_upload_plan_atomically(uuid,jsonb,jsonb)') is not null
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
