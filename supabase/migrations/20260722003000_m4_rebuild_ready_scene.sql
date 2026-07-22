-- Permit a user-owned, completed scene to be rebuilt from its retained source
-- artifacts. This is intended for a corrected processing pipeline: it creates
-- a new durable job and does not delete the source scene or its original file.

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
  if scene_row.status not in (
    'ready'::public.scene_status,
    'failed'::public.scene_status,
    'cancelled'::public.scene_status
  ) then
    return jsonb_build_object('accepted', false, 'reason', 'not_reprocessable');
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
    0, 0, 'reprocess_queued', jsonb_build_object('message', 'Scene rebuild queued from retained source artifacts.')
  );

  return jsonb_build_object('accepted', true, 'job_id', job_id);
end
$$;

grant execute on function public.m4_request_scene_reprocess(uuid, uuid) to service_role;

comment on function public.m4_request_scene_reprocess(uuid, uuid) is
  'Atomically creates one rebuild job from retained source artifacts for a ready, failed, or cancelled scene.';
