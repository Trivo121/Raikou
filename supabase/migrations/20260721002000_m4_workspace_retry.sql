-- M4: durable scene retries for the real workspace and a correct active-job
-- invariant for the M3 lifecycle values.

do $$
begin
  if exists (
    select 1
    from public.processing_jobs
    where kind = 'process_scene'::public.processing_job_kind
      and status in (
        'queued'::public.processing_job_status,
        'running'::public.processing_job_status,
        'validating'::public.processing_job_status,
        'processing'::public.processing_job_status
      )
    group by scene_id
    having count(*) > 1
  ) then
    raise exception 'Cannot apply M4 active-job invariant: duplicate active process jobs exist';
  end if;
end
$$;

drop index if exists public.processing_jobs_one_active_per_scene_idx;
create unique index processing_jobs_one_active_process_job_per_scene_idx
  on public.processing_jobs (scene_id)
  where kind = 'process_scene'::public.processing_job_kind
    and status in (
      'queued'::public.processing_job_status,
      'running'::public.processing_job_status,
      'validating'::public.processing_job_status,
      'processing'::public.processing_job_status
    );

create or replace function public.m4_request_scene_reprocess(
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
  job_id uuid;
begin
  select * into scene_row
  from public.scenes
  where id = p_scene_id and owner_id = p_owner_id
  for update;

  if not found then
    return null;
  end if;
  if scene_row.status = 'deleting'::public.scene_status then
    return jsonb_build_object('accepted', false, 'reason', 'deleting');
  end if;
  if scene_row.status not in ('failed'::public.scene_status, 'cancelled'::public.scene_status) then
    return jsonb_build_object('accepted', false, 'reason', 'not_retryable');
  end if;
  if exists (
    select 1
    from public.processing_jobs
    where scene_id = scene_row.id
      and owner_id = p_owner_id
      and kind = 'process_scene'::public.processing_job_kind
      and status in (
        'queued'::public.processing_job_status,
        'running'::public.processing_job_status,
        'validating'::public.processing_job_status,
        'processing'::public.processing_job_status
      )
  ) then
    return jsonb_build_object('accepted', false, 'reason', 'active_job');
  end if;
  if not exists (
    select 1
    from public.scene_artifacts
    where scene_id = scene_row.id
      and project_id = scene_row.project_id
      and owner_id = p_owner_id
      and kind in ('source_archive'::public.artifact_kind, 'source_raster'::public.artifact_kind)
      and status = 'available'::public.artifact_status
  ) then
    return jsonb_build_object('accepted', false, 'reason', 'no_source');
  end if;

  update public.scenes
  set status = 'queued'::public.scene_status,
      failure_code = null,
      failure_detail = null
  where id = scene_row.id and owner_id = p_owner_id;

  insert into public.processing_jobs (
    owner_id, project_id, scene_id, kind, stage, status, progress, max_attempts
  ) values (
    scene_row.owner_id, scene_row.project_id, scene_row.id,
    'process_scene'::public.processing_job_kind,
    'validate_upload'::public.processing_job_stage,
    'queued'::public.processing_job_status,
    0,
    5
  ) returning id into job_id;

  perform public.m3_enqueue_task(
    job_id,
    'validate_upload'::public.processing_job_stage,
    'cpu'::public.processing_execution_class,
    jsonb_build_object('requested_by', p_owner_id, 'reprocess', true),
    5
  );

  insert into public.processing_job_events (
    processing_job_id, owner_id, project_id, scene_id, status, stage,
    progress, attempt, event_type, detail
  ) values (
    job_id, scene_row.owner_id, scene_row.project_id, scene_row.id,
    'queued'::public.processing_job_status,
    'validate_upload'::public.processing_job_stage,
    0, 0, 'reprocess_queued', jsonb_build_object('message', 'Processing retry queued.')
  );

  return jsonb_build_object('accepted', true, 'job_id', job_id);
end
$$;

grant execute on function public.m4_request_scene_reprocess(uuid, uuid) to service_role;

create or replace function public.m4_workspace_schema_ready()
returns boolean
language sql
stable
set search_path = public
as $$
  select
    to_regprocedure('public.m4_request_scene_reprocess(uuid,uuid)') is not null
    and to_regclass('public.processing_jobs_one_active_process_job_per_scene_idx') is not null
$$;

grant execute on function public.m4_workspace_schema_ready() to anon, authenticated, service_role;

comment on function public.m4_request_scene_reprocess(uuid, uuid) is
  'M4 atomically creates one retry process job from retained source artifacts.';
