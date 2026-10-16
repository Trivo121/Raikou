-- M9 part 1: an execution class for the optical (OlmoEarth land-cover) stage.
--
-- The optical model runs in its own venv and worker process, so its tasks need
-- their own Redis stream, which the dispatcher derives from this class.
--
-- A file of its own on purpose: PostgreSQL will not let a new enum value be
-- used by a function body or check in the transaction that added it, and the
-- deploy loop commits each migration file separately.

alter type public.processing_execution_class add value if not exists 'optical';
