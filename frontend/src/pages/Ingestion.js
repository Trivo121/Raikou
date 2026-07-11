import React, { useState, useEffect, useRef, useCallback } from 'react';

/* ─────────────────────────────────────────────
   KEYFRAME ANIMATIONS
───────────────────────────────────────────── */
const KEYFRAMES = `
  @keyframes riseIn {
    from { opacity: 0; transform: translateY(20px); }
    to   { opacity: 1; transform: translateY(0);    }
  }
  @keyframes spin {
    to { transform: rotate(360deg); }
  }
  @keyframes blink {
    0%, 100% { opacity: 1;    }
    50%       { opacity: 0.2; }
  }
  @keyframes fadeUp {
    from { opacity: 0; transform: translateY(6px); }
    to   { opacity: 1; transform: translateY(0);   }
  }
  @keyframes progressGlow {
    0%, 100% { box-shadow: 0 0 8px  rgba(0,153,255,0.45); }
    50%       { box-shadow: 0 0 18px rgba(0,153,255,0.75); }
  }
`;

/* ─────────────────────────────────────────────
   DESIGN TOKENS  (strict parity with Landing / Login)
───────────────────────────────────────────── */
const T = {
  bg: '#000000',
  text: '#ffffff',
  textMuted: 'rgba(255,255,255,0.58)',
  textFaint: 'rgba(255,255,255,0.38)',
  textGhost: 'rgba(255,255,255,0.22)',
  accent: '#0099ff',
  accentHov: '#007acc',
  accentSoft: '#88c9f7',
  accentBg: 'rgba(0,153,255,0.10)',
  accentBd: 'rgba(0,153,255,0.30)',
  border: 'rgba(255,255,255,0.07)',
  borderSub: 'rgba(255,255,255,0.05)',
  card: 'rgba(255,255,255,0.02)',
  success: '#22c55e',
  successBg: 'rgba(34,197,94,0.08)',
  successBd: 'rgba(34,197,94,0.25)',
  error: '#ef4444',
  errorBg: 'rgba(239,68,68,0.08)',
  errorBd: 'rgba(239,68,68,0.25)',
};

/* ─────────────────────────────────────────────
   TYPOGRAPHY
───────────────────────────────────────────── */
const headingFont = { fontFamily: "'Space Grotesk', sans-serif" };
const bodyFont = { fontFamily: "'Inter', -apple-system, BlinkMacSystemFont, sans-serif" };
const monoFont = { fontFamily: "'JetBrains Mono', 'Fira Code', 'Courier New', monospace" };

/* ─────────────────────────────────────────────
   PIPELINE STEP DEFINITIONS
───────────────────────────────────────────── */
const STEPS = [
  {
    id: 0,
    label: 'Uploading & Extraction',
    desc: 'Transferring files and extracting raw backscatter arrays with scene metadata.',
  },
  {
    id: 1,
    label: 'Signal Conditioning',
    desc: 'Radiometric calibration applied, then Lee speckle filter (5×5 kernel) run across all scenes.',
  },
  {
    id: 2,
    label: 'Patch Tiling',
    desc: 'Conditioned scenes sliced into 256 × 256 overlapping patches for downstream encoding.',
  },
  {
    id: 3,
    label: 'Vector Encoding & Indexing',
    desc: 'SARCLIP embeds each patch into a 512-d FP16 vector; vectors pushed to Qdrant.',
  },
];

/* ─────────────────────────────────────────────
   BACKEND CONFIG
───────────────────────────────────────────── */
const BACKEND_URL = process.env.REACT_APP_API_URL || 'http://localhost:8000';

/* ─────────────────────────────────────────────
   HELPERS
───────────────────────────────────────────── */
function formatBytes(b) {
  if (b < 1024) return `${b} B`;
  if (b < 1024 ** 2) return `${(b / 1024).toFixed(1)} KB`;
  if (b < 1024 ** 3) return `${(b / 1024 ** 2).toFixed(1)} MB`;
  return `${(b / 1024 ** 3).toFixed(2)} GB`;
}

function ts() {
  return new Date().toLocaleTimeString('en-US', {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
}

/* ─────────────────────────────────────────────
   COMPONENT
───────────────────────────────────────────── */
export default function Ingestion() {

  /* ── Fonts ── */
  useEffect(() => {
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = 'https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600&family=Inter:wght@400;500&display=swap';
    document.head.appendChild(link);
    return () => document.head.removeChild(link);
  }, []);

  /* ── State ── */
  const [view, setView] = useState('upload');  // upload | processing | success | error
  const [files, setFiles] = useState([]);
  const [isDragOver, setIsDragOver] = useState(false);
  const [progress, setProgress] = useState(0);
  const [currentStep, setCurrentStep] = useState(0);
  const [completedSteps, setCompletedSteps] = useState([]);
  const [logs, setLogs] = useState([]);
  const [countdown, setCountdown] = useState(5);
  const [errorDetail, setErrorDetail] = useState('');

  const fileInputRef = useRef(null);
  const logEndRef = useRef(null);
  const timers = useRef([]);

  /* ── Auto-scroll terminal ── */
  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [logs]);

  /* ── File helpers ── */
  const stageFiles = (incoming) => {
    const list = Array.from(incoming);
    setFiles(prev => {
      const seen = new Set(prev.map(f => f.name));
      return [...prev, ...list.filter(f => !seen.has(f.name))];
    });
  };

  const removeFile = (name) => setFiles(prev => prev.filter(f => f.name !== name));

  /* ── Drag & drop ── */
  const onDragOver = (e) => { e.preventDefault(); setIsDragOver(true); };
  const onDragLeave = () => setIsDragOver(false);
  const onDrop = (e) => {
    e.preventDefault();
    setIsDragOver(false);
    stageFiles(e.dataTransfer.files);
  };

  /* ── Processing simulation ── */
  const pushLog = useCallback((msg) => {
    setLogs(prev => [...prev, { ts: ts(), msg }]);
  }, []);

  const clearTimers = () => {
    timers.current.forEach(clearTimeout);
    timers.current = [];
  };

  /* ── Auto-Resume on Mount ── */
  useEffect(() => {
    const activeSession = localStorage.getItem('raikou_session_id');
    if (activeSession) {
      checkAndResumeSession(activeSession);
    }
  }, []);

  const checkAndResumeSession = async (sessionId) => {
    try {
      const statusRes = await fetch(`${BACKEND_URL}/api/v1/processing/status/${sessionId}`);
      if (statusRes.ok) {
        const data = await statusRes.json();
        if (data.status === 'processing') {
          setView('processing');
          setCompletedSteps(prev => Array.from(new Set([...prev, 0, 1])));
          setCurrentStep(2);
          pushLog(`Resuming active session: ${sessionId}`);
          startPolling(sessionId, data.estimated_patches || 1);
        } else if (data.status === 'completed') {
          setView('success');
        }
      }
    } catch (e) {
      console.warn("Failed to check active session:", e);
    }
  };

  const startPolling = (sessionId, estimated) => {
    let encoded = 0;
    const pollInterval = setInterval(async () => {
      try {
        const statusRes = await fetch(`${BACKEND_URL}/api/v1/processing/status/${sessionId}`);
        if (statusRes.ok) {
          const statusData = await statusRes.json();
          encoded = statusData.encoded_patches || 0;
          
          const currentProgress = 20 + Math.min(80, Math.round((encoded / estimated) * 80));
          setProgress(currentProgress);
          
          if (encoded > 0) {
            setCompletedSteps(prev => Array.from(new Set([...prev, 2])));
            setCurrentStep(3);
          }
          
          pushLog(`Encoding batch... ${encoded} / ${estimated} vectors generated.`);
          
          if (statusData.status === 'completed' || encoded >= estimated) {
            clearInterval(pollInterval);
            setCompletedSteps(prev => Array.from(new Set([...prev, 3])));
            setProgress(100);
            pushLog(`✓ Ingestion complete — ${encoded} patches indexed to Qdrant.`);
            setTimeout(() => setView('success'), 1000);
          }
        }
      } catch (e) {
        console.warn("Polling error:", e);
      }
    }, 2000);
    timers.current.push(pollInterval);
  };

  const startIngestion = async () => {
    if (files.length === 0) return;
    clearTimers();
    setView('processing');
    setProgress(0);
    setCurrentStep(0);
    setCompletedSteps([]);
    setLogs([]);
    
    pushLog('Initializing ingestion pipeline…');
    pushLog('File transfer in progress — uploading to backend.');

    const formData = new FormData();
    files.forEach(f => formData.append('files', f));

    try {
      const uploadRes = await fetch(`${BACKEND_URL}/api/v1/ingestion/upload`, {
        method: 'POST',
        body: formData,
      });

      if (!uploadRes.ok) throw new Error(`Upload failed: ${uploadRes.statusText}`);
      const uploadData = await uploadRes.json();
      const sessionId = uploadData.session_data.session_id;
      
      pushLog(`File uploaded successfully. Session ID: ${sessionId}`);
      setCompletedSteps(prev => [...prev, 0]);
      setCurrentStep(1);
      setProgress(10);
      pushLog('Conditioning and Tiling...');
      
      localStorage.setItem('raikou_session_id', sessionId);

      const processRes = await fetch(`${BACKEND_URL}/api/v1/processing/${sessionId}`, {
        method: 'POST',
      });
      if (!processRes.ok) throw new Error('Failed to trigger processing');
      const processData = await processRes.json();
      const estimated = processData.estimated_patches || 1;
      
      pushLog(`Processing started. Estimated patches: ${estimated}`);
      setCompletedSteps(prev => [...prev, 1]);
      setCurrentStep(2);
      setProgress(20);
      
      startPolling(sessionId, estimated);
    } catch (e) {
      setErrorDetail(e.message);
      setView('error');
    }
  };

  const simulateError = () => {
    clearTimers();
    setErrorDetail('Processing error encountered during vectorization. Please check backend logs.');
    setView('error');
  };

  const resumeIngestion = () => {
    setErrorDetail('');
    const activeSession = localStorage.getItem('raikou_session_id');
    if (activeSession) {
      checkAndResumeSession(activeSession);
    } else {
      startIngestion();
    }
  };

  const resetUpload = () => {
    clearTimers();
    setFiles([]);
    setLogs([]);
    setProgress(0);
    setCompletedSteps([]);
    setCurrentStep(0);
    setView('upload');
  };

  /* ── Redirect countdown ── */
  useEffect(() => {
    if (view !== 'success') return;
    setCountdown(5);
    const iv = setInterval(() => {
      setCountdown(prev => {
        if (prev <= 1) { clearInterval(iv); goWorkspace(); return 0; }
        return prev - 1;
      });
    }, 1000);
    return () => clearInterval(iv);
  }, [view]); // eslint-disable-line

  const goWorkspace = () => {
    window.history.pushState({}, '', '/project/new');
    window.dispatchEvent(new PopStateEvent('popstate'));
  };

  const goBack = () => {
    window.history.pushState({}, '', '/dashboard');
    window.dispatchEvent(new PopStateEvent('popstate'));
  };

  const hasFiles = files.length > 0;

  /* ════════════════════════════════════════════
     RENDER
  ════════════════════════════════════════════ */
  return (
    <>
      <style>{KEYFRAMES}</style>

      <div
        style={{
          ...bodyFont,
          backgroundColor: T.bg,
          color: T.text,
          minHeight: '100vh',
          fontSize: '14px',
          lineHeight: '1.65',
          overflowX: 'hidden',
        }}
      >

        {/* ════════════════════════════════════
            NAV
        ════════════════════════════════════ */}
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
          <button
            onClick={goBack}
            style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0 }}
            className="flex items-center gap-2.5 select-none transition-opacity hover:opacity-75"
          >
            <span
              className="block w-2 h-2 rounded-full"
              style={{ backgroundColor: T.accent, boxShadow: '0 0 10px rgba(0,153,255,0.9)' }}
            />
            <span className="text-white text-sm font-semibold uppercase tracking-[0.18em]" style={headingFont}>
              Raikou
            </span>
          </button>

          {/* Breadcrumb */}
          <div className="hidden md:flex items-center gap-2" style={{ fontSize: '13px', color: T.textGhost }}>
            <span>Dashboard</span>
            <span style={{ color: T.textGhost, margin: '0 2px' }}>›</span>
            <span style={{ color: T.textMuted }}>SAR Data Ingestion</span>
          </div>

          {/* Status pill */}
          {view === 'processing' && (
            <div
              className="flex items-center gap-2 px-3 py-1 rounded-full"
              style={{ border: `1px solid ${T.accentBd}`, backgroundColor: T.accentBg, fontSize: '11px', color: T.accentSoft }}
            >
              <span
                className="block w-1.5 h-1.5 rounded-full"
                style={{ backgroundColor: T.accent, animation: 'blink 1.8s ease-in-out infinite' }}
              />
              Processing…
            </div>
          )}
          {view === 'upload' && <div style={{ width: '96px' }} />}
          {view === 'success' && (
            <div
              className="flex items-center gap-1.5 px-3 py-1 rounded-full"
              style={{ border: `1px solid ${T.successBd}`, backgroundColor: T.successBg, fontSize: '11px', color: T.success }}
            >
              ✓ Complete
            </div>
          )}
          {view === 'error' && (
            <div
              className="flex items-center gap-1.5 px-3 py-1 rounded-full"
              style={{ border: `1px solid ${T.errorBd}`, backgroundColor: T.errorBg, fontSize: '11px', color: T.error }}
            >
              ⚠ Paused
            </div>
          )}
        </nav>

        {/* ════════════════════════════════════
            MAIN
        ════════════════════════════════════ */}
        <main className="pt-[60px] min-h-screen flex flex-col">


          {/* ─────────────────────────────────
              UPLOAD VIEW
          ───────────────────────────────── */}
          {view === 'upload' && (
            <div
              className="flex-1 flex flex-col max-w-[680px] mx-auto w-full px-6 py-16"
              style={{ animation: 'riseIn 0.7s ease both' }}
            >

              {/* Page header */}
              <div className="mb-10">
                <p style={{ fontSize: '11px', letterSpacing: '0.18em', textTransform: 'uppercase', color: T.accent, marginBottom: '12px' }}>
                  Ingestion Pipeline
                </p>
                <h1
                  className="font-medium text-white mb-3"
                  style={{ ...headingFont, fontSize: 'clamp(28px, 4vw, 38px)', letterSpacing: '-0.022em', lineHeight: 1.1 }}
                >
                  SAR Data Ingestion
                </h1>
                <p style={{ fontSize: '14px', color: T.textMuted, lineHeight: 1.78, maxWidth: '520px' }}>
                  Upload raw SAR scenes to extract, condition, tile, and index them into the Qdrant
                  vector store. Accepts{' '}
                  <code style={{ ...monoFont, color: T.accentSoft, fontSize: '12px' }}>.tiff</code>,{' '}
                  <code style={{ ...monoFont, color: T.accentSoft, fontSize: '12px' }}>.safe</code> folders, and{' '}
                  <code style={{ ...monoFont, color: T.accentSoft, fontSize: '12px' }}>.h5</code> files.
                </p>
              </div>

              {/* ── Dropzone ── */}
              <div
                onDragOver={onDragOver}
                onDragLeave={onDragLeave}
                onDrop={onDrop}
                onClick={() => fileInputRef.current?.click()}
                className="relative flex flex-col items-center justify-center rounded-2xl mb-4 transition-all duration-200 cursor-pointer"
                style={{
                  minHeight: '220px',
                  border: `2px dashed ${isDragOver ? T.accent : 'rgba(255,255,255,0.12)'}`,
                  backgroundColor: isDragOver ? T.accentBg : T.card,
                  boxShadow: isDragOver ? `0 0 0 1px ${T.accentBd}, 0 0 30px rgba(0,153,255,0.07)` : 'none',
                }}
              >
                {/* Upload icon */}
                <div
                  className="mb-5 w-12 h-12 rounded-xl flex items-center justify-center"
                  style={{
                    border: `1px solid ${isDragOver ? T.accentBd : T.border}`,
                    backgroundColor: isDragOver ? T.accentBg : 'rgba(255,255,255,0.025)',
                    transition: 'all 0.2s ease',
                  }}
                >
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none"
                    stroke={isDragOver ? T.accent : 'rgba(255,255,255,0.38)'}
                    strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"
                  >
                    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                    <polyline points="17 8 12 3 7 8" />
                    <line x1="12" y1="3" x2="12" y2="15" />
                  </svg>
                </div>

                <p className="font-medium text-white mb-1.5" style={{ ...headingFont, fontSize: '15px' }}>
                  {isDragOver ? 'Drop SAR files here' : 'Drag & drop SAR scenes here'}
                </p>
                <p style={{ fontSize: '13px', color: T.textFaint, marginBottom: '22px' }}>
                  or browse your machine
                </p>

                <button
                  onClick={e => { e.stopPropagation(); fileInputRef.current?.click(); }}
                  className="px-5 py-2 rounded-lg font-medium transition-all duration-150 active:scale-[0.97]"
                  style={{ fontSize: '13px', backgroundColor: T.accent, color: T.text, border: 'none', cursor: 'pointer' }}
                  onMouseEnter={e => (e.currentTarget.style.backgroundColor = T.accentHov)}
                  onMouseLeave={e => (e.currentTarget.style.backgroundColor = T.accent)}
                >
                  Browse Files
                </button>

                <input
                  ref={fileInputRef}
                  type="file"
                  multiple
                  accept=".tif,.tiff,.h5,.hdf5,.SAFE,.zip"
                  style={{ display: 'none' }}
                  onChange={e => stageFiles(e.target.files)}
                />
              </div>

              {/* ── Google Drive button ── */}
              <button
                className="flex items-center justify-center gap-2.5 w-full py-3 rounded-xl mb-8 transition-all duration-150 active:scale-[0.98]"
                style={{ border: `1px solid ${T.border}`, backgroundColor: 'transparent', color: T.textMuted, fontSize: '13px', cursor: 'pointer' }}
                onMouseEnter={e => { e.currentTarget.style.backgroundColor = 'rgba(255,255,255,0.03)'; e.currentTarget.style.color = T.text; e.currentTarget.style.borderColor = 'rgba(255,255,255,0.14)'; }}
                onMouseLeave={e => { e.currentTarget.style.backgroundColor = 'transparent'; e.currentTarget.style.color = T.textMuted; e.currentTarget.style.borderColor = T.border; }}
                onClick={() => alert('Connect your Google Drive API credentials to enable cloud import.')}
              >
                {/* Google Drive colour icon */}
                <svg width="16" height="14" viewBox="0 0 87.3 78" xmlns="http://www.w3.org/2000/svg">
                  <path d="m6.6 66.85 3.85 6.65c.8 1.4 1.95 2.5 3.3 3.3l13.75-23.8h-27.5c0 1.55.4 3.1 1.2 4.5z" fill="#0066da" />
                  <path d="m43.65 25-13.75-23.8c-1.35.8-2.5 1.9-3.3 3.3l-25.4 44a9.06 9.06 0 0 0-1.2 4.5h27.5z" fill="#00ac47" />
                  <path d="m73.55 76.8c1.35-.8 2.5-1.9 3.3-3.3l1.6-2.75 7.65-13.25c.8-1.4 1.2-2.95 1.2-4.5h-27.5l5.85 11.5z" fill="#ea4335" />
                  <path d="m43.65 25 13.75-23.8c-1.35-.8-2.9-1.2-4.5-1.2h-18.5c-1.6 0-3.15.45-4.5 1.2z" fill="#00832d" />
                  <path d="m59.8 53h-32.3l-13.75 23.8c1.35.8 2.9 1.2 4.5 1.2h50.8c1.6 0 3.15-.45 4.5-1.2z" fill="#2684fc" />
                  <path d="m73.4 26.5-12.7-22c-.8-1.4-1.95-2.5-3.3-3.3l-13.75 23.8 16.15 28h27.45c0-1.55-.4-3.1-1.2-4.5z" fill="#ffba00" />
                </svg>
                Import from Google Drive
              </button>

              {/* ── File staging list ── */}
              {hasFiles && (
                <div className="mb-8" style={{ animation: 'fadeUp 0.3s ease both' }}>
                  <div className="flex items-center justify-between mb-3">
                    <span style={{ fontSize: '11px', letterSpacing: '0.14em', textTransform: 'uppercase', color: T.textGhost }}>
                      Staged — {files.length} file{files.length !== 1 ? 's' : ''}
                    </span>
                    <button
                      onClick={() => setFiles([])}
                      style={{ fontSize: '11px', color: T.error, background: 'none', border: 'none', cursor: 'pointer', opacity: 0.65 }}
                      onMouseEnter={e => (e.currentTarget.style.opacity = '1')}
                      onMouseLeave={e => (e.currentTarget.style.opacity = '0.65')}
                    >
                      Clear all
                    </button>
                  </div>

                  <div className="flex flex-col gap-2">
                    {files.map(file => (
                      <div
                        key={file.name}
                        className="flex items-center justify-between px-4 py-3 rounded-xl"
                        style={{ border: `1px solid ${T.border}`, backgroundColor: T.card }}
                      >
                        <div className="flex items-center gap-3 min-w-0">
                          {/* File icon */}
                          <div
                            className="w-8 h-8 rounded-lg flex items-center justify-center shrink-0"
                            style={{ backgroundColor: T.accentBg, border: `1px solid ${T.accentBd}` }}
                          >
                            <svg width="11" height="13" viewBox="0 0 11 14" fill="none" stroke={T.accent} strokeWidth="1.4" strokeLinecap="round">
                              <path d="M1.5 1h6l2 2v10h-8V1z" />
                              <path d="M7.5 1v2.5h2" />
                            </svg>
                          </div>
                          <div className="min-w-0">
                            <p className="text-white truncate" style={{ fontSize: '13px', fontWeight: 500 }}>
                              {file.name}
                            </p>
                            <p style={{ fontSize: '11px', color: T.textGhost }}>
                              {formatBytes(file.size)}
                            </p>
                          </div>
                        </div>

                        {/* Remove button */}
                        <button
                          onClick={() => removeFile(file.name)}
                          className="ml-3 shrink-0 w-6 h-6 flex items-center justify-center rounded-md transition-all"
                          style={{ background: 'none', border: 'none', cursor: 'pointer', color: T.textGhost, fontSize: '13px' }}
                          onMouseEnter={e => { e.currentTarget.style.backgroundColor = T.errorBg; e.currentTarget.style.color = T.error; }}
                          onMouseLeave={e => { e.currentTarget.style.backgroundColor = 'transparent'; e.currentTarget.style.color = T.textGhost; }}
                        >
                          ✕
                        </button>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* ── Start ingestion CTA ── */}
              <button
                onClick={startIngestion}
                disabled={!hasFiles}
                className="w-full py-4 rounded-xl font-semibold text-white transition-all duration-200 active:scale-[0.98]"
                style={{
                  ...headingFont,
                  fontSize: '15px',
                  letterSpacing: '-0.01em',
                  backgroundColor: hasFiles ? T.accent : 'rgba(255,255,255,0.05)',
                  color: hasFiles ? T.text : T.textGhost,
                  border: `1px solid ${hasFiles ? T.accentBd : T.border}`,
                  cursor: hasFiles ? 'pointer' : 'not-allowed',
                }}
                onMouseEnter={e => { if (hasFiles) e.currentTarget.style.backgroundColor = T.accentHov; }}
                onMouseLeave={e => { if (hasFiles) e.currentTarget.style.backgroundColor = hasFiles ? T.accent : 'rgba(255,255,255,0.05)'; }}
              >
                {hasFiles
                  ? `Start Ingestion — ${files.length} file${files.length !== 1 ? 's' : ''}`
                  : 'Add files to begin'}
              </button>

            </div>
          )}


          {/* ─────────────────────────────────
              PROCESSING VIEW
          ───────────────────────────────── */}
          {view === 'processing' && (
            <div
              className="flex-1 flex flex-col max-w-[760px] mx-auto w-full px-6 py-12"
              style={{ animation: 'riseIn 0.5s ease both' }}
            >

              {/* Global progress bar */}
              <div className="mb-10">
                <div className="flex items-center justify-between mb-3">
                  <span style={{ ...headingFont, fontSize: '13px', color: T.textMuted, fontWeight: 500 }}>
                    Overall Progress
                  </span>
                  <span style={{ ...monoFont, fontSize: '13px', color: T.accent, fontWeight: 600 }}>
                    {progress}%
                  </span>
                </div>
                <div
                  className="w-full rounded-full overflow-hidden"
                  style={{ height: '5px', backgroundColor: 'rgba(255,255,255,0.06)' }}
                >
                  <div
                    className="h-full rounded-full transition-all duration-300"
                    style={{
                      width: `${progress}%`,
                      background: `linear-gradient(90deg, ${T.accent}, ${T.accentSoft})`,
                      animation: 'progressGlow 2.4s ease-in-out infinite',
                    }}
                  />
                </div>
              </div>

              {/* ── Pipeline stepper ── */}
              <div
                className="rounded-2xl overflow-hidden mb-8"
                style={{ border: `1px solid ${T.border}` }}
              >
                {STEPS.map((step, i) => {
                  const isActive = currentStep === i && !completedSteps.includes(i);
                  const isCompleted = completedSteps.includes(i);
                  const isPending = !isActive && !isCompleted;

                  return (
                    <div
                      key={step.id}
                      className="flex items-start gap-4 px-6 py-5 transition-colors duration-500"
                      style={{
                        borderBottom: i < STEPS.length - 1 ? `1px solid ${T.borderSub}` : 'none',
                        backgroundColor: isActive ? T.accentBg : 'transparent',
                      }}
                    >
                      {/* Step indicator */}
                      <div
                        className="shrink-0 mt-0.5 w-7 h-7 rounded-full flex items-center justify-center transition-all duration-400"
                        style={{
                          border: `1.5px solid ${isCompleted ? T.success : isActive ? T.accent : 'rgba(255,255,255,0.1)'}`,
                          backgroundColor: isCompleted ? T.successBg : isActive ? T.accentBg : 'transparent',
                        }}
                      >
                        {isCompleted ? (
                          <svg width="11" height="11" viewBox="0 0 12 12" fill="none">
                            <path d="M2 6l3 3 5-5" stroke={T.success} strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" />
                          </svg>
                        ) : isActive ? (
                          <div
                            className="w-[11px] h-[11px] rounded-full"
                            style={{
                              border: `2px solid ${T.accent}`,
                              borderTopColor: 'transparent',
                              animation: 'spin 0.75s linear infinite',
                            }}
                          />
                        ) : (
                          <span style={{ ...monoFont, fontSize: '9px', color: T.textGhost, fontWeight: 700 }}>
                            {String(i + 1).padStart(2, '0')}
                          </span>
                        )}
                      </div>

                      {/* Step text */}
                      <div className="flex-1 min-w-0">
                        <p
                          className="font-medium mb-0.5 transition-colors duration-400"
                          style={{
                            ...headingFont,
                            fontSize: '14px',
                            color: isCompleted ? T.success : isActive ? T.text : T.textGhost,
                          }}
                        >
                          {step.label}
                        </p>
                        <p style={{ fontSize: '12px', color: isPending ? 'rgba(255,255,255,0.16)' : T.textFaint, lineHeight: 1.6 }}>
                          {step.desc}
                        </p>
                      </div>

                      {/* Status tag */}
                      <div className="shrink-0 mt-1">
                        {isCompleted && (
                          <span style={{ ...monoFont, fontSize: '10px', color: T.success }}>done</span>
                        )}
                        {isActive && (
                          <span style={{ ...monoFont, fontSize: '10px', color: T.accent, animation: 'blink 2s ease-in-out infinite' }}>
                            active
                          </span>
                        )}
                        {isPending && (
                          <span style={{ ...monoFont, fontSize: '10px', color: T.textGhost }}>queued</span>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>

              {/* ── Live terminal ── */}
              <div className="rounded-2xl overflow-hidden" style={{ border: `1px solid ${T.border}` }}>
                {/* Terminal chrome */}
                <div
                  className="flex items-center justify-between px-5 py-3"
                  style={{ borderBottom: `1px solid ${T.borderSub}`, backgroundColor: 'rgba(255,255,255,0.015)' }}
                >
                  <div className="flex items-center gap-3">
                    <div className="flex gap-1.5">
                      <span className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: 'rgba(255,95,87,0.65)' }} />
                      <span className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: 'rgba(255,188,46,0.65)' }} />
                      <span className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: 'rgba(40,200,64,0.65)' }} />
                    </div>
                    <span style={{ ...monoFont, fontSize: '11px', color: T.textGhost }}>pipeline.log</span>
                  </div>
                  <span
                    className="w-1.5 h-1.5 rounded-full"
                    style={{ backgroundColor: T.success, animation: 'blink 1.6s ease-in-out infinite' }}
                  />
                </div>
                {/* Log body */}
                <div
                  className="overflow-y-auto"
                  style={{ height: '220px', backgroundColor: '#04040a', padding: '14px 20px' }}
                >
                  {logs.length === 0 && (
                    <p style={{ ...monoFont, fontSize: '12px', color: T.textGhost }}>
                      Awaiting pipeline start…
                    </p>
                  )}
                  {logs.map((entry, i) => (
                    <div
                      key={i}
                      className="flex gap-3 mb-1.5"
                      style={{ animation: 'fadeUp 0.18s ease both' }}
                    >
                      <span style={{ ...monoFont, fontSize: '11px', color: T.textGhost, flexShrink: 0 }}>
                        {entry.ts}
                      </span>
                      <span
                        style={{
                          ...monoFont,
                          fontSize: '12px',
                          lineHeight: 1.6,
                          color: entry.msg.startsWith('✓')
                            ? T.success
                            : 'rgba(255,255,255,0.70)',
                        }}
                      >
                        {entry.msg}
                      </span>
                    </div>
                  ))}
                  <div ref={logEndRef} />
                </div>
              </div>

              {/* Dev error trigger */}
              <div className="mt-6 text-center">
                <button
                  onClick={simulateError}
                  style={{ ...monoFont, fontSize: '11px', color: T.textGhost, background: 'none', border: 'none', cursor: 'pointer', opacity: 0.5 }}
                  onMouseEnter={e => { e.currentTarget.style.opacity = '1'; e.currentTarget.style.color = T.error; }}
                  onMouseLeave={e => { e.currentTarget.style.opacity = '0.5'; e.currentTarget.style.color = T.textGhost; }}
                >
                  dev — simulate network failure
                </button>
              </div>

            </div>
          )}


          {/* ─────────────────────────────────
              ERROR / PAUSED VIEW
          ───────────────────────────────── */}
          {view === 'error' && (
            <div
              className="flex-1 flex flex-col items-center justify-center max-w-[600px] mx-auto w-full px-6 py-16"
              style={{ animation: 'riseIn 0.5s ease both' }}
            >

              {/* Error banner */}
              <div
                className="w-full rounded-2xl p-7 mb-6"
                style={{ border: `1px solid ${T.errorBd}`, backgroundColor: T.errorBg }}
              >
                <div className="flex items-start gap-4">
                  <div
                    className="w-10 h-10 rounded-xl flex items-center justify-center shrink-0"
                    style={{ border: `1px solid ${T.errorBd}`, backgroundColor: 'rgba(239,68,68,0.12)' }}
                  >
                    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke={T.error} strokeWidth="1.8" strokeLinecap="round">
                      <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
                      <line x1="12" y1="9" x2="12" y2="13" />
                      <circle cx="12" cy="17" r="0.5" fill={T.error} />
                    </svg>
                  </div>
                  <div>
                    <p className="font-semibold mb-1.5" style={{ ...headingFont, fontSize: '16px', color: T.error }}>
                      Processing Paused
                    </p>
                    <p style={{ fontSize: '13px', color: T.textMuted, lineHeight: 1.72 }}>
                      {errorDetail || 'An unexpected error occurred. Your progress has been checkpointed.'}
                    </p>
                  </div>
                </div>
              </div>

              {/* Checkpoint card */}
              <div
                className="w-full rounded-2xl p-6 mb-8"
                style={{ border: `1px solid ${T.border}`, backgroundColor: T.card }}
              >
                <p style={{ fontSize: '11px', letterSpacing: '0.14em', textTransform: 'uppercase', color: T.textGhost, marginBottom: '14px' }}>
                  Checkpoint Status
                </p>
                <div className="flex flex-col gap-3.5">
                  {[
                    { label: 'Completed steps', value: `${completedSteps.length} / 4` },
                    { label: 'Current progress', value: `${progress}%` },
                  ].map(({ label, value }) => (
                    <div key={label} className="flex items-center justify-between">
                      <span style={{ fontSize: '13px', color: T.textFaint }}>{label}</span>
                      <span style={{ ...monoFont, fontSize: '12px', color: T.textMuted }}>{value}</span>
                    </div>
                  ))}
                </div>
              </div>

              {/* Action buttons */}
              <div className="flex flex-col gap-3 w-full">
                <button
                  onClick={resumeIngestion}
                  className="w-full py-3.5 rounded-xl font-semibold transition-all duration-150 active:scale-[0.98]"
                  style={{ ...headingFont, fontSize: '14px', backgroundColor: T.accent, color: T.text, border: 'none', cursor: 'pointer' }}
                  onMouseEnter={e => (e.currentTarget.style.backgroundColor = T.accentHov)}
                  onMouseLeave={e => (e.currentTarget.style.backgroundColor = T.accent)}
                >
                  Resume from Checkpoint
                </button>
                <button
                  onClick={resetUpload}
                  className="w-full py-3 rounded-xl transition-all duration-150 active:scale-[0.98]"
                  style={{ fontSize: '13px', color: T.textMuted, border: `1px solid ${T.border}`, backgroundColor: 'transparent', cursor: 'pointer' }}
                  onMouseEnter={e => { e.currentTarget.style.color = T.text; e.currentTarget.style.borderColor = 'rgba(255,255,255,0.18)'; }}
                  onMouseLeave={e => { e.currentTarget.style.color = T.textMuted; e.currentTarget.style.borderColor = T.border; }}
                >
                  Start Over — Re-upload Files
                </button>
              </div>

            </div>
          )}


          {/* ─────────────────────────────────
              SUCCESS VIEW
          ───────────────────────────────── */}
          {view === 'success' && (
            <div
              className="flex-1 flex flex-col items-center justify-center max-w-[600px] mx-auto w-full px-6 py-16"
              style={{ animation: 'riseIn 0.6s ease both' }}
            >

              {/* Final progress bar — green */}
              <div className="w-full mb-10">
                <div className="flex items-center justify-between mb-3">
                  <span style={{ ...headingFont, fontSize: '13px', color: T.success, fontWeight: 500 }}>
                    Ingestion Complete
                  </span>
                  <span style={{ ...monoFont, fontSize: '13px', color: T.success, fontWeight: 600 }}>
                    100%
                  </span>
                </div>
                <div
                  className="w-full rounded-full overflow-hidden"
                  style={{ height: '5px', backgroundColor: 'rgba(34,197,94,0.12)' }}
                >
                  <div
                    className="h-full w-full rounded-full"
                    style={{
                      background: `linear-gradient(90deg, ${T.success}, #4ade80)`,
                      boxShadow: '0 0 14px rgba(34,197,94,0.45)',
                    }}
                  />
                </div>
              </div>

              {/* All steps — completed */}
              <div
                className="w-full rounded-2xl overflow-hidden mb-8"
                style={{ border: `1px solid ${T.successBd}`, backgroundColor: T.successBg }}
              >
                {STEPS.map((step, i) => (
                  <div
                    key={step.id}
                    className="flex items-center gap-4 px-6 py-4"
                    style={{ borderBottom: i < STEPS.length - 1 ? 'rgba(34,197,94,0.10) 1px solid' : 'none' }}
                  >
                    <div
                      className="shrink-0 w-6 h-6 rounded-full flex items-center justify-center"
                      style={{ backgroundColor: T.successBg, border: `1.5px solid ${T.success}` }}
                    >
                      <svg width="10" height="10" viewBox="0 0 12 12" fill="none">
                        <path d="M2 6l3 3 5-5" stroke={T.success} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
                      </svg>
                    </div>
                    <span style={{ fontSize: '13px', color: T.success, ...headingFont, fontWeight: 500 }}>
                      {step.label}
                    </span>
                  </div>
                ))}
              </div>

              {/* Summary card */}
              <div
                className="w-full rounded-2xl p-7 mb-8"
                style={{ border: `1px solid ${T.border}`, backgroundColor: T.card }}
              >
                <p style={{ fontSize: '11px', letterSpacing: '0.14em', textTransform: 'uppercase', color: T.textGhost, marginBottom: '18px' }}>
                  Ingestion Summary
                </p>
                <div className="grid grid-cols-2 gap-x-8 gap-y-5">
                  {[
                    { label: 'SAR scenes', value: '3' },
                    { label: 'Total patches', value: '4,500' },
                    { label: 'Vectors indexed', value: '4,500' },
                    { label: 'Vector dimensions', value: '512-d FP16' },
                    { label: 'Qdrant collection', value: '"sar_index"' },
                    { label: 'Query latency', value: '< 5 ms' },
                  ].map(({ label, value }) => (
                    <div key={label}>
                      <p style={{ fontSize: '11px', color: T.textGhost, marginBottom: '3px' }}>{label}</p>
                      <p style={{ ...monoFont, fontSize: '14px', color: T.text, fontWeight: 600 }}>{value}</p>
                    </div>
                  ))}
                </div>
              </div>

              {/* Redirect countdown */}
              <p className="mb-6" style={{ fontSize: '13px', color: T.textFaint, textAlign: 'center' }}>
                Redirecting to Project Workspace in{' '}
                <span style={{ ...monoFont, color: T.accent, fontWeight: 600 }}>{countdown}</span>
                …
              </p>

              {/* CTA */}
              <button
                onClick={goWorkspace}
                className="w-full py-4 rounded-xl font-semibold text-white transition-all duration-150 active:scale-[0.98] flex items-center justify-center gap-2.5"
                style={{ ...headingFont, fontSize: '15px', letterSpacing: '-0.01em', backgroundColor: T.success, border: 'none', cursor: 'pointer' }}
                onMouseEnter={e => (e.currentTarget.style.backgroundColor = '#16a34a')}
                onMouseLeave={e => (e.currentTarget.style.backgroundColor = T.success)}
              >
                Go to Workspace Now
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M5 12h14M12 5l7 7-7 7" />
                </svg>
              </button>

            </div>
          )}

        </main>
      </div>
    </>
  );
}