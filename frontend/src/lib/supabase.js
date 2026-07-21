import { createClient } from '@supabase/supabase-js';

let supabaseClient;

function configuredValue(value) {
  return value?.trim().replace(/^['"]|['"]$/g, '') || '';
}

/**
 * The browser gets only the Supabase URL and anonymous key. Product data must
 * travel through FastAPI; this client is intentionally limited to auth.
 */
export function getSupabase() {
  if (supabaseClient) return supabaseClient;

  const url = configuredValue(
    import.meta.env.VITE_SUPABASE_URL || import.meta.env.REACT_APP_SUPABASE_URL,
  );
  const anonKey = configuredValue(
    import.meta.env.VITE_SUPABASE_ANON_KEY || import.meta.env.REACT_APP_SUPABASE_ANON_KEY,
  );

  if (!url || !anonKey) return null;

  supabaseClient = createClient(url.replace(/\/rest\/v1\/?$/, ''), anonKey, {
    auth: {
      persistSession: true,
      autoRefreshToken: true,
      detectSessionInUrl: true,
    },
  });

  return supabaseClient;
}

export function getSupabaseConfigurationError() {
  return getSupabase()
    ? null
    : 'Supabase is not configured. Set VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY.';
}
