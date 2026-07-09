import React, { useState, useEffect } from 'react';
import {
    Search, ChevronDown, Send, MessageSquare,
    Map, PanelRightClose, PanelRightOpen,
    Maximize2, Crosshair, Activity, Info
} from 'lucide-react';
import { getSupabase } from '../App';

export default function Chat() {
    const [profile, setProfile] = useState(null);
    const [messageInput, setMessageInput] = useState('');
    const [isSourcesOpen, setIsSourcesOpen] = useState(true);

    const [chatHistory, setChatHistory] = useState([
        {
            id: 1,
            role: 'user',
            content: 'Identify commercial cargo vessels traveling south in the Rotterdam scene.'
        },
        {
            id: 2,
            role: 'assistant',
            content: 'Based on the SAR backscatter data from the Rotterdam scene, I have identified 3 distinct high-intensity anomalies matching the geometric signature of commercial cargo vessels. They are currently positioned in the outbound shipping lane on a southern trajectory. Coordinates and patch references are isolated in the sources panel.',
            sources: ['patch_942', 'patch_945']
        }
    ]);

    const [retrievedSources, setRetrievedSources] = useState([
        {
            id: 'patch_942',
            title: 'Vessel Signature Alpha',
            confidence: '98%',
            scene: 'S1A_IW_SLC',
            coords: '51.96° N, 4.02° E'
        },
        {
            id: 'patch_945',
            title: 'Vessel Signature Beta',
            confidence: '94%',
            scene: 'S1A_IW_SLC',
            coords: '51.94° N, 3.98° E'
        }
    ]);

    useEffect(() => {
        async function fetchProfile() {
            const supabase = getSupabase();
            if (!supabase) return;

            const { data: { session } } = await supabase.auth.getSession();
            if (session) {
                const { data, error } = await supabase
                    .from('profiles')
                    .select('*')
                    .eq('id', session.user.id)
                    .single();

                if (data && !error) {
                    setProfile(data);
                } else {
                    setProfile({
                        full_name: session.user.user_metadata?.full_name || session.user.email,
                        avatar_url: session.user.user_metadata?.avatar_url
                    });
                }
            }
        }
        fetchProfile();
    }, []);

    const handleSendMessage = async (e) => {
        e.preventDefault();
        if (!messageInput.trim()) return;

        const query = messageInput.trim();
        setMessageInput('');

        const userMsgId = Date.now();
        setChatHistory(prev => [...prev, { id: userMsgId, role: 'user', content: query }]);

        const assistantMsgId = Date.now() + 1;
        setChatHistory(prev => [...prev, { id: assistantMsgId, role: 'assistant', content: '', sources: [] }]);

        try {
            const sessionId = localStorage.getItem('raikou_session_id') || 'default_session';
            const BACKEND_URL = process.env.REACT_APP_API_URL || 'http://localhost:8000';
            const response = await fetch(`${BACKEND_URL}/api/v1/search/rag/chat`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    query,
                    session_id: sessionId,
                    limit: 3
                })
            });

            if (!response.ok) throw new Error('API Error');

            const reader = response.body.getReader();
            const decoder = new TextDecoder();

            while (true) {
                const { value, done } = await reader.read();
                if (done) break;

                const chunkStr = decoder.decode(value, { stream: true });
                const lines = chunkStr.split('\\n');

                for (const line of lines) {
                    if (!line.trim()) continue;
                    try {
                        const data = JSON.parse(line);

                        if (data.type === 'sources') {
                            const newSources = data.data.map((s, i) => ({
                                id: `patch_${s.id}`,
                                title: `Match ${i + 1}`,
                                confidence: `${(s.score * 100).toFixed(1)}%`,
                                scene: s.scene,
                                coords: `${s.row}, ${s.col}`
                            }));
                            setRetrievedSources(newSources);
                            setIsSourcesOpen(true);

                            setChatHistory(prev => prev.map(msg =>
                                msg.id === assistantMsgId ? { ...msg, sources: newSources.map(s => s.id) } : msg
                            ));
                        } else if (data.type === 'text') {
                            setChatHistory(prev => prev.map(msg =>
                                msg.id === assistantMsgId ? { ...msg, content: msg.content + data.data } : msg
                            ));
                        } else if (data.type === 'error') {
                            console.error("VLM Error:", data.data);
                        }
                    } catch (e) {
                        console.error("Failed to parse chunk:", line, e);
                    }
                }
            }
        } catch (error) {
            console.error("RAG Error:", error);
        }
    };

    const goBackToDashboard = () => {
        window.history.pushState({}, '', '/dashboard');
        window.dispatchEvent(new PopStateEvent('popstate'));
    };

    return (
        <div className="min-h-screen bg-[#09090b] text-[#c5c5c9] font-['Inter'] text-[13px] flex selection:bg-[#0088ff]/30">

            {/* ══════════════════════════════════════
          SIDEBAR (Matching Dashboard.js)
      ══════════════════════════════════════ */}
            <aside className="w-60 shrink-0 border-r border-[#1c1c22] bg-[#0c0c0e] p-3 flex flex-col h-screen select-none transition-all">

                {/* Workspace Dropdown */}
                <div className="flex items-center justify-between p-1.5 mb-2.5 hover:bg-[#1a1a1f] rounded-lg cursor-pointer transition-colors duration-150 group">
                    <div className="flex items-center gap-2">
                        {profile?.avatar_url ? (
                            <img src={profile.avatar_url} alt="Avatar" className="w-5.5 h-5.5 rounded object-cover" style={{ width: '22px', height: '22px' }} />
                        ) : (
                            <div className="w-5.5 h-5.5 rounded bg-[#0088ff] flex items-center justify-center text-[11px] font-bold text-white uppercase" style={{ width: '22px', height: '22px' }}>
                                {profile?.full_name ? profile.full_name.charAt(0).toUpperCase() : 'U'}
                            </div>
                        )}
                        <span className="text-white font-medium text-[13px] tracking-tight truncate max-w-[130px]">
                            {profile?.full_name ? `${profile.full_name.split(' ')[0]}'s Workspace` : 'My Workspace'}
                        </span>
                    </div>
                    <ChevronDown size={14} className="text-zinc-500 group-hover:text-zinc-300 transition-colors" />
                </div>

                {/* Global Back to Projects */}
                <button
                    onClick={goBackToDashboard}
                    className="flex items-center gap-2.5 px-2.5 py-1.5 mb-4 rounded-lg text-left text-zinc-400 hover:text-white hover:bg-[#15151a] transition-colors duration-150"
                >
                    <Search size={14} />
                    <span className="text-[12px]">Back to Dashboard</span>
                </button>

                {/* Current Project Context */}
                <div className="flex-1 flex flex-col">
                    <span className="text-zinc-500 font-semibold text-[10px] uppercase tracking-wider mb-2 px-2.5">
                        Current Session
                    </span>
                    <nav className="flex flex-col gap-0.5">
                        <button className="flex items-center gap-2.5 px-2.5 py-1.5 rounded-lg text-left bg-[#1e1e24] text-white font-medium transition-colors duration-150">
                            <MessageSquare size={14} className="text-white" />
                            <span className="text-[12px] truncate">Rotterdam Analysis</span>
                        </button>
                        <button className="flex items-center gap-2.5 px-2.5 py-1.5 rounded-lg text-left text-zinc-400 hover:text-zinc-200 hover:bg-[#15151a] transition-colors duration-150">
                            <Map size={14} className="text-zinc-400" />
                            <span className="text-[12px] truncate">Raw SAR Patches</span>
                        </button>
                    </nav>
                </div>
            </aside>

            {/* ══════════════════════════════════════
          MAIN CHAT AREA
      ══════════════════════════════════════ */}
            <main className="flex-1 flex flex-col h-screen relative">

                {/* Header */}
                <header className="h-[60px] shrink-0 border-b border-[#1c1c22] bg-[#09090b] flex items-center justify-between px-6 select-none">
                    <div className="flex items-center gap-3">
                        <h1 className="text-[14px] text-white font-semibold tracking-tight">Project: S1A_IW_Rotterdam</h1>
                        <span className="px-2 py-0.5 rounded-md bg-[#0088ff]/10 border border-[#0088ff]/20 text-[#0088ff] text-[10px] font-medium tracking-wide uppercase">
                            RAG Active
                        </span>
                    </div>

                    <button
                        onClick={() => setIsSourcesOpen(!isSourcesOpen)}
                        className="flex items-center gap-2 bg-[#18181b] border border-[#242429] text-zinc-300 px-3 py-1.5 rounded-lg hover:bg-[#202025] hover:text-white transition-colors text-[12px] font-medium active:scale-[0.98]"
                    >
                        {isSourcesOpen ? <PanelRightClose size={14} /> : <PanelRightOpen size={14} />}
                        <span>{isSourcesOpen ? 'Hide Context' : 'Show Context'}</span>
                    </button>
                </header>

                {/* Chat Thread */}
                <div className="flex-1 overflow-y-auto px-6 py-8">
                    <div className="max-w-[800px] mx-auto flex flex-col gap-6">

                        {chatHistory.map((msg) => (
                            <div
                                key={msg.id}
                                className={`flex gap-4 ${msg.role === 'user' ? 'flex-row-reverse' : 'flex-row'}`}
                            >
                                {/* Avatar */}
                                <div className="shrink-0">
                                    {msg.role === 'user' ? (
                                        <div className="w-8 h-8 rounded-full bg-[#18181b] border border-[#242429] flex items-center justify-center text-zinc-400">
                                            U
                                        </div>
                                    ) : (
                                        <div className="w-8 h-8 rounded-full bg-[#0088ff]/10 border border-[#0088ff]/30 flex items-center justify-center text-[#0088ff]">
                                            <Activity size={16} />
                                        </div>
                                    )}
                                </div>

                                {/* Message Bubble */}
                                <div
                                    className={`max-w-[85%] rounded-2xl px-5 py-3.5 text-[14px] leading-relaxed ${msg.role === 'user'
                                            ? 'bg-[#18181b] border border-[#242429] text-white'
                                            : 'bg-transparent text-zinc-300'
                                        }`}
                                >
                                    {msg.content}

                                    {/* Inline Source Chips (if AI) */}
                                    {msg.sources && (
                                        <div className="flex flex-wrap gap-2 mt-4 pt-4 border-t border-[#1c1c22]">
                                            {msg.sources.map(source => (
                                                <button
                                                    key={source}
                                                    onClick={() => setIsSourcesOpen(true)}
                                                    className="flex items-center gap-1.5 px-2 py-1 rounded bg-[#131316] border border-[#202025] hover:border-[#0088ff]/40 text-[11px] font-mono text-zinc-400 transition-colors"
                                                >
                                                    <Crosshair size={10} className="text-[#0088ff]" />
                                                    {source}
                                                </button>
                                            ))}
                                        </div>
                                    )}
                                </div>
                            </div>
                        ))}

                    </div>
                </div>

                {/* Input Area */}
                <div className="shrink-0 bg-[#09090b] p-6 pb-8">
                    <div className="max-w-[800px] mx-auto relative">
                        <form onSubmit={handleSendMessage} className="relative flex items-end gap-2">
                            <div className="relative flex-1">
                                <textarea
                                    value={messageInput}
                                    onChange={(e) => setMessageInput(e.target.value)}
                                    placeholder="Ask about your SAR data..."
                                    className="w-full bg-[#131316] border border-[#242429] focus:border-[#383840] text-white placeholder:text-zinc-500 rounded-xl pl-4 pr-12 py-3.5 outline-none text-[13px] transition-colors resize-none shadow-sm min-h-[52px] max-h-[200px]"
                                    rows="1"
                                    onKeyDown={(e) => {
                                        if (e.key === 'Enter' && !e.shiftKey) {
                                            e.preventDefault();
                                            handleSendMessage(e);
                                        }
                                    }}
                                />
                            </div>
                            <button
                                type="submit"
                                disabled={!messageInput.trim()}
                                className="shrink-0 h-[52px] w-[52px] flex items-center justify-center bg-[#0088ff] hover:bg-[#007cdb] disabled:bg-[#18181b] disabled:text-zinc-600 active:scale-[0.96] transition-all text-white rounded-xl"
                            >
                                <Send size={18} className={messageInput.trim() ? "ml-0.5" : ""} />
                            </button>
                        </form>
                        <div className="text-center mt-3">
                            <span className="text-[10px] text-zinc-600 font-medium tracking-wide">
                                Raikou Intelligence can make mistakes. Verify critical coordinates.
                            </span>
                        </div>
                    </div>
                </div>
            </main>

            {/* ══════════════════════════════════════
          RIGHT PANEL: RETRIEVED SOURCES
      ══════════════════════════════════════ */}
            {isSourcesOpen && (
                <aside className="w-80 shrink-0 border-l border-[#1c1c22] bg-[#0c0c0e] flex flex-col h-screen">
                    <div className="h-[60px] shrink-0 border-b border-[#1c1c22] flex items-center px-4">
                        <h2 className="text-[13px] text-white font-medium flex items-center gap-2">
                            <Info size={14} className="text-zinc-400" />
                            Retrieved Context
                        </h2>
                    </div>

                    <div className="flex-1 overflow-y-auto p-4 flex flex-col gap-4">
                        {retrievedSources.map((source) => (
                            <div
                                key={source.id}
                                className="group flex flex-col bg-[#131316] border border-[#202025] hover:border-[#383840] rounded-xl overflow-hidden transition-all duration-200 cursor-pointer"
                            >
                                {/* Mock SAR Patch Visual */}
                                <div className="aspect-square w-full bg-[#1a1a1f] relative overflow-hidden flex items-center justify-center border-b border-[#202025]">
                                    <div className="absolute inset-0 opacity-[0.03] bg-[radial-gradient(#ffffff_1px,transparent_1px)] [background-size:12px_12px]" />
                                    {/* Fake radar texture representation */}
                                    <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1" className="text-zinc-600 group-hover:text-zinc-400 transition-colors">
                                        <path d="M12 22C17.5228 22 22 17.5228 22 12C22 6.47715 17.5228 2 12 2C6.47715 2 2 6.47715 2 12C2 17.5228 6.47715 22 12 22Z" strokeDasharray="4 4" />
                                        <circle cx="12" cy="12" r="4" fill="currentColor" fillOpacity="0.2" />
                                        <path d="M12 12L20 4" />
                                    </svg>

                                    <div className="absolute top-2 right-2 bg-black/60 backdrop-blur-md px-1.5 py-0.5 rounded text-[9px] font-mono text-zinc-300 border border-white/10 flex items-center gap-1">
                                        <Maximize2 size={8} />
                                        View
                                    </div>
                                </div>

                                {/* Patch Metadata */}
                                <div className="p-3">
                                    <div className="flex items-start justify-between mb-2">
                                        <h3 className="text-zinc-200 font-medium text-[12px] truncate group-hover:text-[#0088ff] transition-colors">
                                            {source.title}
                                        </h3>
                                        <span className="shrink-0 bg-emerald-950/30 border border-emerald-900/50 text-emerald-400 text-[9px] font-bold px-1.5 py-0.5 rounded ml-2">
                                            {source.confidence}
                                        </span>
                                    </div>

                                    <div className="flex flex-col gap-1.5">
                                        <div className="flex items-center justify-between text-[11px]">
                                            <span className="text-zinc-500">Scene</span>
                                            <span className="font-mono text-zinc-300">{source.scene}</span>
                                        </div>
                                        <div className="flex items-center justify-between text-[11px]">
                                            <span className="text-zinc-500">Location</span>
                                            <span className="font-mono text-zinc-300">{source.coords}</span>
                                        </div>
                                        <div className="flex items-center justify-between text-[11px]">
                                            <span className="text-zinc-500">Vector ID</span>
                                            <span className="font-mono text-[#0088ff]/70">{source.id}</span>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        ))}
                    </div>
                </aside>
            )}

        </div>
    );
}
