-- M2 recovery follow-on: durable browser initiate idempotency and reload-safe
-- plan lookup.  This deliberately leaves historical upload plans nullable so
-- it can be applied safely after M2 has already accepted uploads.

alter table public.upload_plans
  add column if not exists client_request_id uuid;

alter table public.upload_plans
  add column if not exists request_fingerprint text;

-- New API-created rows always have both values.  NOT VALID avoids scanning
-- existing M2 history while PostgreSQL still enforces the invariant for every
-- new or changed row after this migration.
do $$
begin
  if not exists (
    select 1
    from pg_constraint
    where conname = 'upload_plans_client_request_fingerprint_ck'
      and conrelid = 'public.upload_plans'::regclass
  ) then
    alter table public.upload_plans
      add constraint upload_plans_client_request_fingerprint_ck
      check (
        (client_request_id is null and request_fingerprint is null)
        or (
          client_request_id is not null
          and request_fingerprint ~ '^[0-9a-f]{64}$'
        )
      ) not valid;
  end if;
end
$$;

-- A key is scoped to its authenticated owner.  The partial predicate retains
-- compatibility with M2 plans created before browser request IDs existed.
create unique index if not exists upload_plans_owner_client_request_id_key
  on public.upload_plans (owner_id, client_request_id)
  where client_request_id is not null;

comment on column public.upload_plans.client_request_id is
  'Browser-generated UUID identifying one logical POST /uploads/initiate action; unique per owner when present.';
comment on column public.upload_plans.request_fingerprint is
  'Lowercase SHA-256 hex digest of the canonical initiate request payload, excluding client_request_id.';

-- Keep /readyz from reporting M2 as ready when an API deployment expects the
-- recovery columns but this follow-on migration has not yet been applied.
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
