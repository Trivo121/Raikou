import React, { useState, useEffect } from 'react';
import Landing from './pages/Landing.js';
import Dashboard from './pages/Dashboard.js';
import Login from './pages/Login.js';
import { createClient } from '@supabase/supabase-js';

// Lazy client initialization to prevent app crash if env vars are undefined on load
let supabaseClient = null;
export const getSupabase = () => {
  if (!supabaseClient) {
    const supabaseUrl = process.env.REACT_APP_SUPABASE_URL || process.env.SUPABASE_URL;
    const supabaseAnonKey = process.env.REACT_APP_SUPABASE_ANON_KEY || process.env.SUPABASE_ANON_KEY;

    if (!supabaseUrl || !supabaseAnonKey) {
      console.error(
        "Supabase configuration missing! " +
        "React requires 'REACT_APP_' prefix for environment variables in the browser. " +
        "Please add REACT_APP_SUPABASE_URL and REACT_APP_SUPABASE_ANON_KEY to your environment/build."
      );
      return null;
    }
    // Clean up URL and Key (trim spaces/quotes and strip /rest/v1)
    const cleanUrl = supabaseUrl.trim().replace(/\/rest\/v1\/?$/, '').replace(/^["']|["']$/g, '');
    const cleanKey = supabaseAnonKey.trim().replace(/^["']|["']$/g, '');
    
    supabaseClient = createClient(cleanUrl, cleanKey);
  }
  return supabaseClient;
};

function App() {
  const [currentPath, setCurrentPath] = useState(window.location.pathname);
  const [session, setSession] = useState(null);

  useEffect(() => {
    const handleLocationChange = () => {
      setCurrentPath(window.location.pathname);
    };

    // Listen for back/forward navigation
    window.addEventListener('popstate', handleLocationChange);

    const supabase = getSupabase();
    let subscription = null;

    if (supabase) {
      // Get initial session
      supabase.auth.getSession().then(({ data: { session } }) => {
        setSession(session);
        if (session && (window.location.pathname === '/login' || window.location.pathname === '/')) {
          window.history.pushState({}, '', '/dashboard');
          setCurrentPath('/dashboard');
        }
      });

      // Listen for auth changes
      const { data } = supabase.auth.onAuthStateChange((_event, session) => {
        setSession(session);
        if (session) {
          window.history.pushState({}, '', '/dashboard');
          setCurrentPath('/dashboard');
        } else {
          window.history.pushState({}, '', '/login');
          setCurrentPath('/login');
        }
      });
      subscription = data.subscription;
    }

    return () => {
      window.removeEventListener('popstate', handleLocationChange);
      if (subscription) {
        subscription.unsubscribe();
      }
    };
  }, []);

  // Simple path-based routing switch
  switch (currentPath) {
    case '/dashboard':
      return <Dashboard />;
    case '/login':
    case '/auth':
      return <Login />;
    case '/':
    default:
      return <Landing />;
  }
}

export default App;
