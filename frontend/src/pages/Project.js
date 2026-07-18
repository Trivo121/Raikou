import React, { useState, useEffect, useRef } from 'react';
import {
  ArrowLeft, ArrowUp, Search, Plus, Users, MapPin,
  Radar, MoreHorizontal, Download, FileText, Database, Paperclip
} from 'lucide-react';
import { getSupabase } from '../App';

/* ─────────────────────────────────────────────
   KEYFRAMES
───────────────────────────────────────────── */
const KEYFRAMES = `
  @keyframes riseIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
  @keyframes pulseDot { 0%, 100% { opacity: 1; } 50% { opacity: 0.25; } }
  @media (prefers-reduced-motion: reduce) {
    * { animation: none !important; transition: none !important; }
  }
`;

/* ─────────────────────────────────────────────
   MOCK PROJECT DATA
   (keyed to match the project ids used in Dashboard.js)
───────────────────────────────────────────── */
const PROJECTS = {
  'proj-1': {
    title: 'Primary Anything',
    theme: 'A general-purpose scratch index for testing detection prompts across mixed terrain and open water scenes.',
    sceneCount: 2, patchCount: 1800, vectorCount: 1800, indexName: 'sar_index_primary',
    files: [
      { name: 'Scene_A_test.tif', size: '412 MB' },
      { name: 'Scene_B_test.tif', size: '388 MB' },
    ],
    sources: [
      { scene: 'Scene_A_test', coords: '19.07°N, 72.87°E', confidence: 88, date: '2024-03-02', patch: '0512', label: 'Bright point return' },
      { scene: 'Scene_A_test', coords: '19.05°N, 72.90°E', confidence: 81, date: '2024-03-02', patch: '0399', label: 'Linear structure' },
      { scene: 'Scene_B_test', coords: '19.11°N, 72.84°E', confidence: 76, date: '2024-03-04', patch: '1140', label: 'Textured surface change' },
      { scene: 'Scene_B_test', coords: '19.09°N, 72.86°E', confidence: 69, date: '2024-03-04', patch: '0987', label: 'Speckle cluster' },
      { scene: 'Scene_A_test', coords: '19.06°N, 72.88°E', confidence: 62, date: '2024-03-02', patch: '0201', label: 'Weak backscatter' },
    ],
    prompts: [
      'What anomalies were detected across both scenes?',
      'Show me the highest-confidence match',
      'Are there any linear structures in the data?',
      'Summarize backscatter patterns by scene',
    ],
  },
  'proj-2': {
    title: 'S1A_IW_Rotterdam',
    theme: 'Sentinel-1A IW-mode captures over the Port of Rotterdam, tracking berth occupancy and container yard density.',
    sceneCount: 4, patchCount: 6200, vectorCount: 6200, indexName: 'sar_index_rotterdam',
    files: [
      { name: 'S1A_IW_20240512.SAFE', size: '1.1 GB' },
      { name: 'S1A_IW_20240524.SAFE', size: '1.1 GB' },
      { name: 'S1A_IW_20240605.SAFE', size: '1.2 GB' },
    ],
    sources: [
      { scene: 'S1A_IW_20240605', coords: '51.95°N, 4.14°E', confidence: 94, date: '2024-06-05', patch: '3381', label: 'Berth occupancy — container vessel' },
      { scene: 'S1A_IW_20240524', coords: '51.96°N, 4.12°E', confidence: 89, date: '2024-05-24', patch: '2210', label: 'Crane gantry return' },
      { scene: 'S1A_IW_20240605', coords: '51.94°N, 4.16°E', confidence: 85, date: '2024-06-05', patch: '3402', label: 'Bulk carrier, moored' },
      { scene: 'S1A_IW_20240512', coords: '51.97°N, 4.10°E', confidence: 78, date: '2024-05-12', patch: '0765', label: 'Empty berth slot' },
      { scene: 'S1A_IW_20240524', coords: '51.93°N, 4.15°E', confidence: 71, date: '2024-05-24', patch: '2094', label: 'Container stack density change' },
    ],
    prompts: [
      'Which berths are occupied in the latest scene?',
      'Compare container density between May and June',
      'Show me all crane gantry detections',
      'Are any berths empty across all scenes?',
    ],
  },
  'proj-3': {
    title: 'South_China_Sea_Detection',
    theme: 'Wide-area vessel detection over contested reef zones, flagging dark vessels not broadcasting AIS.',
    sceneCount: 5, patchCount: 8400, vectorCount: 8400, indexName: 'sar_index_scs',
    files: [
      { name: 'Scene_01_S1A_20240609.tif', size: '980 MB' },
      { name: 'Scene_02_S1A_20240611.tif', size: '1.0 GB' },
      { name: 'Scene_03_S1A_20240613.tif', size: '960 MB' },
    ],
    sources: [
      { scene: 'Scene_02_S1A_20240611', coords: '14.32°N, 111.89°E', confidence: 96, date: '2024-06-11', patch: '1147', label: 'Dark vessel — no AIS match' },
      { scene: 'Scene_01_S1A_20240609', coords: '14.30°N, 111.85°E', confidence: 91, date: '2024-06-09', patch: '0892', label: 'Vessel cluster, anchored' },
      { scene: 'Scene_03_S1A_20240613', coords: '14.35°N, 111.92°E', confidence: 87, date: '2024-06-13', patch: '2210', label: 'Small craft, underway' },
      { scene: 'Scene_02_S1A_20240611', coords: '14.28°N, 111.80°E', confidence: 79, date: '2024-06-11', patch: '1523', label: 'Wake signature' },
      { scene: 'Scene_01_S1A_20240609', coords: '14.40°N, 111.95°E', confidence: 74, date: '2024-06-09', patch: '0334', label: 'Possible reef structure' },
    ],
    prompts: [
      'Show vessels with no matching AIS signal',
      'Summarize anomalies across all scenes',
      'Compare Scene_01 and Scene_03 for changes',
      'What is the confidence on the largest detection?',
    ],
  },
  'proj-4': {
    title: 'Amazon_Deforestation_SAR',
    theme: 'All-weather canopy monitoring across cloud-persistent basin sectors, flagging fresh clearing against baseline.',
    sceneCount: 6, patchCount: 11200, vectorCount: 11200, indexName: 'sar_index_amazon',
    files: [
      { name: 'Baseline_Q1_2024.h5', size: '2.1 GB' },
      { name: 'Sector_07_Q2_2024.h5', size: '1.9 GB' },
      { name: 'Sector_12_Q2_2024.h5', size: '2.0 GB' },
    ],
    sources: [
      { scene: 'Sector_07_Q2_2024', coords: '3.12°S, 60.03°W', confidence: 93, date: '2024-05-18', patch: '4410', label: 'Fresh clearing vs. baseline' },
      { scene: 'Sector_12_Q2_2024', coords: '3.45°S, 59.88°W', confidence: 88, date: '2024-05-22', patch: '5120', label: 'Road spur, new' },
      { scene: 'Sector_07_Q2_2024', coords: '3.08°S, 60.10°W', confidence: 82, date: '2024-05-18', patch: '4287', label: 'Canopy edge shift' },
      { scene: 'Baseline_Q1_2024', coords: '3.15°S, 60.01°W', confidence: 90, date: '2024-02-14', patch: '1102', label: 'Baseline canopy, intact' },
      { scene: 'Sector_12_Q2_2024', coords: '3.50°S, 59.92°W', confidence: 68, date: '2024-05-22', patch: '5344', label: 'Low-confidence texture shift' },
    ],
    prompts: [
      'Where is the largest new clearing this quarter?',
      'Compare Sector_07 against the Q1 baseline',
      'Show any new road spurs detected',
      'Which patches have the lowest confidence?',
    ],
  },
  'proj-5': {
    title: 'Suez_Transit_RAG',
    theme: 'Canal transit monitoring — vessel queue length, chokepoint occupancy, and convoy spacing.',
    sceneCount: 3, patchCount: 3900, vectorCount: 3900, indexName: 'sar_index_suez',
    files: [
      { name: 'Suez_North_20240601.tif', size: '740 MB' },
      { name: 'Suez_Mid_20240601.tif', size: '710 MB' },
    ],
    sources: [
      { scene: 'Suez_North_20240601', coords: '30.58°N, 32.27°E', confidence: 92, date: '2024-06-01', patch: '0210', label: 'Northbound convoy, 6 vessels' },
      { scene: 'Suez_Mid_20240601', coords: '30.42°N, 32.34°E', confidence: 85, date: '2024-06-01', patch: '0876', label: 'Anchorage queue, southbound' },
      { scene: 'Suez_North_20240601', coords: '30.60°N, 32.25°E', confidence: 80, date: '2024-06-01', patch: '0155', label: 'Tug escort return' },
      { scene: 'Suez_Mid_20240601', coords: '30.40°N, 32.36°E', confidence: 73, date: '2024-06-01', patch: '0902', label: 'Vessel spacing anomaly' },
      { scene: 'Suez_North_20240601', coords: '30.57°N, 32.29°E', confidence: 66, date: '2024-06-01', patch: '0244', label: 'Low-confidence return' },
    ],
    prompts: [
      'How many vessels are in the northbound convoy?',
      'Is there a queue at the southbound anchorage?',
      'Show any spacing anomalies in the convoy',
      'What time was this scene captured?',
    ],
  },
  'proj-6': {
    title: 'Strait_of_Hormuz_Scan',
    theme: 'Chokepoint monitoring for tanker traffic density and loitering behavior near territorial boundaries.',
    sceneCount: 4, patchCount: 5600, vectorCount: 5600, indexName: 'sar_index_hormuz',
    files: [
      { name: 'Hormuz_E_20240620.tif', size: '890 MB' },
      { name: 'Hormuz_W_20240622.tif', size: '860 MB' },
    ],
    sources: [
      { scene: 'Hormuz_E_20240620', coords: '26.57°N, 56.25°E', confidence: 95, date: '2024-06-20', patch: '1980', label: 'Tanker, loitering >6h' },
      { scene: 'Hormuz_W_20240622', coords: '26.61°N, 56.18°E', confidence: 89, date: '2024-06-22', patch: '2410', label: 'Tanker, transiting' },
      { scene: 'Hormuz_E_20240620', coords: '26.55°N, 56.30°E', confidence: 83, date: '2024-06-20', patch: '2033', label: 'Escort vessel, close proximity' },
      { scene: 'Hormuz_W_20240622', coords: '26.59°N, 56.20°E', confidence: 75, date: '2024-06-22', patch: '2477', label: 'Wake signature, high speed' },
      { scene: 'Hormuz_E_20240620', coords: '26.50°N, 56.35°E', confidence: 64, date: '2024-06-20', patch: '2100', label: 'Ambiguous return' },
    ],
    prompts: [
      'Which tankers are loitering longest?',
      'Show vessels near the territorial boundary',
      'Any high-speed wake signatures detected?',
      'Compare traffic density east vs. west scene',
    ],
  },
};

const DEFAULT_PROJECT = {
  title: 'New SAR Project',
  theme: 'Freshly ingested scenes, indexed and ready to query.',
  sceneCount: 3, patchCount: 4500, vectorCount: 4500, indexName: 'sar_index',
  files: [
    { name: 'Scene_01.tif', size: '910 MB' },
    { name: 'Scene_02.tif', size: '940 MB' },
    { name: 'Scene_03.tif', size: '905 MB' },
  ],
  sources: [
    { scene: 'Scene_02', coords: '—', confidence: 90, date: '—', patch: '1147', label: 'Detected anomaly' },
    { scene: 'Scene_01', coords: '—', confidence: 84, date: '—', patch: '0892', label: 'Detected anomaly' },
    { scene: 'Scene_03', coords: '—', confidence: 77, date: '—', patch: '2210', label: 'Detected anomaly' },
    { scene: 'Scene_02', coords: '—', confidence: 70, date: '—', patch: '1523', label: 'Weak return' },
    { scene: 'Scene_01', coords: '—', confidence: 63, date: '—', patch: '0334', label: 'Weak return' },
  ],
  prompts: [
    'Summarize what you found in this dataset',
    'What is the highest-confidence detection?',
    'List all scenes and their capture dates',
    'Are there any anomalies I should review first?',
  ],
};

function getProject() {
  const parts = window.location.pathname.split('/').filter(Boolean);
  const id = parts[1] || 'new';
  const base = PROJECTS[id] || DEFAULT_PROJECT;
  return { id, ...base };
}

/* ─────────────────────────────────────────────
   SIMULATED RAG RESPONSES
───────────────────────────────────────────── */
function buildWelcome(project) {
  const top = [...project.sources].sort((a, b) => b.confidence - a.confidence).slice(0, 3);
  const text = `I've indexed ${project.sceneCount} SAR scene${project.sceneCount !== 1 ? 's' : ''} — ${project.patchCount.toLocaleString()} patches total — for ${project.title}. ${project.theme} The strongest signal so far is in ${top[0].scene}: ${top[0].label.toLowerCase()} at ${top[0].confidence}% confidence [1]. Ask me anything about the imagery, detections, or coordinates.`;
  return { text, sources: top };
}

function buildAnswer(project, question, seed) {
  const pool = project.sources;
  const qWords = question.toLowerCase().split(/\s+/).filter(w => w.length > 3);
  let picked = pool.filter(s => qWords.some(w => s.label.toLowerCase().includes(w) || s.scene.toLowerCase().includes(w)));
  if (picked.length < 3) {
    const rest = pool.filter(s => !picked.includes(s));
    picked = [...picked, ...rest].slice(0, 3);
  } else {
    picked = picked.slice(0, 3);
  }
  picked = [...picked].sort((a, b) => b.confidence - a.confidence);

  const [s0, s1, s2] = picked;
  const opinionLine = s0.confidence >= 90
    ? "That's a strong match — confidence this high is rarely speckle noise."
    : s0.confidence >= 75
      ? "That's a solid match, though worth a manual look given the mid-range confidence."
      : "Confidence here is on the lower side — could be genuine signal or residual speckle after filtering.";

  const templates = [
    `Querying the index for "${question}" surfaces ${s0.scene} at ${s0.coords} as the top match — ${s0.label.toLowerCase()} at ${s0.confidence}% confidence [1]. A secondary candidate sits in ${s1.scene} (${s1.confidence}% match, captured ${s1.date}) [2], and a third, weaker signal appears in ${s2.scene} at ${s2.confidence}% [3]. ${opinionLine}`,
    `Based on the retrieved patches, ${s0.scene} shows the clearest signal: ${s0.label.toLowerCase()} near ${s0.coords} at ${s0.confidence}% confidence [1]. ${s1.scene} adds a related detection at ${s1.confidence}% [2], and ${s2.scene} contributes a lower-confidence match at ${s2.confidence}% [3]. ${opinionLine}`,
    `Top result for that query is patch #${s0.patch} in ${s0.scene} — ${s0.label.toLowerCase()}, ${s0.confidence}% confidence, near ${s0.coords} [1]. It's followed by ${s1.scene} at ${s1.confidence}% [2] and ${s2.scene} at ${s2.confidence}% [3]. ${opinionLine}`,
  ];

  return { text: templates[seed % templates.length], sources: picked };
}

/* ─────────────────────────────────────────────
   SIGNATURE VISUAL — retrieved-patch thumbnail
───────────────────────────────────────────── */
function PatchThumb({ confidence }) {
  const tone = confidence >= 90 ? '#22c55e' : confidence >= 75 ? '#0088ff' : '#71717a';
  const bx = 20 + ((confidence * 7) % 13) - 6;
  const by = 20 - ((confidence * 3) % 9) + 3;
  return (
    <div className="shrink-0 w-10 h-10 rounded-lg relative overflow-hidden" style={{ background: '#0a0a0c', border: '1px solid #1f1f26' }}>
      <svg viewBox="0 0 40 40" className="w-full h-full">
        <circle cx="20" cy="20" r="17" fill="none" stroke={tone} strokeOpacity="0.18" strokeWidth="1" />
        <circle cx="20" cy="20" r="11" fill="none" stroke={tone} strokeOpacity="0.28" strokeWidth="1" />
        <circle cx="20" cy="20" r="5" fill="none" stroke={tone} strokeOpacity="0.4" strokeWidth="1" />
        <circle cx={bx} cy={by} r="1.6" fill={tone} />
      </svg>
      <div className="absolute inset-0 pointer-events-none" style={{ background: `linear-gradient(115deg, transparent 40%, ${tone}22 50%, transparent 60%)` }} />
    </div>
  );
}

/* ─────────────────────────────────────────────
   COMPONENT
───────────────────────────────────────────── */
export default function Project() {
  const initialProject = getProject();
  const [projectData, setProjectData] = useState(initialProject);
  const [activeSessionId, setActiveSessionId] = useState(null);
  const welcome = useRef(buildWelcome(projectData)).current;

  const [messages, setMessages] = useState(() => ([
    { id: 'm0', role: 'assistant', content: welcome.text, sources: welcome.sources, streaming: false },
  ]));
  const [activeSources, setActiveSources] = useState(welcome.sources);
  const [activeMessageId, setActiveMessageId] = useState('m0');
  const [input, setInput] = useState('');
  const [isThinking, setIsThinking] = useState(false);
  const [thinkingStage, setThinkingStage] = useState(0);
  const [fileFilter, setFileFilter] = useState('');
  const [copySuccess, setCopySuccess] = useState(false);

  const messagesEndRef = useRef(null);
  const textareaRef = useRef(null);
  const timers = useRef([]);
  const streamInterval = useRef(null);
  const idCounter = useRef(1);
  const nextId = () => `m${idCounter.current++}`;

  /* ── Auth & Real Project Loading ── */
  useEffect(() => {
    async function initProject() {
      const supabase = getSupabase();
      if (!supabase) return;
      const { data: { session } } = await supabase.auth.getSession();
      if (!session) {
        navigate('/login');
        return;
      }

      const parts = window.location.pathname.split('/').filter(Boolean);
      const id = parts[1];
      if (!id || id === 'new') return;

      // 1. Fetch conversation for title and session_id
      const { data: convData } = await supabase
        .from('conversations')
        .select('*')
        .eq('id', id)
        .single();
        
      if (convData) {
        setProjectData(prev => ({
          ...prev,
          title: convData.title,
          id: convData.id
        }));
        
        if (convData.session_id) {
          setActiveSessionId(convData.session_id);
        }
      }
    }
    initProject();
  }, []);

  /* ── Auto-scroll ── */
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, isThinking]);

  /* ── Cleanup timers on unmount ── */
  useEffect(() => () => {
    timers.current.forEach(clearTimeout);
    if (streamInterval.current) clearInterval(streamInterval.current);
  }, []);

  const navigate = (path) => {
    window.history.pushState({}, '', path);
    window.dispatchEvent(new PopStateEvent('popstate'));
  };
  const goBack = () => navigate('/dashboard');

  const handleCopyLink = () => {
    navigator.clipboard.writeText(window.location.href);
    setCopySuccess(true);
    setTimeout(() => setCopySuccess(false), 2000);
  };

  const autoResize = () => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 160) + 'px';
  };

  const handleSend = async (rawText) => {
    const text = (typeof rawText === 'string' ? rawText : input).trim();
    if (!text || isThinking) return;

    setInput('');
    if (textareaRef.current) textareaRef.current.style.height = 'auto';

    const userId = nextId();
    setMessages(prev => [...prev, { id: userId, role: 'user', content: text }]);
    setIsThinking(true);
    setThinkingStage(0);

    timers.current.forEach(clearTimeout);
    timers.current = [];

    const assistantId = nextId();
    setActiveMessageId(assistantId);
    setMessages(prev => [...prev, { id: assistantId, role: 'assistant', content: '', sources: [], streaming: true }]);

    try {
        const sessionId = activeSessionId || localStorage.getItem('raikou_session_id') || 'default_session';
        const BACKEND_URL = process.env.REACT_APP_API_URL || 'http://localhost:8000';
        
        const supabase = getSupabase();
        let token = '';
        if (supabase) {
            const { data: { session } } = await supabase.auth.getSession();
            if (session) {
                token = session.access_token;
            }
        }

        const response = await fetch(`${BACKEND_URL}/api/v1/search/rag/chat`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                ...(token ? { 'Authorization': `Bearer ${token}` } : {})
            },
            body: JSON.stringify({
                query: text,
                session_id: sessionId,
                limit: 6
            })
        });

        if (!response.ok) throw new Error('API Error');

        const reader = response.body.getReader();
        const decoder = new TextDecoder();

        while (true) {
            const { value, done } = await reader.read();
            if (done) break;

            const chunkStr = decoder.decode(value, { stream: true });
            const lines = chunkStr.split('\n');

            for (const line of lines) {
                if (!line.trim()) continue;
                try {
                    const data = JSON.parse(line);

                    if (data.type === 'sources') {
                        setThinkingStage(1);
                        const newSources = data.data.map((s, i) => ({
                            id: `patch_${s.id}`,
                            patch: String(s.id).substring(0, 4),
                            label: `Database Match ${i + 1}`,
                            confidence: Math.round(s.score * 100),
                            scene: s.scene,
                            coords: `${s.row}, ${s.col}`,
                            date: 'Live Query'
                        }));
                        setActiveSources(newSources);
                        setMessages(prev => prev.map(msg =>
                            msg.id === assistantId ? { ...msg, sources: newSources } : msg
                        ));
                    } else if (data.type === 'text') {
                        setMessages(prev => prev.map(msg =>
                            msg.id === assistantId ? { ...msg, content: msg.content + data.data } : msg
                        ));
                    }
                } catch (e) {
                    console.warn("Failed to parse chunk:", line, e);
                }
            }
        }
        
        setMessages(prev => prev.map(msg =>
            msg.id === assistantId ? { ...msg, streaming: false } : msg
        ));
    } catch (error) {
        console.error("RAG Error:", error);
        setMessages(prev => prev.map(msg =>
            msg.id === assistantId ? { ...msg, content: msg.content + `\n\n⚠️ Error connecting to RAG backend.`, streaming: false } : msg
        ));
    } finally {
        setIsThinking(false);
    }
  };

  const onKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const renderContent = (text, sources, msgId) => {
    const parts = text.split(/(\[\d+\])/g);
    return parts.map((part, i) => {
      const m = part.match(/^\[(\d+)\]$/);
      if (m && sources) {
        const idx = parseInt(m[1], 10) - 1;
        const src = sources[idx];
        return (
          <button
            key={i}
            onClick={() => { setActiveSources(sources); setActiveMessageId(msgId); }}
            title={src ? `${src.scene} · ${src.confidence}% match` : ''}
            className="inline-flex items-center justify-center min-w-[16px] h-4 px-1 rounded-full bg-[#0088ff]/15 border border-[#0088ff]/40 text-[#5eb6ff] text-[9px] font-bold mx-0.5 align-middle hover:bg-[#0088ff]/25 transition-colors"
          >
            {m[1]}
          </button>
        );
      }
      return <span key={i}>{part}</span>;
    });
  };

  const filteredFiles = projectData.files.filter(f => f.name.toLowerCase().includes(fileFilter.toLowerCase()));
  const hasStreamingMessage = messages.some(m => m.streaming);

  return (
    <>
      <style>{KEYFRAMES}</style>

      <div className="min-h-screen bg-[#09090b] text-[#c5c5c9] font-['Inter'] text-[13px] flex selection:bg-[#0088ff]/30">

        {/* ══════════════════════════════════════
            SIDEBAR
        ══════════════════════════════════════ */}
        <aside className="w-60 shrink-0 border-r border-[#1c1c22] bg-[#0c0c0e] p-3 flex flex-col h-screen select-none">

          <button
            onClick={goBack}
            className="flex items-center gap-2 p-1.5 mb-2.5 rounded-lg hover:bg-[#1a1a1f] transition-colors text-zinc-400 hover:text-zinc-200 w-full text-left"
          >
            <ArrowLeft size={13} />
            <span className="text-[12px]">All Projects</span>
          </button>

          <div className="rounded-lg bg-[#131316] border border-[#1c1c22] p-2.5 mb-4">
            <div className="flex items-center gap-2 mb-1.5">
              <div className="w-6 h-6 rounded bg-[#0088ff]/15 border border-[#0088ff]/25 flex items-center justify-center shrink-0">
                <Radar size={12} className="text-[#5eb6ff]" />
              </div>
              <span className="text-white font-medium text-[12px] truncate">{projectData.title}</span>
            </div>
            <p className="text-zinc-500 text-[11px] leading-snug">
              {projectData.sceneCount} scenes · {projectData.patchCount.toLocaleString()} patches
            </p>
          </div>

          <div className="relative mb-4">
            <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-zinc-500" />
            <input
              type="text"
              value={fileFilter}
              onChange={e => setFileFilter(e.target.value)}
              placeholder="Search sources..."
              className="w-full bg-[#18181b] border border-[#242429] text-white placeholder:text-zinc-500 rounded-lg pl-8 pr-3 py-1.5 outline-none text-[12px] focus:border-zinc-700 transition-colors"
            />
          </div>

          <div className="flex-1 flex flex-col min-h-0">
            <span className="text-zinc-500 font-semibold text-[10px] uppercase tracking-wider mb-2 px-2.5">
              Sources
            </span>
            <nav className="flex flex-col gap-0.5 overflow-y-auto">
              {filteredFiles.map((f) => (
                <div
                  key={f.name}
                  className="flex items-center gap-2.5 px-2.5 py-1.5 rounded-lg text-zinc-400 hover:text-zinc-200 hover:bg-[#15151a] transition-colors duration-150"
                >
                  <FileText size={13} className="text-zinc-500 shrink-0" />
                  <span className="text-[12px] truncate flex-1">{f.name}</span>
                  <span className="text-[10px] text-zinc-600 shrink-0">{f.size}</span>
                </div>
              ))}
              {filteredFiles.length === 0 && (
                <p className="text-[11px] text-zinc-600 px-2.5 py-2">No sources match.</p>
              )}
            </nav>

            <button
              onClick={() => navigate('/ingestion')}
              className="flex items-center gap-2.5 px-2.5 py-1.5 mt-1 rounded-lg text-left text-zinc-500 hover:text-zinc-300 hover:bg-[#15151a] transition-colors duration-150"
            >
              <Plus size={14} className="text-zinc-500" />
              <span className="text-[12px]">Add more data...</span>
            </button>
          </div>

          <div className="mt-auto border-t border-[#1c1c22] pt-3 flex items-center justify-between">
            <button className="flex items-center gap-2 text-zinc-400 hover:text-white transition-colors py-1 px-1.5 rounded hover:bg-[#1a1a1f] text-[12px]">
              <Users size={14} className="text-zinc-500" />
              <span>Invite your team</span>
            </button>
            <button
              onClick={handleCopyLink}
              className={`
                text-[11px] font-medium px-2.5 py-1.5 rounded-lg border transition-all active:scale-[0.96]
                ${copySuccess
                  ? 'bg-emerald-950/40 border-emerald-800 text-emerald-400'
                  : 'bg-[#18181c] border-[#25252b] text-zinc-200 hover:bg-[#202026] hover:text-white'
                }
              `}
            >
              {copySuccess ? 'Copied!' : 'Copy Link'}
            </button>
          </div>
        </aside>

        {/* ══════════════════════════════════════
            MAIN COLUMN
        ══════════════════════════════════════ */}
        <div className="flex-1 flex flex-col h-screen min-w-0">

          {/* Header */}
          <header className="h-14 shrink-0 border-b border-[#1c1c22] flex items-center justify-between px-6">
            <div className="flex items-center gap-3 min-w-0">
              <button
                onClick={goBack}
                className="p-1.5 rounded-lg hover:bg-[#1a1a1f] text-zinc-400 hover:text-white transition-colors shrink-0"
              >
                <ArrowLeft size={16} />
              </button>
              <h1 className="text-white font-semibold text-[14px] truncate">{projectData.title}</h1>
              <span className="shrink-0 flex items-center gap-1.5 bg-[#0f2a1c] border border-[#1c4a30] text-emerald-400 text-[10px] font-semibold px-2 py-0.5 rounded-full uppercase tracking-wider">
                <span className="w-1.5 h-1.5 rounded-full bg-emerald-400" />
                Indexed
              </span>
            </div>
            <div className="flex items-center gap-1.5 shrink-0">
              <button
                onClick={() => alert('Export coming soon.')}
                title="Export conversation"
                className="p-1.5 rounded-lg hover:bg-[#1a1a1f] text-zinc-500 hover:text-zinc-200 transition-colors"
              >
                <Download size={15} />
              </button>
              <button
                onClick={() => alert('More options coming soon.')}
                title="More"
                className="p-1.5 rounded-lg hover:bg-[#1a1a1f] text-zinc-500 hover:text-zinc-200 transition-colors"
              >
                <MoreHorizontal size={15} />
              </button>
            </div>
          </header>

          {/* Body: chat + sources */}
          <div className="flex-1 flex min-h-0">

            {/* Chat column */}
            <div className="flex-1 flex flex-col min-w-0">
              <div className="flex-1 overflow-y-auto px-6 py-6">
                {messages.map((msg) => (
                  <div
                    key={msg.id}
                    className={`flex mb-6 ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}
                    style={{ animation: 'riseIn 0.3s ease both' }}
                  >
                    {msg.role === 'assistant' && (
                      <div className="w-7 h-7 rounded-lg bg-[#0088ff]/15 border border-[#0088ff]/30 flex items-center justify-center shrink-0 mr-3 mt-0.5">
                        <Radar size={13} className="text-[#5eb6ff]" />
                      </div>
                    )}
                    <div className={msg.role === 'user' ? 'max-w-[72%]' : 'max-w-[80%] flex-1'}>
                      {msg.role === 'user' ? (
                        <div className="bg-[#0088ff]/12 border border-[#0088ff]/25 text-white rounded-2xl rounded-tr-sm px-4 py-2.5 text-[13px] leading-relaxed">
                          {msg.content}
                        </div>
                      ) : (
                        <>
                          <div className="text-[#c5c5c9] text-[13px]" style={{ lineHeight: 1.7 }}>
                            {renderContent(msg.content, msg.sources, msg.id)}
                            {msg.streaming && (
                              <span
                                className="inline-block w-[7px] h-[13px] ml-1 align-middle bg-[#0088ff]"
                                style={{ animation: 'pulseDot 0.8s step-start infinite' }}
                              />
                            )}
                          </div>
                          {!msg.streaming && msg.sources && (
                            <button
                              onClick={() => { setActiveSources(msg.sources); setActiveMessageId(msg.id); }}
                              className={`mt-2 inline-flex items-center gap-1.5 text-[11px] px-2 py-1 rounded-md border transition-colors ${activeMessageId === msg.id
                                ? 'border-[#0088ff]/50 bg-[#0088ff]/10 text-[#5eb6ff]'
                                : 'border-[#242429] text-zinc-500 hover:text-zinc-300 hover:border-[#33333a]'
                                }`}
                            >
                              <Database size={11} />
                              {msg.sources.length} source{msg.sources.length !== 1 ? 's' : ''}
                            </button>
                          )}
                        </>
                      )}

                      {msg.id === 'm0' && messages.length === 1 && (
                        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 mt-4 max-w-[560px]">
                          {projectData.prompts.map((p) => (
                            <button
                              key={p}
                              onClick={() => handleSend(p)}
                              className="text-left text-[12px] text-zinc-400 hover:text-white bg-[#131316] hover:bg-[#18181c] border border-[#1c1c22] hover:border-[#2b2b33] rounded-xl px-3.5 py-2.5 transition-colors"
                            >
                              {p}
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                  </div>
                ))}

                {isThinking && !hasStreamingMessage && (
                  <div className="flex items-center gap-2 mb-6 ml-10">
                    <span
                      className="w-1.5 h-1.5 rounded-full bg-[#0088ff]"
                      style={{ animation: 'pulseDot 1.2s ease-in-out infinite' }}
                    />
                    <span className="text-[12px] text-zinc-500">
                      {thinkingStage === 0
                        ? 'Querying vector index…'
                        : `Re-ranking ${projectData.patchCount.toLocaleString()} candidate patches…`}
                    </span>
                  </div>
                )}

                <div ref={messagesEndRef} />
              </div>

              {/* Input bar */}
              <div className="border-t border-[#1c1c22] p-4 shrink-0">
                <div className="flex items-end gap-2 bg-[#131316] border border-[#242429] rounded-2xl px-3 py-2 focus-within:border-[#33333a] transition-colors">
                  <button
                    onClick={() => navigate('/ingestion')}
                    title="Add more data"
                    className="p-1.5 rounded-lg text-zinc-500 hover:text-zinc-200 hover:bg-[#1c1c22] transition-colors shrink-0 mb-0.5"
                  >
                    <Paperclip size={15} />
                  </button>
                  <textarea
                    ref={textareaRef}
                    rows={1}
                    value={input}
                    onChange={e => { setInput(e.target.value); autoResize(); }}
                    onKeyDown={onKeyDown}
                    placeholder={`Ask about ${projectData.title}…`}
                    disabled={isThinking}
                    className="flex-1 bg-transparent outline-none resize-none text-[13px] text-white placeholder:text-zinc-600 py-1.5 max-h-40 disabled:opacity-50"
                  />
                  <button
                    onClick={() => handleSend()}
                    disabled={!input.trim() || isThinking}
                    className="shrink-0 w-8 h-8 rounded-full flex items-center justify-center transition-all active:scale-95 disabled:opacity-30 disabled:cursor-not-allowed"
                    style={{ backgroundColor: '#0088ff' }}
                  >
                    <ArrowUp size={15} className="text-white" />
                  </button>
                </div>
                <p className="text-[10px] text-zinc-600 mt-2 text-center">
                  AI-generated answers are grounded in retrieved patches but may still be wrong. Verify critical detections.
                </p>
              </div>
            </div>

            {/* Sources panel */}
            <aside className="hidden xl:flex w-[340px] shrink-0 border-l border-[#1c1c22] bg-[#0c0c0e] flex-col h-full overflow-hidden">
              <div className="p-4 border-b border-[#1c1c22] shrink-0">
                <div className="flex items-center justify-between mb-3">
                  <span className="text-zinc-500 font-semibold text-[10px] uppercase tracking-wider">Index</span>
                  <span className="flex items-center gap-1.5 text-[10px] text-emerald-400">
                    <span
                      className="w-1.5 h-1.5 rounded-full bg-emerald-400"
                      style={{ animation: 'pulseDot 2s ease-in-out infinite' }}
                    />
                    Live
                  </span>
                </div>
                <p className="text-white text-[12px] font-mono mb-3 truncate">{projectData.indexName}</p>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <p className="text-[10px] text-zinc-600 mb-0.5">Scenes</p>
                    <p className="text-[13px] font-mono text-zinc-200 font-semibold">{projectData.sceneCount}</p>
                  </div>
                  <div>
                    <p className="text-[10px] text-zinc-600 mb-0.5">Patches</p>
                    <p className="text-[13px] font-mono text-zinc-200 font-semibold">{projectData.patchCount.toLocaleString()}</p>
                  </div>
                  <div>
                    <p className="text-[10px] text-zinc-600 mb-0.5">Vectors</p>
                    <p className="text-[13px] font-mono text-zinc-200 font-semibold">{projectData.vectorCount.toLocaleString()}</p>
                  </div>
                  <div>
                    <p className="text-[10px] text-zinc-600 mb-0.5">Dimensions</p>
                    <p className="text-[13px] font-mono text-zinc-200 font-semibold">512-d</p>
                  </div>
                </div>
              </div>

              <div className="p-4 flex-1 overflow-y-auto">
                <div className="flex items-center justify-between mb-3">
                  <span className="text-zinc-500 font-semibold text-[10px] uppercase tracking-wider">Retrieved Sources</span>
                  {activeSources.length > 0 && (
                    <span className="text-[10px] text-zinc-600">
                      {activeSources.length} match{activeSources.length !== 1 ? 'es' : ''}
                    </span>
                  )}
                </div>

                {activeSources.length === 0 ? (
                  <div className="flex flex-col items-center justify-center text-center py-12 px-2">
                    <div className="w-9 h-9 rounded-lg bg-[#131316] border border-[#1c1c22] flex items-center justify-center mb-3">
                      <Database size={14} className="text-zinc-600" />
                    </div>
                    <p className="text-[12px] text-zinc-500 leading-relaxed">
                      Ask a question — matching SAR patches will appear here.
                    </p>
                  </div>
                ) : (
                  <div className="flex flex-col gap-2.5">
                    {activeSources.map((s, i) => (
                      <div
                        key={i}
                        className="rounded-xl border border-[#1c1c22] hover:border-[#2b2b33] bg-[#131316] hover:bg-[#16161a] p-3 transition-colors"
                      >
                        <div className="flex gap-3">
                          <PatchThumb confidence={s.confidence} />
                          <div className="min-w-0 flex-1">
                            <div className="flex items-start justify-between gap-2 mb-1">
                              <span className="text-[11px] font-mono text-zinc-300 truncate">{s.scene}</span>
                              <span
                                className={`shrink-0 text-[10px] font-bold px-1.5 py-0.5 rounded ${s.confidence >= 90
                                  ? 'bg-emerald-950/50 text-emerald-400'
                                  : s.confidence >= 75
                                    ? 'bg-[#0088ff]/15 text-[#5eb6ff]'
                                    : 'bg-zinc-800/60 text-zinc-400'
                                  }`}
                              >
                                {s.confidence}%
                              </span>
                            </div>
                            <p className="text-[11px] text-zinc-500 mb-1.5 truncate">{s.label}</p>
                            <div className="flex items-center gap-1 text-[10px] text-zinc-600">
                              <MapPin size={9} />
                              <span className="font-mono truncate">{s.coords}</span>
                            </div>
                            <div className="flex items-center justify-between mt-1 text-[10px] text-zinc-600">
                              <span>Patch #{s.patch}</span>
                              <span>{s.date}</span>
                            </div>
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </aside>

          </div>
        </div>
      </div>
    </>
  );
}