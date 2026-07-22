-- M3 tasks have an independent retry budget.  Older upload-created jobs
-- retain a max_attempts value of 3, whereas M3 tasks default to 5.  Keep the
-- parent job's summary attempt inside its legacy check constraint; the task
-- attempt remains the authoritative per-stage retry count.

create or replace function public.m3_bound_processing_job_attempt()
returns trigger
language plpgsql
set search_path = public, pg_temp
as $$
begin
  if new.attempt > new.max_attempts then
    new.attempt := new.max_attempts;
  end if;
  return new;
end;
$$;

drop trigger if exists m3_bound_processing_job_attempt on public.processing_jobs;
create trigger m3_bound_processing_job_attempt
before insert or update of attempt, max_attempts on public.processing_jobs
for each row execute function public.m3_bound_processing_job_attempt();
