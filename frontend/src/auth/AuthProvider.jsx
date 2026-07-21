import { createContext, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { createApiClient } from '../services/api';
import { queryClient } from '../lib/queryClient';
import { getSupabase, getSupabaseConfigurationError } from '../lib/supabase';

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [session, setSession] = useState(null);
  const [loading, setLoading] = useState(true);
  const [configurationError, setConfigurationError] = useState(null);
  const activeUserId = useRef(null);

  useEffect(() => {
    const client = getSupabase();
    if (!client) {
      setConfigurationError(getSupabaseConfigurationError());
      setLoading(false);
      return undefined;
    }

    const applySession = (nextSession) => {
      const nextUserId = nextSession?.user?.id ?? null;
      if (activeUserId.current !== nextUserId) {
        // Query data is private. Never reuse a previous browser user's cache
        // while Supabase completes a sign-out/sign-in transition.
        queryClient.clear();
        activeUserId.current = nextUserId;
      }
      setSession(nextSession ?? null);
      setLoading(false);
    };

    let active = true;
    client.auth.getSession().then(({ data, error }) => {
      if (!active) return;
      if (error) setConfigurationError(error.message);
      applySession(data.session);
    });

    const { data: listener } = client.auth.onAuthStateChange((_event, nextSession) => {
      if (active) applySession(nextSession);
    });

    return () => {
      active = false;
      listener.subscription.unsubscribe();
    };
  }, []);

  const api = useMemo(
    () => createApiClient(async () => session?.access_token || null),
    [session?.access_token],
  );

  const value = useMemo(() => {
    const client = getSupabase();
    return {
      api,
      session,
      user: session?.user ?? null,
      loading,
      configurationError,
      signInWithPassword: (credentials) => client?.auth.signInWithPassword(credentials),
      signUp: (credentials) => client?.auth.signUp(credentials),
      signInWithGoogle: () => {
        if (!client) {
          return Promise.resolve({
            data: { url: null },
            error: new Error(getSupabaseConfigurationError()),
          });
        }

        // Keep the redirect under application control. This makes a blocked
        // or failed OAuth hand-off visible in the UI rather than silently
        // leaving the user on the login screen.
        return client.auth.signInWithOAuth({
          provider: 'google',
          options: {
            redirectTo: `${window.location.origin}/dashboard`,
            skipBrowserRedirect: true,
          },
        });
      },
      signOut: async () => {
        queryClient.clear();
        activeUserId.current = null;
        return client?.auth.signOut();
      },
    };
  }, [api, configurationError, loading, session]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) throw new Error('useAuth must be used inside AuthProvider.');
  return context;
}

export function useApi() {
  return useAuth().api;
}
