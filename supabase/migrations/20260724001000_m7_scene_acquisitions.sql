-- M7 part 2: provider-fetched scene sources.
--
-- This is a second way to get bytes into a scene, beside the M2 browser upload.
-- It deliberately does not reuse upload_plans: those rows carry NOT NULL
-- multipart_upload_id/part_size_bytes/part_count plus the part_layout_ck
-- constraint tying count to size, none of which a server-side download has.
-- Reusing that table would mean weakening a live constraint.
--
-- Everything downstream of the fetch is unchanged: the worker writes an
-- ordinary 'source_archive' scene_artifacts row, and validate_upload onward
-- cannot tell how the bytes arrived.

do $$
begin
  if not exists (select 1 from pg_type where typname = 'scene_acquisition_status' and typnamespace = 'public'::regnamespace) then
    create type public.scene_acquisition_status as enum (
      'queued', 'downloading', 'downloaded', 'failed', 'cancelled'
    );
  end if;

  if not exists (select 1 from pg_type where typname = 'scene_acquisition_provider' and typnamespace = 'public'::regnamespace) then
    create type public.scene_acquisition_provider as enum ('copernicus');
  end if;
end
$$;

create table if not exists public.scene_acquisitions (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null default auth.uid(),
  project_id uuid not null,
  scene_id uuid not null,
  provider public.scene_acquisition_provider not null default 'copernicus',
  status public.scene_acquisition_status not null default 'queued',
  -- Provider product identity. Only server-fetched values are ever persisted:
  -- the API re-reads the product from the provider before calling the RPC.
  product_id text not null,
  product_name text not null,
  product_type text not null,
  polarisation_channels text not null,
  sensing_start timestamptz,
  online boolean not null default true,
  expected_size_bytes bigint not null,
  footprint jsonb,
  -- Populated by the worker once the object is durable.
  storage_bucket text,
  storage_key text,
  downloaded_size_bytes bigint,
  checksum_sha256 text,
  artifact_id uuid,
  downloaded_at timestamptz,
  failure_code text,
  failure_detail text,
  client_request_id text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint scene_acquisitions_scene_scope_fkey foreign key (scene_id, project_id, owner_id)
    references public.scenes (id, project_id, owner_id) on delete cascade,
  constraint scene_acquisitions_scope_key unique (id, scene_id, project_id, owner_id),
  constraint scene_acquisitions_artifact_scope_fkey foreign key (artifact_id, scene_id, project_id, owner_id)
    references public.scene_artifacts (id, scene_id, project_id, owner_id)
    on delete set null (artifact_id),
  -- The durable copy of the pipeline contract. stages.py sorts measurement
  -- bands on 'vv'/'vh' and scattering_map.py hardcodes GRDH's 10 m spacing, so
  -- a GRDM, single-pol, or SLC-adjacent product processes to 'ready' with the
  -- scattering block, mechanism map, and land cover silently missing. The
  -- catalogue filter is the first defence; this is the one that survives a
  -- code change. 1SDV is dual-pol VV+VH; 1SSV/1SSH are single-pol.
  constraint scene_acquisitions_product_type_ck check (product_type = 'IW_GRDH_1S'),
  constraint scene_acquisitions_polarisation_ck check (polarisation_channels = 'VV&VH'),
  constraint scene_acquisitions_product_name_ck check (
    product_name ~ '^S1[A-D]_IW_GRDH_1SDV_'
    and char_length(product_name) between 1 and 512
    and product_name !~ '[[:cntrl:]]'
  ),
  constraint scene_acquisitions_product_id_ck check (
    char_length(product_id) between 1 and 128
    and product_id = btrim(product_id)
    and product_id !~ '[[:cntrl:]]'
  ),
  constraint scene_acquisitions_expected_size_ck check (expected_size_bytes > 0),
  constraint scene_acquisitions_downloaded_size_ck check (
    downloaded_size_bytes is null or downloaded_size_bytes > 0
  ),
  constraint scene_acquisitions_footprint_object_ck check (
    footprint is null or jsonb_typeof(footprint) = 'object'
  ),
  constraint scene_acquisitions_storage_bucket_ck check (
    storage_bucket is null
    or (
      char_length(storage_bucket) between 1 and 255
      and storage_bucket = btrim(storage_bucket)
      and storage_bucket !~ '[[:cntrl:]]'
    )
  ),
  -- Copied in shape from upload_plan_files/scene_artifacts so a fetched object
  -- key is held to exactly the same path-safety rules as an uploaded one.
  constraint scene_acquisitions_storage_key_ck check (
    storage_key is null
    or (
      char_length(storage_key) between 1 and 1024
      and storage_key = btrim(storage_key)
      and left(storage_key, 1) <> '/'
      and position(chr(92) in storage_key) = 0
      and position('//' in storage_key) = 0
      and coalesce(array_position(string_to_array(storage_key, '/'), '.'), 0) = 0
      and coalesce(array_position(string_to_array(storage_key, '/'), '..'), 0) = 0
      and storage_key !~ '[[:cntrl:]]'
    )
  ),
  -- scene_artifacts stores SHA-256 as lowercase hex; stay consistent.
  constraint scene_acquisitions_checksum_ck check (
    checksum_sha256 is null or checksum_sha256 ~ '^[0-9A-Fa-f]{64}$'
  ),
  constraint scene_acquisitions_client_request_id_ck check (
    client_request_id is null
    or (
      char_length(client_request_id) between 8 and 128
      and client_request_id = btrim(client_request_id)
      and client_request_id !~ '[[:cntrl:]]'
    )
  ),
  constraint scene_acquisitions_downloaded_fields_ck check (
    status <> 'downloaded'
    or (
      downloaded_at is not null
      and storage_bucket is not null
      and storage_key is not null
      and downloaded_size_bytes is not null
    )
  )
);

-- Retain a direct, scoped link from the job to the acquisition that created
-- it, mirroring processing_jobs.upload_plan_id.
alter table public.processing_jobs
  add column if not exists scene_acquisition_id uuid;

do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conname = 'processing_jobs_scene_acquisition_scope_fkey'
      and conrelid = 'public.processing_jobs'::regclass
  ) then
    alter table public.processing_jobs
      add constraint processing_jobs_scene_acquisition_scope_fkey
      foreign key (scene_acquisition_id, scene_id, project_id, owner_id)
      references public.scene_acquisitions (id, scene_id, project_id, owner_id)
      on delete restrict;
  end if;
end
$$;

create unique index if not exists processing_jobs_scene_acquisition_key
  on public.processing_jobs (scene_acquisition_id)
  where scene_acquisition_id is not null;

-- One open acquisition per scene, mirroring upload_plans_one_open_per_scene_idx.
create unique index if not exists scene_acquisitions_one_open_per_scene_idx
  on public.scene_acquisitions (scene_id)
  where status in (
    'queued'::public.scene_acquisition_status,
    'downloading'::public.scene_acquisition_status
  );

-- Durable idempotency for a retried POST, mirroring
-- upload_plans_owner_client_request_id_key. This is why the browser needs no
-- sessionStorage recovery for this path.
create unique index if not exists scene_acquisitions_owner_client_request_id_key
  on public.scene_acquisitions (owner_id, client_request_id)
  where client_request_id is not null;

create index if not exists scene_acquisitions_owner_project_created_idx
  on public.scene_acquisitions (owner_id, project_id, created_at desc);
create index if not exists scene_acquisitions_scene_created_idx
  on public.scene_acquisitions (scene_id, created_at desc);
create index if not exists scene_acquisitions_product_idx
  on public.scene_acquisitions (provider, product_id);

drop trigger if exists scene_acquisitions_set_updated_at on public.scene_acquisitions;
create trigger scene_acquisitions_set_updated_at
before update on public.scene_acquisitions
for each row execute function public.set_updated_at();

comment on table public.scene_acquisitions is
  'Server-side provider fetch of one scene source product. Never stores provider credentials or bearer tokens.';
comment on column public.scene_acquisitions.footprint is
  'Provider ground footprint of the whole frame. The AOI is a search filter, never a crop: this scene covers the AOI, it is not clipped to it.';

alter table public.scene_acquisitions enable row level security;

drop policy if exists scene_acquisitions_owner_access on public.scene_acquisitions;
create policy scene_acquisitions_owner_access on public.scene_acquisitions
for all to authenticated
using (owner_id = (select auth.uid()))
with check (owner_id = (select auth.uid()));

revoke all on table public.scene_acquisitions from public, anon, authenticated;
grant all privileges on table public.scene_acquisitions to service_role;

grant usage on type
  public.scene_acquisition_status,
  public.scene_acquisition_provider
to service_role;

-- Create the acquisition, its scene transition, its job, and its first task in
-- one transaction. Deliberately creates no artifact -- the worker does that
-- once bytes are durable -- and enqueues its own task rather than writing a
-- processing_job_dispatches row: that outbox is upload-specific (its CHECK
-- restricts message_type and bootstrap_m2_jobs is its only consumer), so this
-- follows m4_request_scene_reprocess instead.
create or replace function public.start_scene_acquisition(
  p_owner_id uuid,
  p_scene_id uuid,
  p_client_request_id text,
  p_product jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  scene_row public.scenes%rowtype;
  existing_row public.scene_acquisitions%rowtype;
  created_row public.scene_acquisitions%rowtype;
  job_id uuid;
  v_client_request_id text;
  v_product_id text;
  v_product_name text;
  v_product_type text;
  v_polarisation text;
  v_sensing_start timestamptz;
  v_online boolean;
  v_expected_size bigint;
  v_footprint jsonb;
begin
  if p_owner_id is null or p_scene_id is null then
    raise exception 'owner_id and scene_id are required';
  end if;
  if jsonb_typeof(p_product) is distinct from 'object' then
    raise exception 'product must be a JSON object';
  end if;

  v_client_request_id := nullif(btrim(coalesce(p_client_request_id, '')), '');

  -- Replay before locking anything: a retried POST whose response was lost on
  -- the network must return the original acquisition, not a second download.
  if v_client_request_id is not null then
    select * into existing_row
    from public.scene_acquisitions
    where owner_id = p_owner_id and client_request_id = v_client_request_id;
    if found then
      return jsonb_build_object(
        'accepted', true,
        'replayed', true,
        'acquisition_id', existing_row.id,
        'scene_id', existing_row.scene_id,
        'project_id', existing_row.project_id,
        'status', existing_row.status,
        'job_id', (
          select id from public.processing_jobs
          where scene_acquisition_id = existing_row.id
          limit 1
        )
      );
    end if;
  end if;

  select * into scene_row
  from public.scenes
  where id = p_scene_id and owner_id = p_owner_id
  for update;
  if not found then
    return null;
  end if;

  -- Same acceptable-status set as the upload path (uploads.py
  -- _UPLOADABLE_SCENE_STATUSES) so both sources agree on what a fresh scene is.
  if scene_row.status not in (
    'draft'::public.scene_status,
    'failed'::public.scene_status,
    'cancelled'::public.scene_status
  ) then
    return jsonb_build_object('accepted', false, 'reason', 'scene_not_acceptable');
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
    return jsonb_build_object('accepted', false, 'reason', 'upload_in_progress');
  end if;

  if exists (
    select 1 from public.processing_jobs
    where scene_id = scene_row.id and owner_id = p_owner_id
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

  if exists (
    select 1 from public.scene_acquisitions
    where scene_id = scene_row.id and owner_id = p_owner_id
      and status in (
        'queued'::public.scene_acquisition_status,
        'downloading'::public.scene_acquisition_status
      )
  ) then
    return jsonb_build_object('accepted', false, 'reason', 'acquisition_in_progress');
  end if;

  -- Two source artifacts on one scene would make the worker's
  -- _materialize_sources pick a nondeterministic archive, and the artifact
  -- unique index does not protect against it: an uploaded source has
  -- logical_key IS NULL while a fetched one has 'source:copernicus-archive:v1'.
  if exists (
    select 1 from public.scene_artifacts
    where scene_id = scene_row.id and owner_id = p_owner_id
      and kind in ('source_archive'::public.artifact_kind, 'source_raster'::public.artifact_kind)
      and status = 'available'::public.artifact_status
  ) then
    return jsonb_build_object('accepted', false, 'reason', 'source_already_present');
  end if;

  v_product_id := nullif(btrim(coalesce(p_product->>'product_id', '')), '');
  v_product_name := nullif(btrim(coalesce(p_product->>'product_name', '')), '');
  v_product_type := nullif(btrim(coalesce(p_product->>'product_type', '')), '');
  v_polarisation := nullif(btrim(coalesce(p_product->>'polarisation_channels', '')), '');
  v_sensing_start := nullif(btrim(coalesce(p_product->>'sensing_start', '')), '')::timestamptz;
  v_online := coalesce((p_product->>'online')::boolean, true);
  v_expected_size := nullif(btrim(coalesce(p_product->>'expected_size_bytes', '')), '')::bigint;
  v_footprint := case
    when jsonb_typeof(p_product->'footprint') = 'object' then p_product->'footprint'
    else null
  end;

  if v_product_id is null or v_product_name is null
    or v_product_type is null or v_polarisation is null or v_expected_size is null then
    raise exception 'product is missing a required field';
  end if;

  -- The API rejects this with a 409 first; this is the durable backstop.
  if not v_online then
    return jsonb_build_object('accepted', false, 'reason', 'product_offline');
  end if;

  insert into public.scene_acquisitions (
    owner_id, project_id, scene_id, provider, status,
    product_id, product_name, product_type, polarisation_channels,
    sensing_start, online, expected_size_bytes, footprint, client_request_id
  ) values (
    scene_row.owner_id, scene_row.project_id, scene_row.id,
    'copernicus'::public.scene_acquisition_provider,
    'queued'::public.scene_acquisition_status,
    v_product_id, v_product_name, v_product_type, v_polarisation,
    v_sensing_start, v_online, v_expected_size, v_footprint, v_client_request_id
  )
  returning * into created_row;

  update public.scenes
  set status = 'queued'::public.scene_status,
      failure_code = null,
      failure_detail = null
  where id = scene_row.id and owner_id = p_owner_id;

  -- kind stays 'process_scene'. A new kind would break workspace.py's
  -- _job_maps, m3_request_scene_cleanup, and bootstrap_m2_jobs.
  insert into public.processing_jobs (
    owner_id, project_id, scene_id, kind, stage, status, progress,
    max_attempts, scene_acquisition_id
  ) values (
    scene_row.owner_id, scene_row.project_id, scene_row.id,
    'process_scene'::public.processing_job_kind,
    'fetch_source'::public.processing_job_stage,
    'queued'::public.processing_job_status,
    0,
    5,
    created_row.id
  ) returning id into job_id;

  perform public.m3_enqueue_task(
    job_id,
    'fetch_source'::public.processing_job_stage,
    'cpu'::public.processing_execution_class,
    jsonb_build_object(
      'scene_acquisition_id', created_row.id,
      'provider', 'copernicus',
      'requested_by', p_owner_id
    ),
    5
  );

  -- Give the workspace something to render immediately: the job polls events
  -- every 5s and the download itself can run for minutes.
  insert into public.processing_job_events (
    processing_job_id, owner_id, project_id, scene_id, status, stage,
    progress, attempt, event_type, detail
  ) values (
    job_id, scene_row.owner_id, scene_row.project_id, scene_row.id,
    'queued'::public.processing_job_status,
    'fetch_source'::public.processing_job_stage,
    0, 0, 'acquisition_queued',
    jsonb_build_object(
      'message', 'Queued a Copernicus download for ' || v_product_name || '.',
      'product_name', v_product_name
    )
  );

  return jsonb_build_object(
    'accepted', true,
    'replayed', false,
    'acquisition_id', created_row.id,
    'scene_id', created_row.scene_id,
    'project_id', created_row.project_id,
    'status', created_row.status,
    'job_id', job_id
  );
end
$$;

revoke all on function public.start_scene_acquisition(uuid, uuid, text, jsonb)
from public, anon, authenticated;
grant execute on function public.start_scene_acquisition(uuid, uuid, text, jsonb)
to service_role;

comment on function public.start_scene_acquisition(uuid, uuid, text, jsonb) is
  'Atomically creates one provider acquisition, its scene transition, its fetch_source job, and its first CPU task.';

-- Side-effect-free probe, following the m2/m3/m4/m5 convention. Unlike those,
-- this one is deliberately NOT a /readyz gate: for M2-M5 the schema was the
-- product, so failing closed was right, but for an optional add-on a forgotten
-- migration must degrade the feature rather than take the whole API down.
-- GET /acquisitions/providers reports schema_not_applied and the UI hides it.
create or replace function public.m7_acquisition_schema_ready()
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select
    to_regclass('public.scene_acquisitions') is not null
    and to_regprocedure('public.start_scene_acquisition(uuid,uuid,text,jsonb)') is not null
    and exists (
      select 1 from pg_attribute
      where attrelid = 'public.processing_jobs'::regclass
        and attname = 'scene_acquisition_id'
        and not attisdropped
    )
    and exists (
      select 1 from pg_enum
      where enumtypid = 'public.processing_job_stage'::regtype
        and enumlabel = 'fetch_source'
    );
$$;

revoke all on function public.m7_acquisition_schema_ready()
from public, anon, authenticated;
grant execute on function public.m7_acquisition_schema_ready()
to service_role;

-- No new cleanup code is needed. scene_acquisitions cascades from scenes, and
-- artifacts_for_cleanup(include_sources => true) already deletes the fetched
-- object from storage during scene cleanup.
