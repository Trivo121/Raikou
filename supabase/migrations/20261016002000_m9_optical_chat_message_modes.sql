-- M9 part 3: message modes for the optical (land-cover) chat.
--
-- Scene-scoped chat on an optical run persists every reply with the path that
-- produced it: optical_record_<intent> for answers composed from the run's
-- record, optical_refusal for out-of-scope questions (objects, ownership,
-- military/border/surveillance), optical_insufficient when the run has no
-- record yet, and optical_grounded[_failed] for the language-model fallback.
-- Without these the first optical reply would fail its insert and abort the
-- NDJSON stream mid-response.
--
-- A strict superset of 20260723000000_m5_scene_record_message_modes.sql, so it
-- is re-runnable and every existing row still satisfies it.

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
        'scene_record_visual_evidence',
        'optical_record_download',
        'optical_record_spectral',
        'optical_record_similarity',
        'optical_record_change',
        'optical_record_trust',
        'optical_record_imagery',
        'optical_record_method',
        'optical_record_area',
        'optical_record_summary',
        'optical_refusal',
        'optical_insufficient',
        'optical_grounded',
        'optical_grounded_failed'
      ]
    )
  );
