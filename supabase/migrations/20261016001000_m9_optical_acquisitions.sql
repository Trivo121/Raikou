-- M9 part 2: Sentinel-2 acquisitions for optical (land-cover) runs.
--
-- An optical run is an ordinary scene with scenes.metadata.pipeline = 'optical'
-- and one scene_acquisitions row that holds one to three Sentinel-2 dates
-- (stack) on one native-UTM 10 m grid (grid). Everything else -- jobs, tasks,
-- artifacts, cleanup -- is the existing machinery.
--
-- The Sentinel-1 product contract is unchanged: each CHECK below keeps the old
-- Sentinel-1 rule verbatim and adds a Sentinel-2 branch beside it.
--
-- Re-runnable: every statement is add-if-missing, drop-if-exists or
-- create-or-replace.

alter table public.scene_acquisitions
  add column if not exists collection text not null default 'SENTINEL-1',
  add column if not exists cloud_cover double precision,
  add column if not exists stack jsonb,
  add column if not exists grid jsonb,
  add column if not exists estimated_processing_units double precision;

-- Sentinel-2 has no polarisation.
alter table public.scene_acquisitions alter column polarisation_channels drop not null;

alter table public.scene_acquisitions drop constraint if exists scene_acquisitions_product_type_ck;
alter table public.scene_acquisitions add constraint scene_acquisitions_product_type_ck check (
  (collection = 'SENTINEL-1' and product_type = 'IW_GRDH_1S')
  or (collection = 'SENTINEL-2' and product_type = 'S2MSI2A')
);

alter table public.scene_acquisitions drop constraint if exists scene_acquisitions_polarisation_ck;
alter table public.scene_acquisitions add constraint scene_acquisitions_polarisation_ck check (
  (collection = 'SENTINEL-1' and polarisation_channels = 'VV&VH')
  or (collection = 'SENTINEL-2' and polarisation_channels is null)
);

alter table public.scene_acquisitions drop constraint if exists scene_acquisitions_product_name_ck;
alter table public.scene_acquisitions add constraint scene_acquisitions_product_name_ck check (
  (
    (collection = 'SENTINEL-1' and product_name ~ '^S1[A-D]_IW_GRDH_1SDV_')
    or (collection = 'SENTINEL-2' and product_name ~ '^S2[A-D]_MSIL2A_')
  )
  and char_length(product_name) between 1 and 512
  and product_name !~ '[[:cntrl:]]'
);

alter table public.scene_acquisitions drop constraint if exists scene_acquisitions_collection_ck;
alter table public.scene_acquisitions add constraint scene_acquisitions_collection_ck check (
  collection in ('SENTINEL-1', 'SENTINEL-2')
);

-- A Sentinel-2 row is always an area subset with its dates and its grid; the
-- worker cannot act on one without them.
alter table public.scene_acquisitions drop constraint if exists scene_acquisitions_optical_shape_ck;
alter table public.scene_acquisitions add constraint scene_acquisitions_optical_shape_ck check (
  collection <> 'SENTINEL-2' or (
    mode = 'aoi_subset'::public.scene_acquisition_mode
    and jsonb_typeof(stack) = 'array'
    and jsonb_array_length(stack) between 1 and 3
    and jsonb_typeof(grid) = 'object'
  )
);

alter table public.scene_acquisitions drop constraint if exists scene_acquisitions_cloud_cover_ck;
alter table public.scene_acquisitions add constraint scene_acquisitions_cloud_cover_ck check (
  cloud_cover is null or cloud_cover between 0 and 100
);

alter table public.scene_acquisitions drop constraint if exists scene_acquisitions_estimated_pu_ck;
alter table public.scene_acquisitions add constraint scene_acquisitions_estimated_pu_ck check (
  estimated_processing_units is null or estimated_processing_units >= 0
);

create index if not exists scene_acquisitions_collection_created_idx
  on public.scene_acquisitions (collection, created_at desc);

comment on column public.scene_acquisitions.collection is
  'SENTINEL-1 (SAR pipeline) or SENTINEL-2 (optical land-cover pipeline).';
comment on column public.scene_acquisitions.stack is
  'Sentinel-2 only: the one to three dates of an optical run, oldest first.';
comment on column public.scene_acquisitions.grid is
  'Sentinel-2 only: the native-UTM 10 m grid every date is rendered onto.';
comment on column public.scene_acquisitions.estimated_processing_units is
  'Processing units this request is expected to cost; counted against budgets until processing_units is known.';


-- Create an optical run's acquisition, its scene transition, its job and its
-- first task in one transaction. Mirrors start_scene_acquisition (m8), which
-- stays untouched so the SAR path cannot regress.
create or replace function public.start_optical_acquisition(
  p_owner_id uuid,
  p_scene_id uuid,
  p_client_request_id text,
  p_request jsonb
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
  v_stack jsonb;
  v_grid jsonb;
  v_first jsonb;
  v_dates integer;
begin
  if p_owner_id is null or p_scene_id is null then
    raise exception 'owner_id and scene_id are required';
  end if;
  if jsonb_typeof(p_request) is distinct from 'object' then
    raise exception 'request must be a JSON object';
  end if;

  v_client_request_id := nullif(btrim(coalesce(p_client_request_id, '')), '');

  -- Replay before locking anything: a retried POST must return the original run.
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
        'job_id', (select id from public.processing_jobs where scene_acquisition_id = existing_row.id limit 1)
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

  if coalesce(scene_row.metadata->>'pipeline', '') <> 'optical' then
    return jsonb_build_object('accepted', false, 'reason', 'not_optical_scene');
  end if;

  if scene_row.status not in (
    'draft'::public.scene_status,
    'failed'::public.scene_status,
    'cancelled'::public.scene_status
  ) then
    return jsonb_build_object('accepted', false, 'reason', 'scene_not_acceptable');
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

  if exists (
    select 1 from public.scene_artifacts
    where scene_id = scene_row.id and owner_id = p_owner_id
      and kind in ('source_archive'::public.artifact_kind, 'source_raster'::public.artifact_kind)
      and status = 'available'::public.artifact_status
  ) then
    return jsonb_build_object('accepted', false, 'reason', 'source_already_present');
  end if;

  v_stack := p_request->'stack';
  v_grid := p_request->'grid';
  if jsonb_typeof(v_stack) is distinct from 'array'
     or jsonb_array_length(v_stack) not between 1 and 3
     or jsonb_typeof(v_grid) is distinct from 'object' then
    raise exception 'an optical request needs one to three dates and a grid';
  end if;
  v_first := v_stack->0;
  v_dates := jsonb_array_length(v_stack);

  insert into public.scene_acquisitions (
    owner_id, project_id, scene_id, provider, status, collection,
    product_id, product_name, product_type, polarisation_channels,
    sensing_start, online, expected_size_bytes, footprint, client_request_id,
    mode, subset_west, subset_south, subset_east, subset_north,
    subset_width_px, subset_height_px, subset_metres_per_pixel,
    cloud_cover, stack, grid, estimated_processing_units
  ) values (
    scene_row.owner_id, scene_row.project_id, scene_row.id,
    'copernicus'::public.scene_acquisition_provider,
    'queued'::public.scene_acquisition_status,
    'SENTINEL-2',
    v_first->>'product_id',
    v_first->>'product_name',
    'S2MSI2A',
    null,
    nullif(v_first->>'sensing_start', '')::timestamptz,
    true,
    (p_request->>'expected_size_bytes')::bigint,
    case when jsonb_typeof(p_request->'footprint') = 'object' then p_request->'footprint' else null end,
    v_client_request_id,
    'aoi_subset'::public.scene_acquisition_mode,
    (p_request->>'west')::double precision,
    (p_request->>'south')::double precision,
    (p_request->>'east')::double precision,
    (p_request->>'north')::double precision,
    (v_grid->>'width')::integer,
    (v_grid->>'height')::integer,
    10,
    nullif(p_request->>'cloud_cover', '')::double precision,
    v_stack,
    v_grid,
    nullif(p_request->>'estimated_processing_units', '')::double precision
  )
  returning * into created_row;

  update public.scenes
  set status = 'queued'::public.scene_status,
      failure_code = null,
      failure_detail = null
  where id = scene_row.id and owner_id = p_owner_id;

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
      'pipeline', 'optical',
      'scene_acquisition_id', created_row.id,
      'provider', 'copernicus',
      'collection', 'SENTINEL-2',
      'requested_by', p_owner_id
    ),
    5
  );

  insert into public.processing_job_events (
    processing_job_id, owner_id, project_id, scene_id, status, stage,
    progress, attempt, event_type, detail
  ) values (
    job_id, scene_row.owner_id, scene_row.project_id, scene_row.id,
    'queued'::public.processing_job_status,
    'fetch_source'::public.processing_job_stage,
    0, 0, 'acquisition_queued',
    jsonb_build_object(
      'message', 'Queued a Sentinel-2 fetch of ' || v_dates || case when v_dates = 1 then ' date.' else ' dates.' end,
      'dates', v_dates
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

revoke all on function public.start_optical_acquisition(uuid, uuid, text, jsonb) from public, anon, authenticated;
grant execute on function public.start_optical_acquisition(uuid, uuid, text, jsonb) to service_role;

comment on function public.start_optical_acquisition(uuid, uuid, text, jsonb) is
  'Atomically creates one Sentinel-2 acquisition (1-3 dates on one grid), its scene transition, its fetch_source job and its first CPU task.';


-- Processing units this calendar month (UTC): this user's optical runs, and
-- every acquisition on the account (SAR subsets included), each counting the
-- spend once known and the estimate until then.
create or replace function public.optical_processing_units_this_month(p_owner_id uuid)
returns jsonb
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select jsonb_build_object(
    'user_optical', coalesce(sum(coalesce(processing_units, estimated_processing_units, 0))
      filter (where owner_id = p_owner_id and collection = 'SENTINEL-2'), 0),
    'all', coalesce(sum(coalesce(processing_units, estimated_processing_units, 0)), 0),
    'month_start', date_trunc('month', now() at time zone 'utc')
  )
  from public.scene_acquisitions
  where created_at >= (date_trunc('month', now() at time zone 'utc') at time zone 'utc')
    and status <> 'failed'::public.scene_acquisition_status;
$$;

revoke all on function public.optical_processing_units_this_month(uuid) from public, anon, authenticated;
grant execute on function public.optical_processing_units_this_month(uuid) to service_role;


-- Side-effect-free probe for GET /optical/providers, in the m7 style: a missing
-- migration degrades the optical feature instead of failing /readyz.
create or replace function public.m9_optical_schema_ready()
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select
    to_regprocedure('public.start_optical_acquisition(uuid,uuid,text,jsonb)') is not null
    and to_regprocedure('public.optical_processing_units_this_month(uuid)') is not null
    and exists (
      select 1 from pg_attribute
      where attrelid = 'public.scene_acquisitions'::regclass
        and attname in ('collection', 'stack', 'grid')
        and not attisdropped
      having count(*) = 3
    )
    and exists (
      select 1 from pg_enum
      where enumtypid = 'public.processing_execution_class'::regtype
        and enumlabel = 'optical'
    );
$$;

revoke all on function public.m9_optical_schema_ready() from public, anon, authenticated;
grant execute on function public.m9_optical_schema_ready() to service_role;
