-- M5 grounded chat uses explicit evidence-bound message modes.  Preserve the
-- legacy modes while allowing the API to persist user turns, insufficient-
-- evidence replies, and completed/failed grounded generations.

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
        'grounded_rag_failed'
      ]
    )
  );
