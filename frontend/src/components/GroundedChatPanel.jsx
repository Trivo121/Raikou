import { useMemo, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, Bot, FileImage, LoaderCircle, MessageSquare, Plus, Send, ShieldCheck } from 'lucide-react';

function readable(value, fallback = 'Not available') {
  if (!value) return fallback;
  return String(value).replace(/_/g, ' ');
}

function formatDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? 'Unknown time' : new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(date);
}

function CitationCards({ citations, onOpenPatch, onOpenPreview, onOpenScene }) {
  if (!Array.isArray(citations) || citations.length === 0) return null;
  return <div className="mt-3 grid gap-2 sm:grid-cols-2">{citations.slice(0, 8).map((citation, index) => {
    const canOpenPatch = citation.patch_id;
    const canOpenArtifact = citation.artifact_id;
    const onClick = canOpenPatch
      ? () => onOpenPatch(citation.patch_id, citation.scene_id)
      : (canOpenArtifact
        ? () => onOpenPreview({ id: citation.artifact_id, kind: citation.source_type })
        : (citation.scene_id ? () => onOpenScene(citation.scene_id) : undefined));
    return <button key={`${citation.source_type}-${citation.source_id}-${index}`} type="button" disabled={!onClick} onClick={onClick} className="rounded-lg border border-white/[0.08] bg-black/15 p-2.5 text-left text-[11px] leading-5 text-zinc-400 transition enabled:hover:border-sky-400/35 enabled:hover:bg-sky-400/[0.04] disabled:cursor-default"><div className="flex items-center gap-1.5 font-semibold capitalize text-zinc-200"><ShieldCheck size={12} className="text-sky-300" /> {readable(citation.source_type)}</div><p className="mt-1 line-clamp-2">{citation.why_provided || 'Authorized evidence source.'}</p>{citation.patch_id && <p className="mt-1 text-sky-300">Patch {String(citation.patch_id).slice(0, 8)}</p>}</button>;
  })}</div>;
}

export default function GroundedChatPanel({ api, userId, projectId, scenes, selectedSceneId, conversationId, onConversationChange, onOpenPatch, onOpenPreview, onOpenScene }) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState('');
  const [scopeSceneId, setScopeSceneId] = useState(selectedSceneId || '');
  const [streamText, setStreamText] = useState('');
  const [streamCitations, setStreamCitations] = useState([]);
  const [streamStatus, setStreamStatus] = useState(null);
  const [error, setError] = useState(null);
  const controllerRef = useRef(null);

  const readyScenes = useMemo(() => scenes.map((item) => item.scene).filter((scene) => scene?.status === 'ready'), [scenes]);
  const conversationsQuery = useQuery({
    queryKey: ['m5-conversations', userId, projectId],
    queryFn: ({ signal }) => api.conversations.list(projectId, { signal }),
    enabled: Boolean(userId && projectId),
  });
  const conversations = conversationsQuery.data || [];
  const activeConversation = conversations.find((conversation) => conversation.id === conversationId) || null;
  const messagesQuery = useQuery({
    queryKey: ['m5-conversation-messages', userId, conversationId],
    queryFn: ({ signal }) => api.conversations.messages(conversationId, { signal }),
    enabled: Boolean(userId && conversationId),
  });

  const beginNewConversation = () => {
    if (controllerRef.current) return;
    onConversationChange(null);
    setStreamText('');
    setStreamCitations([]);
    setStreamStatus(null);
    setError(null);
  };

  const send = async (event) => {
    event.preventDefault();
    const query = draft.trim();
    if (!query || controllerRef.current) return;
    setError(null);
    setStreamText('');
    setStreamCitations([]);
    setStreamStatus('Preparing private evidence…');
    let activeId = conversationId;
    const initialScope = activeConversation
      ? (activeConversation.scene_id || null)
      : (scopeSceneId || null);
    const controller = new AbortController();
    controllerRef.current = controller;
    try {
      if (!activeId) {
        const conversation = await api.conversations.create({
          project_id: projectId,
          scene_id: initialScope,
          title: query.slice(0, 160),
        }, { signal: controller.signal });
        activeId = conversation.id;
        onConversationChange(activeId);
        await queryClient.invalidateQueries({ queryKey: ['m5-conversations', userId, projectId] });
      }
      setDraft('');
      await api.conversations.stream(activeId, {
        query,
        // A conversation's stored scope wins after the first turn. This keeps
        // every persisted message in the same authorized scene/project scope.
        scene_id: activeConversation?.scene_id || null,
        limit: 6,
        filters: { ready_only: true },
      }, {
        signal: controller.signal,
        onEvent: (streamEvent) => {
          if (streamEvent.type === 'status') setStreamStatus(streamEvent.data?.message || readable(streamEvent.data?.state));
          if (streamEvent.type === 'citations') setStreamCitations(Array.isArray(streamEvent.data) ? streamEvent.data : []);
          if (streamEvent.type === 'text') setStreamText((value) => value + String(streamEvent.data || ''));
          if (streamEvent.type === 'error') setError(new Error(streamEvent.data?.message || 'Grounded chat is temporarily unavailable.'));
        },
      });
      await queryClient.fetchQuery({
        queryKey: ['m5-conversation-messages', userId, activeId],
        queryFn: ({ signal }) => api.conversations.messages(activeId, { signal }),
      });
      await queryClient.invalidateQueries({ queryKey: ['m5-conversations', userId, projectId] });
      // The API persisted the completed answer before emitting `done`; the
      // query cache now owns the displayed history and avoids a duplicate
      // transient streaming card after a successful response.
      setStreamText('');
      setStreamCitations([]);
    } catch (nextError) {
      if (nextError?.name !== 'AbortError') setError(nextError);
    } finally {
      controllerRef.current = null;
      setStreamStatus(null);
    }
  };

  return <section className="grid gap-5 xl:grid-cols-[17rem_minmax(0,1fr)]">
    <aside className="overflow-hidden rounded-xl border border-white/[0.08] bg-[#111114] xl:sticky xl:top-24 xl:max-h-[calc(100vh-8rem)] xl:overflow-y-auto">
      <div className="flex items-center justify-between border-b border-white/[0.07] px-4 py-3"><div><h2 className="text-sm font-semibold text-white">Conversations</h2><p className="mt-0.5 text-xs text-zinc-500">Private project history</p></div><button type="button" onClick={beginNewConversation} disabled={Boolean(controllerRef.current)} className="grid h-8 w-8 place-items-center rounded-lg border border-sky-400/25 bg-sky-400/10 text-sky-200 hover:bg-sky-400/20 disabled:opacity-40" aria-label="New conversation"><Plus size={15} /></button></div>
      {conversationsQuery.isLoading && <p className="p-4 text-xs text-zinc-500">Loading conversations…</p>}
      {conversationsQuery.isError && <p className="p-4 text-xs text-red-300">{conversationsQuery.error.message}</p>}
      {!conversationsQuery.isLoading && conversations.length === 0 && <div className="p-6 text-center"><MessageSquare className="mx-auto text-zinc-600" size={20} /><p className="mt-3 text-xs text-zinc-500">Start a question to create private history.</p></div>}
      <ul className="divide-y divide-white/[0.06]">{conversations.map((conversation) => <li key={conversation.id}><button type="button" onClick={() => { if (!controllerRef.current) onConversationChange(conversation.id); }} className={`w-full px-4 py-3 text-left transition ${conversation.id === conversationId ? 'bg-sky-400/[0.08] shadow-[inset_2px_0_0_#38bdf8]' : 'hover:bg-white/[0.03]'}`}><p className="line-clamp-2 text-xs font-medium text-zinc-200">{conversation.title}</p><p className="mt-1 text-[11px] text-zinc-600">{conversation.scene_id ? 'Scene scoped' : 'Project scoped'} · {formatDate(conversation.updated_at)}</p></button></li>)}</ul>
    </aside>

    <div className="min-w-0 rounded-xl border border-white/[0.08] bg-[#111114]">
      <div className="border-b border-white/[0.07] px-5 py-4"><p className="text-[11px] font-bold uppercase tracking-[0.16em] text-sky-300">Grounded ask</p><h2 className="mt-1 text-lg font-semibold text-white">Evidence-bound SAR analysis</h2><p className="mt-1 text-xs leading-5 text-zinc-500">Answers cite only authorized metadata, overviews, retrieved patches, and approved detector facts. Model observations remain labeled as observations.</p></div>
      <div className="max-h-[32rem] min-h-[18rem] space-y-4 overflow-y-auto p-5">
        {messagesQuery.isLoading && <div className="flex items-center gap-2 text-sm text-zinc-500"><LoaderCircle size={15} className="animate-spin" /> Loading saved history…</div>}
        {messagesQuery.isError && <div className="rounded-lg border border-red-500/25 bg-red-500/[0.08] p-3 text-xs text-red-100">Could not load this conversation: {messagesQuery.error.message}</div>}
        {!conversationId && !streamText && <EmptyAsk />}
        {(messagesQuery.data || []).map((message) => <article key={message.id} className={`max-w-3xl rounded-xl border p-4 ${message.role === 'user' ? 'ml-auto border-sky-400/20 bg-sky-400/[0.06]' : 'border-white/[0.08] bg-black/15'}`}><div className="flex items-center gap-2 text-xs font-semibold capitalize text-zinc-300"><Bot size={14} className={message.role === 'assistant' ? 'text-violet-300' : 'text-sky-300'} /> {message.role === 'assistant' ? 'Evidence-grounded response' : 'You'}</div><p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-zinc-200">{message.content}</p>{message.role === 'assistant' && <CitationCards citations={message.citations} onOpenPatch={onOpenPatch} onOpenPreview={onOpenPreview} onOpenScene={onOpenScene} />}{message.status === 'failed' && <p className="mt-2 text-xs text-amber-200">Generation did not complete. The saved response may be partial.</p>}</article>)}
        {streamStatus && <p className="text-xs text-sky-300">{streamStatus}</p>}
        {(streamText || streamCitations.length > 0) && <article className="max-w-3xl rounded-xl border border-violet-400/20 bg-violet-400/[0.05] p-4"><div className="flex items-center gap-2 text-xs font-semibold text-violet-200"><Bot size={14} /> Generating grounded response</div>{streamText && <p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-zinc-100">{streamText}</p>}<CitationCards citations={streamCitations} onOpenPatch={onOpenPatch} onOpenPreview={onOpenPreview} onOpenScene={onOpenScene} /></article>}
        {error && <div className="rounded-lg border border-red-500/25 bg-red-500/[0.08] p-3 text-xs text-red-100"><div className="flex gap-2"><AlertTriangle size={15} className="shrink-0" />{error.message}</div></div>}
      </div>
      <form onSubmit={send} className="border-t border-white/[0.07] p-4"><div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto]"><textarea value={draft} onChange={(event) => setDraft(event.target.value)} maxLength={1000} rows={3} placeholder="Ask about the authorized SAR evidence…" className="min-h-[5.5rem] w-full resize-y rounded-lg border border-white/[0.1] bg-black/20 px-3 py-2.5 text-sm text-white outline-none placeholder:text-zinc-600 focus:border-sky-400/55 focus:ring-2 focus:ring-sky-400/10" /><button type="submit" disabled={!draft.trim() || Boolean(controllerRef.current)} className="inline-flex items-center justify-center gap-2 self-end rounded-lg bg-sky-500 px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-sky-400 disabled:cursor-not-allowed disabled:opacity-45"><Send size={15} /> Ask</button></div><div className="mt-3 flex flex-col gap-2 text-xs text-zinc-500 sm:flex-row sm:items-center sm:justify-between"><label className="flex items-center gap-2">Scope for a new conversation<select value={activeConversation?.scene_id || scopeSceneId} disabled={Boolean(activeConversation)} onChange={(event) => setScopeSceneId(event.target.value)} className="rounded-md border border-white/[0.1] bg-black/20 px-2 py-1 text-xs text-zinc-300 disabled:opacity-55"><option value="">Entire project</option>{readyScenes.map((scene) => <option key={scene.id} value={scene.id}>{scene.name}</option>)}</select></label><span>{activeConversation ? (activeConversation.scene_id ? 'This conversation is locked to its selected scene.' : 'This conversation searches the full project.') : 'Choose a scene to create a scene-scoped conversation.'}</span></div></form>
    </div>
  </section>;
}

function EmptyAsk() {
  return <div className="grid min-h-[14rem] place-items-center text-center"><div><span className="mx-auto grid h-10 w-10 place-items-center rounded-xl border border-white/[0.08] bg-white/[0.03] text-zinc-500"><FileImage size={18} /></span><p className="mt-3 text-sm font-semibold text-zinc-200">Ask from evidence, not guesses</p><p className="mt-1 max-w-md text-xs leading-5 text-zinc-500">The assistant will explicitly say when authorized retrieval is empty or too weak to support a confident answer.</p></div></div>;
}
