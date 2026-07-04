import React, { useState, useEffect } from 'react';
import { getSupabase } from '../App';

/* ─────────────────────────────────────────────
   KEYFRAME ANIMATIONS
───────────────────────────────────────────── */
const KEYFRAMES = `
  @keyframes riseIn {
    from { opacity: 0; transform: translateY(16px); }
    to   { opacity: 1; transform: translateY(0);    }
  }
`;

/* ─────────────────────────────────────────────
   DESIGN TOKENS (Strict adherence to your spec)
───────────────────────────────────────────── */
const T = {
  bg: 'rgb(0, 0, 0)',
  text: 'rgb(255, 255, 255)',
  textMuted: 'rgba(255, 255, 255, 0.6)',
  accent: 'rgb(0, 153, 255)',
  accentHover: '#007acc',
  border: 'rgba(255, 255, 255, 0.1)',
  card: 'rgba(255, 255, 255, 0.02)',
};

/* ─────────────────────────────────────────────
   TYPOGRAPHY SYSTEM
───────────────────────────────────────────── */
const headingFont = { fontFamily: "'Space Grotesk', sans-serif" };
const bodyFont = { fontFamily: "'Inter', -apple-system, BlinkMacSystemFont, sans-serif" };

export default function Login() {
  const [isSignUp, setIsSignUp] = useState(false);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState(null);
  const [successMsg, setSuccessMsg] = useState(null);

  useEffect(() => {
    // Inject fonts if not already loaded globally
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = 'https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500&family=Inter:wght@400;500&display=swap';
    document.head.appendChild(link);
    return () => document.head.removeChild(link);
  }, []);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setIsLoading(true);
    setError(null);
    setSuccessMsg(null);

    const client = getSupabase();
    if (!client) {
      setError("Supabase client not initialized.");
      setIsLoading(false);
      return;
    }

    try {
      if (isSignUp) {
        const { error } = await client.auth.signUp({ email, password });
        if (error) throw error;
        setSuccessMsg("Success! Please check your email to confirm your account.");
      } else {
        const { error } = await client.auth.signInWithPassword({ email, password });
        if (error) throw error;
        // App.js auth listener will handle redirect
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setIsLoading(false);
    }
  };

  const handleGoogleAuth = async () => {
    const client = getSupabase();
    if (!client) {
      alert(
        "Supabase client could not be initialized.\n\n" +
        "React requires environment variables in the browser to start with 'REACT_APP_'.\n\n" +
        "Please add duplicate entries in your .env file:\n" +
        "REACT_APP_SUPABASE_URL=...\n" +
        "REACT_APP_SUPABASE_ANON_KEY=..."
      );
      return;
    }
    const { data, error } = await client.auth.signInWithOAuth({
      provider: 'google',
      options: {
        redirectTo: window.location.origin,
      },
    });
    if (error) {
      console.error('Error logging in with Google:', error.message);
    }
  };

  return (
    <>
      <style>{KEYFRAMES}</style>

      <div
        className="min-h-screen flex flex-col relative selection:bg-[#0099FF] selection:text-white"
        style={{ ...bodyFont, backgroundColor: T.bg, color: T.text }}
      >

        {/* ══════════════════════════════════════
            MINIMAL TOP NAV
        ══════════════════════════════════════ */}
        <nav
          className="fixed top-0 left-0 right-0 z-50 flex items-center px-10 h-[60px]"
          style={{
            borderBottom: `1px solid ${T.borderSub || 'rgba(255,255,255,0.05)'}`,
            backgroundColor: 'rgba(0,0,0,0.88)',
            backdropFilter: 'blur(18px)',
            WebkitBackdropFilter: 'blur(18px)',
          }}
        >
          {/* Logo */}
          <a href="/" className="flex items-center gap-2.5 select-none transition-opacity hover:opacity-80" style={{ textDecoration: 'none' }}>
            <span
              className="block w-2 h-2 rounded-full"
              style={{ backgroundColor: T.accent, boxShadow: '0 0 10px rgba(0,153,255,0.9)' }}
            />
            <span
              className="text-white text-sm font-semibold uppercase tracking-[0.18em]"
              style={headingFont}
            >
              Raikou
            </span>
          </a>
        </nav>

        {/* ══════════════════════════════════════
            AUTH CONTAINER
        ══════════════════════════════════════ */}
        <main className="flex-1 flex flex-col items-center justify-center px-6 w-full relative z-10">

          {/* Background Ambient Glow */}
          <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[600px] h-[600px] bg-[#0099FF]/5 blur-[120px] rounded-full pointer-events-none -z-10" />

          {/* Framer-style rigid card */}
          <div
            className="w-full max-w-[420px] p-8 sm:p-10 rounded-2xl backdrop-blur-xl shadow-2xl"
            style={{
              backgroundColor: T.card,
              border: `1px solid ${T.border}`,
              animation: 'riseIn 0.6s cubic-bezier(0.16, 1, 0.3, 1) both'
            }}
          >

            {/* Header */}
            <div className="mb-8">
              <h1
                style={{ ...headingFont, fontSize: '32px', lineHeight: '1', letterSpacing: '-1px' }}
                className="font-medium text-white mb-3"
              >
                {isSignUp ? 'Request access' : 'Welcome back'}
              </h1>
              <p className="text-sm leading-relaxed" style={{ color: T.textMuted }}>
                {isSignUp
                  ? 'Join the private beta to ingest and query cross-modal satellite indices.'
                  : 'Log in to your operator dashboard to query SAR indices.'}
              </p>
            </div>

            {error && (
              <div className="mb-5 p-3 rounded-lg text-xs" style={{ backgroundColor: 'rgba(255,50,50,0.1)', color: '#ff6b6b', border: '1px solid rgba(255,50,50,0.2)' }}>
                {error}
              </div>
            )}
            
            {successMsg && (
              <div className="mb-5 p-3 rounded-lg text-xs" style={{ backgroundColor: 'rgba(50,255,100,0.1)', color: '#4ade80', border: '1px solid rgba(50,255,100,0.2)' }}>
                {successMsg}
              </div>
            )}

            {/* Main Form */}
            <form onSubmit={handleSubmit} className="flex flex-col gap-5">

              {/* Email Input */}
              <div className="flex flex-col gap-2">
                <label htmlFor="email" className="text-xs font-medium" style={{ color: T.textMuted }}>
                  Email
                </label>
                <input
                  id="email"
                  type="email"
                  required
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="operator@raikou.io"
                  className="w-full h-12 px-4 rounded-xl bg-black/50 text-sm text-white placeholder-white/20 border transition-all duration-200 outline-none"
                  style={{ borderColor: T.border }}
                  onFocus={(e) => {
                    e.target.style.borderColor = T.accent;
                    e.target.style.boxShadow = `0 0 0 1px ${T.accent}`;
                  }}
                  onBlur={(e) => {
                    e.target.style.borderColor = T.border;
                    e.target.style.boxShadow = 'none';
                  }}
                />
              </div>

              {/* Password Input */}
              <div className="flex flex-col gap-2">
                <div className="flex justify-between items-center">
                  <label htmlFor="password" className="text-xs font-medium" style={{ color: T.textMuted }}>
                    Password
                  </label>
                  {!isSignUp && (
                    <a href="#" className="text-xs transition-colors hover:text-white" style={{ color: T.textMuted }}>
                      Forgot password?
                    </a>
                  )}
                </div>
                <input
                  id="password"
                  type="password"
                  required
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="••••••••"
                  className="w-full h-12 px-4 rounded-xl bg-black/50 text-sm text-white placeholder-white/20 border transition-all duration-200 outline-none"
                  style={{ borderColor: T.border }}
                  onFocus={(e) => {
                    e.target.style.borderColor = T.accent;
                    e.target.style.boxShadow = `0 0 0 1px ${T.accent}`;
                  }}
                  onBlur={(e) => {
                    e.target.style.borderColor = T.border;
                    e.target.style.boxShadow = 'none';
                  }}
                />
              </div>

              {/* Submit Button */}
              <button
                type="submit"
                disabled={isLoading}
                className="w-full h-12 mt-2 rounded-xl font-medium text-white transition-all active:scale-[0.98] disabled:opacity-50 disabled:cursor-not-allowed"
                style={{ backgroundColor: T.accent, fontSize: '14.5px' }}
                onMouseEnter={e => { if (!isLoading) e.currentTarget.style.backgroundColor = T.accentHover; }}
                onMouseLeave={e => { if (!isLoading) e.currentTarget.style.backgroundColor = T.accent; }}
              >
                {isLoading ? 'Processing...' : (isSignUp ? 'Apply for Beta' : 'Log in')}
              </button>
            </form>

            {/* Divider */}
            <div className="flex items-center gap-4 my-6">
              <div className="flex-1 h-px" style={{ backgroundColor: T.border }} />
              <span className="text-xs uppercase tracking-widest font-medium" style={{ color: T.textMuted }}>Or</span>
              <div className="flex-1 h-px" style={{ backgroundColor: T.border }} />
            </div>

            {/* Google OAuth Button */}
            <button
              onClick={handleGoogleAuth}
              type="button"
              className="w-full h-12 flex items-center justify-center gap-3 rounded-xl border bg-transparent transition-all active:scale-[0.98]"
              style={{ borderColor: T.border, color: T.text, fontSize: '14.5px' }}
              onMouseEnter={e => e.currentTarget.style.backgroundColor = 'rgba(255,255,255,0.03)'}
              onMouseLeave={e => e.currentTarget.style.backgroundColor = 'transparent'}
            >
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" fill="#4285F4" />
                <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853" />
                <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05" />
                <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335" />
              </svg>
              Continue with Google
            </button>

            {/* Toggle Sign Up / Sign In */}
            <div className="mt-8 text-center">
              <span className="text-sm" style={{ color: T.textMuted }}>
                {isSignUp ? 'Already an operator?' : "Don't have an account?"}{' '}
                <button
                  onClick={() => setIsSignUp(!isSignUp)}
                  className="font-medium transition-colors hover:text-white"
                  style={{ color: T.accent }}
                >
                  {isSignUp ? 'Log in' : 'Sign up'}
                </button>
              </span>
            </div>

          </div>
        </main>
      </div>
    </>
  );
}