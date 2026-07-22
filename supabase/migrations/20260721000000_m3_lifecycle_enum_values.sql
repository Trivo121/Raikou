-- M3 part 1: add lifecycle values in their own migration. PostgreSQL requires
-- a commit before newly-added enum values are used by columns or functions.

alter type public.processing_job_status add value if not exists 'validating' after 'queued';
alter type public.processing_job_status add value if not exists 'processing' after 'validating';
alter type public.processing_job_status add value if not exists 'ready' before 'failed';

alter type public.processing_job_stage add value if not exists 'build_vrt' after 'extract_metadata';
alter type public.processing_job_stage add value if not exists 'cleanup' after 'finalize';

alter type public.scene_status add value if not exists 'deleting' before 'archived';

alter type public.artifact_kind add value if not exists 'embedding_manifest' after 'evidence';
alter type public.artifact_kind add value if not exists 'scene_record' after 'embedding_manifest';
