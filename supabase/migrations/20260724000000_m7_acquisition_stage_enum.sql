-- M7 part 1: add the acquisition stage value in its own migration. PostgreSQL
-- requires a commit before a newly-added enum value may be used by columns or
-- functions, which is why the table, RPC, and probe live in the next file.
--
-- This is the only new stage. Everything else deliberately reuses existing
-- labels -- in particular processing_job_kind stays 'process_scene', because a
-- new kind would break _job_maps in workspace.py, m3_request_scene_cleanup, and
-- bootstrap_m2_jobs, all of which filter on that exact value.

alter type public.processing_job_stage add value if not exists 'fetch_source' before 'validate_upload';
