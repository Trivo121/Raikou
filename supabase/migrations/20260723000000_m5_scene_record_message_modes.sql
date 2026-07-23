-- The M5 scene-first chat persists deterministic answers with a
-- "scene_record_<intent>" message mode so every reply records which answer
-- path produced it.  The prior constraint (20260722002000) predates those
-- modes; without this migration the first deterministic scene-record answer
-- fails its insert and aborts the NDJSON stream mid-response.

alter table public.messages
  drop constraint if exists messages_mode_check;

alter table public.messages
  add constraint messages_mode_check
  check (
    mode is null
    or mode = any (
      array[
        'macro_cached',
        'macro_live',
        'micro',
        'hybrid',
        'grounded',
        'insufficient_evidence',
        'grounded_rag',
        'grounded_rag_failed',
        'scene_record_detector_count',
        'scene_record_detector_presence',
        'scene_record_detector_location',
        'scene_record_environment',
        'scene_record_scene_description',
        'scene_record_visual_evidence'
      ]
    )
  );
