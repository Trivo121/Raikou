-- M8: fetch only the area of interest, not the frame that contains it.
--
-- A GRD is distributed as a whole ~250x170 km frame, so analysing a city meant
-- moving 1.7 GB. Sentinel Hub renders an arbitrary box out of the same
-- acquisition instead, orthorectified and already calibrated to sigma0.
--
-- Pixel size is fixed at 10 m and never traded away to fit a larger area:
-- reBEN land cover is trained on 120 px at 10 m, and a coarser subset would
-- hand it a 4.8 km window where it expects 1.2 km. The Process API's 2500 px
-- per-request ceiling therefore becomes a real ~25 km cap on the box, enforced
-- in the API and again in the worker.
--
-- 'full_frame' is the default for every existing row so nothing already
-- recorded changes meaning.

do $$
begin
  if not exists (
    select 1 from pg_type
    where typname = 'scene_acquisition_mode' and typnamespace = 'public'::regnamespace
  ) then
    create type public.scene_acquisition_mode as enum ('aoi_subset', 'full_frame');
  end if;
end
$$;

alter table public.scene_acquisitions
  add column if not exists mode public.scene_acquisition_mode
    not null default 'full_frame'::public.scene_acquisition_mode,
  add column if not exists subset_west double precision,
  add column if not exists subset_south double precision,
  add column if not exists subset_east double precision,
  add column if not exists subset_north double precision,
  add column if not exists subset_width_px integer,
  add column if not exists subset_height_px integer,
  add column if not exists subset_metres_per_pixel double precision,
  add column if not exists processing_units double precision;

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'scene_acquisitions_subset_box_ck'
  ) then
    alter table public.scene_acquisitions add constraint scene_acquisitions_subset_box_ck check (
      mode <> 'aoi_subset'::public.scene_acquisition_mode or (
        subset_west is not null and subset_south is not null
        and subset_east is not null and subset_north is not null
        and subset_west between -180 and 180 and subset_east between -180 and 180
        and subset_south between -90 and 90 and subset_north between -90 and 90
        and subset_west < subset_east and subset_south < subset_north
        and subset_width_px between 1 and 2500
        and subset_height_px between 1 and 2500
      )
    );
  end if;
  -- A subset is rendered on demand, so the frame's ContentLength says nothing
  -- about what will actually be written. Only a full frame must match it.
  if exists (
    select 1 from pg_constraint where conname = 'scene_acquisitions_expected_size_ck'
  ) then
    alter table public.scene_acquisitions drop constraint scene_acquisitions_expected_size_ck;
  end if;
  alter table public.scene_acquisitions add constraint scene_acquisitions_expected_size_ck check (
    expected_size_bytes is null or expected_size_bytes > 0
  );
end
$$;

comment on column public.scene_acquisitions.mode is
  'aoi_subset renders just the drawn box via Sentinel Hub; full_frame downloads the whole SAFE product.';
comment on column public.scene_acquisitions.processing_units is
  'Sentinel Hub processing units spent. Null for a full frame, which draws on the data quota instead.';

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
  v_mode public.scene_acquisition_mode;
  v_west double precision;
  v_south double precision;
  v_east double precision;
  v_north double precision;
  v_width_px integer;
  v_height_px integer;
  v_mpp double precision;
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

  v_mode := coalesce(
    nullif(btrim(coalesce(p_product->>'mode', '')), ''),
    'full_frame'
  )::public.scene_acquisition_mode;
  v_west  := nullif(btrim(coalesce(p_product->>'subset_west',  '')), '')::double precision;
  v_south := nullif(btrim(coalesce(p_product->>'subset_south', '')), '')::double precision;
  v_east  := nullif(btrim(coalesce(p_product->>'subset_east',  '')), '')::double precision;
  v_north := nullif(btrim(coalesce(p_product->>'subset_north', '')), '')::double precision;
  v_width_px  := nullif(btrim(coalesce(p_product->>'subset_width_px',  '')), '')::integer;
  v_height_px := nullif(btrim(coalesce(p_product->>'subset_height_px', '')), '')::integer;
  v_mpp := nullif(btrim(coalesce(p_product->>'subset_metres_per_pixel', '')), '')::double precision;

  -- A subset is defined by its box. Accepting one without it would create an
  -- acquisition the worker cannot act on, discovered only once it ran.
  if v_mode = 'aoi_subset'::public.scene_acquisition_mode
     and (v_west is null or v_south is null or v_east is null or v_north is null
          or v_width_px is null or v_height_px is null) then
    raise exception 'an area-of-interest subset requires its bounding box and pixel size';
  end if;

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
    sensing_start, online, expected_size_bytes, footprint, client_request_id,
    mode, subset_west, subset_south, subset_east, subset_north,
    subset_width_px, subset_height_px, subset_metres_per_pixel
  ) values (
    scene_row.owner_id, scene_row.project_id, scene_row.id,
    'copernicus'::public.scene_acquisition_provider,
    'queued'::public.scene_acquisition_status,
    v_product_id, v_product_name, v_product_type, v_polarisation,
    v_sensing_start, v_online, v_expected_size, v_footprint, v_client_request_id,
    v_mode, v_west, v_south, v_east, v_north, v_width_px, v_height_px, v_mpp
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
      'mode', v_mode::text,
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
    'acquisition_id', created_row.id, 'mode', created_row.mode::text,
    'scene_id', created_row.scene_id,
    'project_id', created_row.project_id,
    'status', created_row.status,
    'job_id', job_id
  );
end
$$;
