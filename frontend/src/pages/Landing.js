import React, { useEffect } from 'react';

/* ─────────────────────────────────────────────
   KEYFRAME ANIMATIONS
   (Defined here so no external CSS file needed)
───────────────────────────────────────────── */
const KEYFRAMES = `
  @keyframes sweep {
    from { transform: rotate(0deg); }
    to   { transform: rotate(360deg); }
  }
  @keyframes riseIn {
    from { opacity: 0; transform: translateY(28px); }
    to   { opacity: 1; transform: translateY(0);    }
  }
  @keyframes blink {
    0%, 100% { opacity: 1; }
    50%       { opacity: 0.25; }
  }
`;

/* ─────────────────────────────────────────────
   DESIGN TOKENS  (match your reference site)
───────────────────────────────────────────── */
const T = {
  bg: '#000000',
  text: '#ffffff',
  textMuted: 'rgba(255,255,255,0.58)',
  textFaint: 'rgba(255,255,255,0.38)',
  textGhost: 'rgba(255,255,255,0.22)',
  accent: '#0099ff',               // rgb(0,153,255)
  accentSoft: '#88c9f7',               // rgb(136,201,247)
  accentBg: 'rgba(0,153,255,0.10)',
  accentBd: 'rgba(0,153,255,0.30)',
  border: 'rgba(255,255,255,0.07)',
  borderSub: 'rgba(255,255,255,0.05)',
  card: 'rgba(255,255,255,0.02)',
};

/* ─────────────────────────────────────────────
   CONTENT DATA
───────────────────────────────────────────── */
const STATS = [
  { val: '512-d', label: 'Shared latent space' },
  { val: '< 5 ms', label: 'Query latency' },
  { val: '24 / 7', label: 'All-weather retrieval' },
];

const FEATURES = [
  {
    eyebrow: 'Sensing',
    title: 'All-weather, day-and-night',
    body: 'SAR microwave backscatter penetrates cloud cover, rain, and complete darkness. When optical sensors go blind, the system keeps working.',
  },
  {
    eyebrow: 'Retrieval',
    title: 'Cross-modal vector search',
    body: 'Query with a text string or an optical image. Both are mapped into the same 512-dimensional space as indexed SAR patches — no modality mismatch.',
  },
  {
    eyebrow: 'Performance',
    title: 'Real-time on edge hardware',
    body: 'HNSW indexing via Qdrant returns nearest-neighbour matches in milliseconds, even on constrained hardware without a dedicated GPU cluster.',
  },
];

const USE_CASES = [
  {
    tag: 'Use case A',
    modality: 'Text → SAR',
    title: 'Weather-blinded search',
    prompt: '"Commercial cargo vessels traveling south"',
    body: 'A tropical cyclone blankets the strait. Optical satellites see only white cloud tops. Type a query — the system searches live SAR frames and returns vessel positions with exact coordinates.',
  },
  {
    tag: 'Use case B',
    modality: 'Optical → SAR',
    title: 'Intelligence match',
    prompt: 'submarine_port_optical_03_2024.tif',
    body: 'You hold a three-month-old daytime photo of a submarine port. Upload it. Structural geometry is extracted, the SAR index is queried, and the midnight radar capture of that exact coordinate is surfaced.',
  },
];

const PIPELINE = [
  { step: 'Ingest', desc: 'GeoTIFF, Sentinel-1 SAFE, HDF5. Scene metadata extracted automatically.' },
  { step: 'Condition', desc: 'Radiometric calibration. Lee speckle filter. 256 × 256 patch tiling.' },
  { step: 'Encode', desc: 'SARCLIP embeds each patch into a 512-d vector at FP16 precision.' },
  { step: 'Retrieve', desc: 'Text or image query maps to the same space. Top-K results instantly.' },
];

/* ─────────────────────────────────────────────
   SHARED STYLE HELPERS
───────────────────────────────────────────── */
const headingFont = { fontFamily: "'Space Grotesk', sans-serif" };
const bodyFont = { fontFamily: "'Inter', -apple-system, BlinkMacSystemFont, sans-serif" };

const eyebrow = {
  fontSize: '11px',
  letterSpacing: '0.18em',
  textTransform: 'uppercase',
  color: T.accent,
  marginBottom: '14px',
};

const sectionHeading = {
  ...headingFont,
  fontSize: 'clamp(28px, 4vw, 40px)',
  fontWeight: 500,
  color: T.text,
  letterSpacing: '-0.022em',
  lineHeight: 1.15,
};

/* ─────────────────────────────────────────────
   COMPONENT
───────────────────────────────────────────── */
export default function Landing() {
  /* Load Space Grotesk (closest public match to GT Walsheim) + Inter */
  useEffect(() => {
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = 'https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600&family=Inter:wght@400;500&display=swap';
    document.head.appendChild(link);
    return () => document.head.removeChild(link);
  }, []);

  /* Both CTAs are placeholders — Supabase auth wired in Login.js */
  const goLogin = () => { 
    window.history.pushState({}, '', '/login');
    window.dispatchEvent(new PopStateEvent('popstate'));
  };

  return (
    <>
      <style>{KEYFRAMES}</style>

      <div
        style={{ ...bodyFont, backgroundColor: T.bg, color: T.text, fontSize: '14px', lineHeight: '1.65', overflowX: 'hidden', minHeight: '100vh' }}
      >

        {/* ══════════════════════════════════════
            NAVBAR
        ══════════════════════════════════════ */}
        <nav
          className="fixed top-0 left-0 right-0 z-50 flex items-center justify-between px-10 h-[60px]"
          style={{
            borderBottom: `1px solid ${T.borderSub}`,
            backgroundColor: 'rgba(0,0,0,0.88)',
            backdropFilter: 'blur(18px)',
            WebkitBackdropFilter: 'blur(18px)',
          }}
        >
          {/* Logo */}
          <div className="flex items-center gap-2.5 select-none">
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
          </div>

          {/* Nav links — hidden on mobile */}
          <div className="hidden md:flex items-center gap-8" style={{ fontSize: '13px', color: T.textMuted }}>
            {['Features', 'Use Cases', 'Pipeline'].map(label => (
              <span
                key={label}
                className="cursor-pointer transition-colors duration-150 hover:text-white"
              >
                {label}
              </span>
            ))}
          </div>

          {/* CTA row */}
          <div className="flex items-center gap-2">
            {/* ── placeholder: Supabase login handled in Login.js ── */}
            <button
              onClick={goLogin}
              className="px-4 py-2 rounded-lg bg-transparent border-0 cursor-pointer transition-colors duration-150 hover:text-white"
              style={{ fontSize: '13px', color: T.textMuted }}
            >
              Login
            </button>

            {/* ── placeholder: Supabase signup / request access ── */}
            <button
              onClick={goLogin}
              className="px-5 py-2 rounded-lg border-0 text-white font-medium cursor-pointer transition-all duration-150 active:scale-[0.96]"
              style={{ fontSize: '13px', backgroundColor: T.accent }}
              onMouseEnter={e => (e.currentTarget.style.backgroundColor = '#007ecc')}
              onMouseLeave={e => (e.currentTarget.style.backgroundColor = T.accent)}
            >
              Request Access
            </button>
          </div>
        </nav>

        {/* ══════════════════════════════════════
            HERO
        ══════════════════════════════════════ */}
        <section
          className="relative flex flex-col items-center justify-center min-h-screen px-6 pt-20 pb-16 overflow-hidden"
        >
          {/* — Radar rings + sweep — */}
          <div className="absolute inset-0 flex items-center justify-center pointer-events-none select-none">
            {[160, 300, 440, 580, 720, 860].map((d, i) => (
              <div
                key={d}
                className="absolute rounded-full"
                style={{
                  width: d,
                  height: d,
                  border: `1px solid rgba(255,255,255,${Math.max(0.008, 0.055 - i * 0.009)})`,
                }}
              />
            ))}

            {/* Radar sweep — the signature element of this page */}
            <div
              className="absolute rounded-full"
              style={{
                width: 580,
                height: 580,
                background: 'conic-gradient(from 0deg at 50% 50%, rgba(0,153,255,0.13) 0deg, transparent 65deg, transparent 360deg)',
                animation: 'sweep 5s linear infinite',
              }}
            />

            {/* Crosshairs */}
            <div className="absolute w-[860px] h-px" style={{ backgroundColor: 'rgba(255,255,255,0.022)' }} />
            <div className="absolute h-[860px] w-px" style={{ backgroundColor: 'rgba(255,255,255,0.022)' }} />
          </div>

          {/* — Grid texture — */}
          <div
            className="absolute inset-0 pointer-events-none"
            style={{
              backgroundImage: 'linear-gradient(rgba(255,255,255,0.017) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,0.017) 1px, transparent 1px)',
              backgroundSize: '56px 56px',
            }}
          />

          {/* — Hero content — */}
          <div
            className="relative z-10 text-center max-w-[800px] mx-auto"
            style={{ animation: 'riseIn 0.9s ease both' }}
          >
            {/* Status badge */}
            <div
              className="inline-flex items-center gap-2 px-4 py-1.5 rounded-full text-xs tracking-wide mb-9"
              style={{
                border: `1px solid ${T.accentBd}`,
                backgroundColor: T.accentBg,
                color: T.accentSoft,
              }}
            >
              <span
                className="block w-1.5 h-1.5 rounded-full"
                style={{ backgroundColor: T.accent, animation: 'blink 2.2s ease-in-out infinite' }}
              />
              Cross-modal SAR intelligence · Private beta
            </div>

            {/* H1 — 62–72 px, weight 500, matching GT Walsheim spec */}
            <h1
              className="font-medium text-white mb-6"
              style={{
                ...headingFont,
                fontSize: 'clamp(44px, 8.5vw, 72px)',
                lineHeight: 1.07,
                letterSpacing: '-0.026em',
              }}
            >
              See through every cloud.
              <br />
              <span style={{ color: T.accent }}>Find every target.</span>
            </h1>

            {/* Sub-heading */}
            <p
              className="mx-auto mb-11 leading-[1.78]"
              style={{ fontSize: '16px', color: T.textMuted, maxWidth: '560px' }}
            >
              A multimodal retrieval system that maps SAR radar backscatter and optical
              imagery into a shared latent space — enabling real-time, sensor-agnostic
              satellite search through storms, darkness, and denied conditions.
            </p>

            {/* CTA buttons */}
            <div className="flex items-center justify-center gap-3 flex-wrap">
              <button
                onClick={goLogin}
                className="px-7 rounded-[10px] border-0 text-white font-medium cursor-pointer transition-all duration-150 active:scale-[0.97]"
                style={{ fontSize: '14px', backgroundColor: T.accent, padding: '13px 28px' }}
                onMouseEnter={e => (e.currentTarget.style.backgroundColor = '#007ecc')}
                onMouseLeave={e => (e.currentTarget.style.backgroundColor = T.accent)}
              >
                Request Access
              </button>
              <button
                onClick={goLogin}
                className="rounded-[10px] cursor-pointer bg-transparent transition-all duration-150 hover:text-white"
                style={{
                  fontSize: '14px',
                  color: T.textMuted,
                  border: '1px solid rgba(255,255,255,0.13)',
                  padding: '13px 28px',
                }}
                onMouseEnter={e => (e.currentTarget.style.borderColor = 'rgba(255,255,255,0.28)')}
                onMouseLeave={e => (e.currentTarget.style.borderColor = 'rgba(255,255,255,0.13)')}
              >
                Sign In →
              </button>
            </div>
          </div>
        </section>

        {/* ══════════════════════════════════════
            STAT STRIP
        ══════════════════════════════════════ */}
        <div
          className="flex justify-center gap-20 px-10 py-8 flex-wrap"
          style={{ borderTop: `1px solid ${T.borderSub}`, borderBottom: `1px solid ${T.borderSub}` }}
        >
          {STATS.map(s => (
            <div key={s.val} className="text-center">
              <div
                className="font-medium text-white"
                style={{ ...headingFont, fontSize: '30px', letterSpacing: '-0.015em' }}
              >
                {s.val}
              </div>
              <div
                className="mt-1 text-xs tracking-wide"
                style={{ color: T.textFaint }}
              >
                {s.label}
              </div>
            </div>
          ))}
        </div>

        {/* ══════════════════════════════════════
            FEATURES
        ══════════════════════════════════════ */}
        <section className="max-w-[1100px] mx-auto px-10 py-24">
          <p style={eyebrow}>Capabilities</p>
          <h2 style={{ ...sectionHeading, marginBottom: '52px' }}>
            Intelligence that never goes dark
          </h2>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-5">
            {FEATURES.map(f => (
              <div
                key={f.title}
                className="rounded-xl p-7 transition-all duration-200 cursor-default"
                style={{ border: `1px solid ${T.border}`, backgroundColor: T.card }}
                onMouseEnter={e => (e.currentTarget.style.borderColor = 'rgba(255,255,255,0.15)')}
                onMouseLeave={e => (e.currentTarget.style.borderColor = T.border)}
              >
                <p
                  style={{ fontSize: '10px', letterSpacing: '0.15em', textTransform: 'uppercase', color: T.accent, marginBottom: '14px' }}
                >
                  {f.eyebrow}
                </p>
                <h3
                  className="font-medium text-white mb-3"
                  style={{ ...headingFont, fontSize: '17px', letterSpacing: '-0.012em' }}
                >
                  {f.title}
                </h3>
                <p style={{ fontSize: '13px', color: T.textMuted, lineHeight: '1.78' }}>{f.body}</p>
              </div>
            ))}
          </div>
        </section>

        {/* ══════════════════════════════════════
            USE CASES
        ══════════════════════════════════════ */}
        <section className="max-w-[1100px] mx-auto px-10 pb-24">
          <p style={eyebrow}>Operator Scenarios</p>
          <h2 style={{ ...sectionHeading, marginBottom: '48px' }}>Built for real conditions</h2>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
            {USE_CASES.map(uc => (
              <div
                key={uc.tag}
                className="rounded-xl p-8"
                style={{ border: `1px solid ${T.border}`, backgroundColor: T.card }}
              >
                {/* Tag + modality badge */}
                <div className="flex items-center justify-between mb-5">
                  <span style={{ fontSize: '11px', letterSpacing: '0.12em', textTransform: 'uppercase', color: T.textGhost }}>
                    {uc.tag}
                  </span>
                  <span
                    className="text-xs px-3 py-0.5 rounded-full"
                    style={{
                      border: `1px solid ${T.accentBd}`,
                      backgroundColor: T.accentBg,
                      color: T.accentSoft,
                      letterSpacing: '0.04em',
                    }}
                  >
                    {uc.modality}
                  </span>
                </div>

                <h3
                  className="font-medium text-white mb-4"
                  style={{ ...headingFont, fontSize: '20px', letterSpacing: '-0.015em' }}
                >
                  {uc.title}
                </h3>

                {/* Query mock — monospace terminal feel */}
                <div
                  className="flex items-center gap-2 px-4 py-2.5 rounded-lg mb-5 font-mono"
                  style={{
                    border: `1px solid ${T.border}`,
                    backgroundColor: 'rgba(0,153,255,0.05)',
                    fontSize: '12px',
                  }}
                >
                  <span style={{ color: T.accent, flexShrink: 0 }}>›</span>
                  <span style={{ color: 'rgba(255,255,255,0.65)' }}>{uc.prompt}</span>
                </div>

                <p style={{ fontSize: '13px', color: T.textMuted, lineHeight: '1.8' }}>{uc.body}</p>
              </div>
            ))}
          </div>
        </section>

        {/* ══════════════════════════════════════
            PIPELINE
        ══════════════════════════════════════ */}
        <section
          className="py-24 px-10"
          style={{ borderTop: `1px solid ${T.borderSub}` }}
        >
          <div className="max-w-[1100px] mx-auto">
            <p style={eyebrow}>Ingestion Pipeline</p>
            <h2 style={{ ...sectionHeading, marginBottom: '52px' }}>
              From raw SAR to grounded answer
            </h2>

            {/* Grid — joined cells with shared borders */}
            <div className="grid grid-cols-2 md:grid-cols-4">
              {PIPELINE.map((p, i) => (
                <div
                  key={p.step}
                  className="p-7 relative"
                  style={{
                    borderTop: `1px solid rgba(255,255,255,0.09)`,
                    borderBottom: `1px solid rgba(255,255,255,0.09)`,
                    borderRight: `1px solid rgba(255,255,255,0.09)`,
                    borderLeft: i === 0 ? `1px solid rgba(255,255,255,0.09)` : 'none',
                  }}
                >
                  {/* Accent tick on top border */}
                  <div
                    className="absolute top-0 left-0 h-px w-10"
                    style={{ backgroundColor: T.accent, opacity: 0.75 }}
                  />

                  <div
                    className="font-mono mb-3"
                    style={{ fontSize: '11px', color: T.textGhost, letterSpacing: '0.08em' }}
                  >
                    {String(i + 1).padStart(2, '0')}
                  </div>
                  <div
                    className="font-medium text-white mb-2.5"
                    style={{ ...headingFont, fontSize: '16px' }}
                  >
                    {p.step}
                  </div>
                  <p style={{ fontSize: '12px', color: T.textFaint, lineHeight: '1.72' }}>{p.desc}</p>
                </div>
              ))}
            </div>
          </div>
        </section>

        {/* ══════════════════════════════════════
            CTA STRIP
        ══════════════════════════════════════ */}
        <section className="px-10 pb-20">
          <div
            className="max-w-[1100px] mx-auto rounded-2xl px-12 py-16 text-center"
            style={{
              border: '1px solid rgba(0,153,255,0.15)',
              background: 'linear-gradient(135deg, rgba(0,153,255,0.08) 0%, rgba(0,0,0,0.0) 70%)',
            }}
          >
            <h2
              className="font-medium text-white mb-4"
              style={{ ...headingFont, fontSize: 'clamp(24px, 4vw, 36px)', letterSpacing: '-0.022em' }}
            >
              Ready to query beyond the clouds?
            </h2>
            <p
              className="mx-auto mb-10"
              style={{ fontSize: '14px', color: T.textMuted, maxWidth: '460px', lineHeight: 1.78 }}
            >
              Limited seats for qualified operators and researchers. Apply for private beta access.
            </p>
            <button
              onClick={goLogin}
              className="border-0 text-white font-medium rounded-[10px] cursor-pointer transition-all duration-150 active:scale-[0.97]"
              style={{ fontSize: '14px', backgroundColor: T.accent, padding: '14px 36px' }}
              onMouseEnter={e => (e.currentTarget.style.backgroundColor = '#007ecc')}
              onMouseLeave={e => (e.currentTarget.style.backgroundColor = T.accent)}
            >
              Request Access
            </button>
          </div>
        </section>

        {/* ══════════════════════════════════════
            FOOTER
        ══════════════════════════════════════ */}
        <footer
          className="flex items-center justify-between flex-wrap gap-4 px-10 py-7"
          style={{ borderTop: `1px solid ${T.borderSub}` }}
        >
          {/* Logo */}
          <div className="flex items-center gap-2.5">
            <span className="block w-1.5 h-1.5 rounded-full" style={{ backgroundColor: T.accent }} />
            <span
              className="text-white text-[13px] font-semibold uppercase tracking-[0.16em]"
              style={headingFont}
            >
              Raikou
            </span>
          </div>

          <span style={{ fontSize: '12px', color: T.textGhost }}>
            Cross-modal satellite intelligence · All-weather retrieval
          </span>

          <div className="flex gap-6" style={{ fontSize: '12px', color: 'rgba(255,255,255,0.3)' }}>
            {['Privacy', 'Terms', 'Docs'].map(label => (
              <span
                key={label}
                className="cursor-pointer transition-colors duration-150 hover:text-white/70"
              >
                {label}
              </span>
            ))}
          </div>
        </footer>

      </div>
    </>
  );
}