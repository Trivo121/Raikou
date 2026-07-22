-- Keep the task UUID in a uniquely named PL/pgSQL variable.  The original
-- function used `task_id` for both this variable and the outbox column,
-- causing PostgreSQL to reject the outbox INSERT as ambiguous.
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
  v_task_id uuid;
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
  returning id into v_task_id;

  insert into public.processing_task_dispatches (
    task_id, processing_job_id, owner_id, project_id, scene_id,
    execution_class, payload
  ) values (
    v_task_id, job_row.id, job_row.owner_id, job_row.project_id, job_row.scene_id,
    p_execution_class,
    jsonb_build_object('task_id', v_task_id, 'job_id', job_row.id, 'schema', 'raikou.m3.task.v1')
  )
  on conflict (task_id) do nothing;

  return v_task_id;
end
$$;
