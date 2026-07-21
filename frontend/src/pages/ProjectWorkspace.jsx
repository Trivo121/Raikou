import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Activity, AlertTriangle, ArrowLeft, Bot, CheckCircle2, Clock3, Eye,
  FileImage, Files, Info, Layers3, LoaderCircle, MapPin, MessageSquare,
  Plus, Radar, RefreshCw, Search, ShieldCheck, X,
} from 'lucide-react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { useAuth } from '../auth/AuthProvider';
import JobStatusCard, { isTerminalJobStatus } from '../components/JobStatusCard';
import SceneUploadPanel from '../components/SceneUploadPanel';
import EvidenceSearchPanel from '../components/EvidenceSearchPanel';
import GroundedChatPanel from '../components/GroundedChatPanel';

const TABS = [
  { id: 'overview', label: 'Overview', icon: Radar },
  { id: 'scenes', label: 'Scenes', icon: Layers3 },
  { id: 'evidence', label: 'Evidence Search', icon: Search },
  { id: 'ask', label: 'Ask', icon: MessageSquare },
];

const ACTIVE_JOB_STATUSES = new Set(['queued', 'validating', 'processing', 'running']);

function readable(value, fallback = 'Not available') {
  if (!value) return fallback;
  return String(value).replace(/_/g, ' ');
}

function formatDate(value, withTime = false) {
  if (!value) return 'Not available';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'Not available';
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    ...(withTime ? { timeStyle: 'short' } : {}),
  }).format(date);
}

function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return 'Unknown size';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let next = bytes / 1024;
  let index = 0;
  while (next >= 1024 && index < units.length - 1) {
    next /= 1024;
    index += 1;
  }
  return `${next.toFixed(next >= 10 || index === 0 ? 0 : 1)} ${units[index]}`;
}

function isActiveJob(job) {
  return ACTIVE_JOB_STATUSES.has(String(job?.status || '').toLowerCase());
}

function statusTone(status) {
  const normalized = String(status || '').toLowerCase();
  if (normalized === 'ready') return 'border-emerald-500/25 bg-emerald-500/10 text-emerald-300';
  if (normalized === 'failed') return 'border-red-500/25 bg-red-500/10 text-red-300';
  if (normalized === 'cancelled') return 'border-zinc-600 bg-zinc-800 text-zinc-300';
  if (normalized === 'queued' || normalized === 'uploading' || normalized === 'uploaded') return 'border-amber-500/25 bg-amber-500/10 text-amber-200';
  if (normalized === 'deleting') return 'border-red-500/25 bg-red-500/10 text-red-200';
  return 'border-sky-500/25 bg-sky-500/10 text-sky-200';
}

function StatusPill({ status }) {
  return <span className={`inline-flex shrink-0 rounded-full border px-2.5 py-1 text-[11px] font-semibold capitalize ${statusTone(status)}`}>{readable(status, 'draft')}</span>;
}

function WorkspaceMessage({ title, detail, action, icon: Icon = Info }) {
  return (
    <section className="flex min-h-[220px] flex-col items-center justify-center rounded-xl border border-white/[0.08] bg-[#111114] p-8 text-center">
      <span className="mb-4 grid h-10 w-10 place-items-center rounded-xl border border-white/[0.08] bg-white/[0.03] text-zinc-500"><Icon size={18} /></span>
      <h2 className="text-sm font-semibold text-zinc-100">{title}</h2>
      {detail && <p className="mt-2 max-w-md text-sm leading-6 text-zinc-500">{detail}</p>}
      {action && <button type="button" onClick={action} className="mt-5 rounded-lg border border-sky-400/25 bg-sky-400/10 px-3 py-2 text-xs font-semibold text-sky-200 transition hover:bg-sky-400/20">Try again</button>}
    </section>
  );
}

function useWorkspaceLocation() {
  const [params, setParams] = useSearchParams();
  const tab = TABS.some((item) => item.id === params.get('tab')) ? params.get('tab') : 'overview';
  const sceneId = params.get('scene') || null;
  const patchId = params.get('patch') || null;
  const conversationId = params.get('conversation') || null;
  const update = (next) => {
    const value = new URLSearchParams(params);
    Object.entries(next).forEach(([key, entry]) => {
      if (entry === null || entry === undefined || entry === '') value.delete(key);
      else value.set(key, entry);
    });
    setParams(value, { replace: true });
  };
  return { tab, sceneId, patchId, conversationId, update };
}

export default function ProjectWorkspace() {
  const { projectId } = useParams();
  const { api, user } = useAuth();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const location = useWorkspaceLocation();
  const [isAddingScene, setIsAddingScene] = useState(false);
  const [sceneName, setSceneName] = useState('');
  const [preview, setPreview] = useState(null);
  const [openedPatchId, setOpenedPatchId] = useState(null);

  const projectQuery = useQuery({
    queryKey: ['workspace', user?.id, projectId],
    queryFn: ({ signal }) => api.projects.workspace(projectId, { signal }),
    enabled: Boolean(user?.id && projectId),
    refetchInterval: (query) => (
      (query.state.data?.scenes || []).some((item) => isActiveJob(item.active_job)) ? 5000 : false
    ),
  });

  const workspace = projectQuery.data;
  const resolvedSceneId = location.sceneId || workspace?.scenes?.[0]?.scene?.id || null;
  const selectedSummary = useMemo(
    () => (workspace?.scenes || []).find((item) => item.scene?.id === resolvedSceneId) || null,
    [resolvedSceneId, workspace?.scenes],
  );
  const sceneQuery = useQuery({
    queryKey: ['scene-workspace', user?.id, resolvedSceneId],
    queryFn: ({ signal }) => api.scenes.workspace(resolvedSceneId, { signal }),
    enabled: Boolean(user?.id && resolvedSceneId),
    refetchInterval: (query) => isActiveJob(query.state.data?.active_job) ? 4000 : false,
  });
  const selectedScene = sceneQuery.data || null;
  const visibleJob = selectedScene?.active_job || selectedScene?.latest_job || selectedSummary?.active_job || selectedSummary?.latest_job || null;
  const eventsQuery = useQuery({
    queryKey: ['job-events', user?.id, visibleJob?.id],
    queryFn: ({ signal }) => api.jobs.events(visibleJob.id, { signal }),
    enabled: Boolean(user?.id && visibleJob?.id && location.tab === 'scenes'),
    refetchInterval: isActiveJob(visibleJob) ? 5000 : false,
  });
  const patchQuery = useQuery({
    queryKey: ['patch-detail', user?.id, location.patchId],
    queryFn: ({ signal }) => api.patches.get(location.patchId, { signal }),
    enabled: Boolean(user?.id && location.patchId),
  });

  const refreshWorkspace = () => {
    queryClient.invalidateQueries({ queryKey: ['workspace', user?.id, projectId] });
    if (resolvedSceneId) {
      queryClient.invalidateQueries({ queryKey: ['scene-workspace', user?.id, resolvedSceneId] });
      queryClient.invalidateQueries({ queryKey: ['scene-evidence', user?.id, resolvedSceneId] });
      queryClient.invalidateQueries({ queryKey: ['scenes', user?.id, resolvedSceneId, 'jobs'] });
      queryClient.invalidateQueries({ queryKey: ['scenes', user?.id, resolvedSceneId, 'artifacts'] });
    }
    if (visibleJob?.id) queryClient.invalidateQueries({ queryKey: ['job-events', user?.id, visibleJob.id] });
    queryClient.invalidateQueries({ queryKey: ['projects', user?.id] });
  };

  const createScene = useMutation({
    mutationFn: (name) => api.scenes.create(projectId, { name }),
    onSuccess: (scene) => {
      setSceneName('');
      setIsAddingScene(false);
      location.update({ tab: 'scenes', scene: scene?.id || null, patch: null });
      refreshWorkspace();
    },
  });
  const cancelJob = useMutation({
    mutationFn: (jobId) => api.jobs.cancel(jobId),
    onSuccess: refreshWorkspace,
  });
  const reprocessScene = useMutation({
    mutationFn: (sceneId) => api.scenes.reprocess(sceneId),
    onSuccess: refreshWorkspace,
  });
  const previewArtifact = useMutation({
    mutationFn: (artifact) => api.artifacts.preview(artifact.id).then((grant) => ({ artifact, grant })),
    onSuccess: setPreview,
  });

  useEffect(() => {
    if (!preview?.grant?.expires_at) return undefined;
    const delay = Math.max(0, new Date(preview.grant.expires_at).getTime() - Date.now());
    const timer = globalThis.setTimeout(() => setPreview(null), delay);
    return () => globalThis.clearTimeout(timer);
  }, [preview]);

  useEffect(() => {
    if (!location.patchId || !patchQuery.data) return;
    if (String(patchQuery.data.project_id) !== String(projectId) || String(patchQuery.data.scene_id) !== String(resolvedSceneId)) {
      location.update({ patch: null });
      return;
    }
    if (!patchQuery.data.preview_artifact || openedPatchId === location.patchId) return;
    if (preview?.artifact?.id === patchQuery.data.preview_artifact.id || previewArtifact.isPending) return;
    setOpenedPatchId(location.patchId);
    previewArtifact.mutate(patchQuery.data.preview_artifact);
  }, [location.patchId, patchQuery.data, preview?.artifact?.id, previewArtifact, projectId, resolvedSceneId, openedPatchId]);

  if (projectQuery.isPending) return <WorkspaceState title="Loading workspace..." />;
  if (projectQuery.isError) {
    return <WorkspaceState title="Project unavailable" detail={projectQuery.error.message} action={() => navigate('/dashboard')} />;
  }

  const project = workspace?.project;
  const openPreview = (artifact) => {
    if (!artifact || previewArtifact.isPending) return;
    previewArtifact.mutate(artifact);
  };
  const selectScene = (sceneId) => location.update({ tab: 'scenes', scene: sceneId, patch: null });
  const submitScene = (event) => {
    event.preventDefault();
    const name = sceneName.trim();
    if (name && !createScene.isPending) createScene.mutate(name);
  };

  return (
    <main className="min-h-screen bg-[#09090b] text-zinc-200">
      <header className="sticky top-0 z-20 border-b border-white/[0.07] bg-[#09090b]/90 backdrop-blur-xl">
        <div className="mx-auto flex max-w-[96rem] items-center justify-between gap-4 px-4 py-3 sm:px-7">
          <div className="min-w-0">
            <Link to="/dashboard" className="inline-flex items-center gap-1.5 text-xs font-medium text-zinc-500 transition hover:text-white"><ArrowLeft size={14} /> All projects</Link>
            <div className="mt-1.5 flex min-w-0 items-center gap-2">
              <span className="grid h-7 w-7 shrink-0 place-items-center rounded-lg border border-sky-400/25 bg-sky-400/10 text-sky-300"><Radar size={15} /></span>
              <h1 className="truncate text-base font-semibold tracking-tight text-white sm:text-lg">{project?.name || 'Project workspace'}</h1>
            </div>
          </div>
          <div className="hidden text-right sm:block">
            <p className="text-[11px] font-semibold uppercase tracking-[0.14em] text-sky-300">Private SAR workspace</p>
            <p className="mt-0.5 text-xs text-zinc-500">{workspace?.counts?.total || 0} scene{workspace?.counts?.total === 1 ? '' : 's'}</p>
          </div>
        </div>
      </header>

      <div className="mx-auto max-w-[96rem] px-4 py-6 sm:px-7 sm:py-8">
        <nav aria-label="Workspace panels" className="mb-6 flex overflow-x-auto border-b border-white/[0.08]">
          {TABS.map(({ id, label, icon: Icon }) => (
            <button key={id} type="button" onClick={() => location.update({ tab: id, patch: null })} className={`inline-flex shrink-0 items-center gap-2 border-b-2 px-4 py-3 text-xs font-semibold transition ${location.tab === id ? 'border-sky-400 text-white' : 'border-transparent text-zinc-500 hover:text-zinc-300'}`}>
              <Icon size={14} /> {label}
            </button>
          ))}
        </nav>

        {location.tab === 'overview' && <OverviewPanel workspace={workspace} onSelectScene={selectScene} onOpenScenes={() => location.update({ tab: 'scenes' })} />}
        {location.tab === 'scenes' && (
          <ScenesPanel
            scenes={workspace?.scenes || []}
            selectedSceneId={resolvedSceneId}
            selectedScene={selectedScene}
            isSceneLoading={sceneQuery.isPending}
            sceneError={sceneQuery.error}
            events={eventsQuery.data?.items || []}
            onSelectScene={selectScene}
            onOpenPreview={openPreview}
            onOpenPatch={(patchId) => { setOpenedPatchId(null); location.update({ tab: 'scenes', scene: resolvedSceneId, patch: patchId }); }}
            onAddScene={() => setIsAddingScene(true)}
            onCancelJob={() => visibleJob?.id && cancelJob.mutate(visibleJob.id)}
            onRetry={() => resolvedSceneId && reprocessScene.mutate(resolvedSceneId)}
            actionPending={cancelJob.isPending || reprocessScene.isPending}
            actionError={cancelJob.error || reprocessScene.error}
            userId={user?.id}
            projectId={projectId}
            onUploadStateChange={refreshWorkspace}
          />
        )}
        {location.tab === 'evidence' && <EvidenceSearchPanel api={api} projectId={projectId} scenes={workspace?.scenes || []} selectedSceneId={resolvedSceneId} onSelectScene={(sceneId) => location.update({ tab: 'scenes', scene: sceneId, patch: null })} onOpenPatch={(patchId, sceneId) => { setOpenedPatchId(null); location.update({ tab: 'scenes', scene: sceneId, patch: patchId }); }} />}
        {location.tab === 'ask' && <GroundedChatPanel api={api} userId={user?.id} projectId={projectId} scenes={workspace?.scenes || []} selectedSceneId={resolvedSceneId} conversationId={location.conversationId} onConversationChange={(conversationId) => location.update({ conversation: conversationId })} onOpenPatch={(patchId, sceneId) => { setOpenedPatchId(null); location.update({ tab: 'scenes', scene: sceneId, patch: patchId }); }} onOpenPreview={openPreview} onOpenScene={(sceneId) => location.update({ tab: 'scenes', scene: sceneId, patch: null })} />}
      </div>

      {isAddingScene && (
        <div className="fixed inset-0 z-40 grid place-items-center bg-black/65 p-4" role="dialog" aria-modal="true" aria-labelledby="new-scene-title">
          <form onSubmit={submitScene} className="w-full max-w-md rounded-2xl border border-white/[0.1] bg-[#141417] p-5 shadow-2xl">
            <div className="flex items-start justify-between gap-4">
              <div><h2 id="new-scene-title" className="text-base font-semibold text-white">Add a scene</h2><p className="mt-1 text-sm text-zinc-500">Create its private record before uploading SAR input.</p></div>
              <button type="button" onClick={() => setIsAddingScene(false)} className="rounded-lg p-1 text-zinc-500 hover:bg-white/[0.06] hover:text-white" aria-label="Close"><X size={17} /></button>
            </div>
            <label htmlFor="scene-name" className="mt-5 block text-xs font-semibold text-zinc-300">Scene name</label>
            <input id="scene-name" autoFocus value={sceneName} maxLength={160} onChange={(event) => setSceneName(event.target.value)} placeholder="e.g. S1A_IW_20240605" className="mt-2 w-full rounded-lg border border-white/[0.1] bg-[#09090b] px-3 py-2.5 text-sm text-white outline-none placeholder:text-zinc-600 focus:border-sky-400/60 focus:ring-2 focus:ring-sky-400/10" />
            {createScene.error && <p className="mt-3 text-xs text-red-300">{createScene.error.message}</p>}
            <div className="mt-5 flex justify-end gap-2"><button type="button" onClick={() => setIsAddingScene(false)} className="rounded-lg px-3 py-2 text-xs font-semibold text-zinc-400 hover:bg-white/[0.05] hover:text-white">Cancel</button><button type="submit" disabled={!sceneName.trim() || createScene.isPending} className="rounded-lg bg-sky-500 px-3 py-2 text-xs font-semibold text-white transition hover:bg-sky-400 disabled:cursor-not-allowed disabled:opacity-45">{createScene.isPending ? 'Adding...' : 'Add scene'}</button></div>
          </form>
        </div>
      )}

      {preview && <PreviewDialog artifact={preview.artifact} grant={preview.grant} onClose={() => setPreview(null)} />}
      {previewArtifact.error && <div className="fixed bottom-5 right-5 z-50 max-w-sm rounded-lg border border-red-500/30 bg-red-950/90 px-4 py-3 text-sm text-red-100 shadow-xl">Could not open preview: {previewArtifact.error.message}</div>}
    </main>
  );
}

function OverviewPanel({ workspace, onSelectScene, onOpenScenes }) {
  const counts = workspace?.counts || {};
  const cards = [
    ['Total scenes', counts.total || 0, Files],
    ['Processing', (counts.queued || 0) + (counts.processing || 0) + (counts.uploading || 0), Activity],
    ['Ready', counts.ready || 0, CheckCircle2],
    ['Needs attention', counts.failed || 0, AlertTriangle],
  ];
  return (
    <section>
      <div className="flex flex-col justify-between gap-4 border-b border-white/[0.08] pb-6 sm:flex-row sm:items-end">
        <div><p className="text-[11px] font-bold uppercase tracking-[0.16em] text-sky-300">Project overview</p><h2 className="mt-2 text-2xl font-semibold tracking-tight text-white">Scene lifecycle at a glance</h2><p className="mt-2 max-w-2xl text-sm leading-6 text-zinc-500">Everything below is loaded from your private project, scene, job, and evidence records.</p></div>
        <button type="button" onClick={onOpenScenes} className="inline-flex items-center justify-center gap-2 rounded-lg border border-white/[0.1] bg-white/[0.04] px-3 py-2 text-xs font-semibold text-zinc-200 transition hover:bg-white/[0.08]"><Layers3 size={14} /> Manage scenes</button>
      </div>
      <div className="mt-6 grid grid-cols-2 gap-3 lg:grid-cols-4">{cards.map(([label, value, Icon]) => <div key={label} className="rounded-xl border border-white/[0.08] bg-[#111114] p-4"><Icon size={16} className="text-sky-300" /><p className="mt-5 text-2xl font-semibold text-white">{value}</p><p className="mt-1 text-xs text-zinc-500">{label}</p></div>)}</div>
      <div className="mt-7 rounded-xl border border-white/[0.08] bg-[#111114]">
        <div className="flex items-center justify-between border-b border-white/[0.07] px-4 py-3"><div><h3 className="text-sm font-semibold text-white">Recent scenes</h3><p className="mt-0.5 text-xs text-zinc-500">Select a scene to inspect its durable state.</p></div><button type="button" onClick={onOpenScenes} className="text-xs font-semibold text-sky-300 hover:text-sky-200">View all</button></div>
        {(workspace?.scenes || []).length === 0 ? <div className="p-8 text-center text-sm text-zinc-500">No scenes yet. Add one from the Scenes panel.</div> : <ul className="divide-y divide-white/[0.06]">{workspace.scenes.slice(0, 6).map((item) => <li key={item.scene.id}><button type="button" onClick={() => onSelectScene(item.scene.id)} className="flex w-full items-center justify-between gap-4 px-4 py-3.5 text-left transition hover:bg-white/[0.03]"><div className="min-w-0"><p className="truncate text-sm font-medium text-zinc-200">{item.scene.name}</p><p className="mt-1 text-xs text-zinc-600">{item.scene.sensor || 'SAR source pending'} · {item.scene.acquisition_time ? formatDate(item.scene.acquisition_time) : 'Acquisition pending'}</p></div><StatusPill status={item.scene.status} /></button></li>)}</ul>}
      </div>
    </section>
  );
}

function ScenesPanel({ scenes, selectedSceneId, selectedScene, isSceneLoading, sceneError, events, onSelectScene, onOpenPreview, onOpenPatch, onAddScene, onCancelJob, onRetry, actionPending, actionError, userId, projectId, onUploadStateChange }) {
  return (
    <section className="grid gap-5 xl:grid-cols-[minmax(17rem,0.8fr)_minmax(0,1.7fr)]">
      <aside className="overflow-hidden rounded-xl border border-white/[0.08] bg-[#111114] xl:sticky xl:top-24 xl:max-h-[calc(100vh-8rem)] xl:overflow-y-auto">
        <div className="flex items-center justify-between border-b border-white/[0.07] px-4 py-3"><div><h2 className="text-sm font-semibold text-white">Scenes</h2><p className="mt-0.5 text-xs text-zinc-500">Private project assets</p></div><button type="button" onClick={onAddScene} className="grid h-8 w-8 place-items-center rounded-lg border border-sky-400/25 bg-sky-400/10 text-sky-200 transition hover:bg-sky-400/20" aria-label="Add scene"><Plus size={15} /></button></div>
        {scenes.length === 0 ? <div className="p-7 text-center"><FileImage className="mx-auto text-zinc-600" size={23} /><p className="mt-3 text-sm font-medium text-zinc-300">No scenes yet</p><button type="button" onClick={onAddScene} className="mt-3 text-xs font-semibold text-sky-300 hover:text-sky-200">Add your first scene</button></div> : <ul className="divide-y divide-white/[0.06]">{scenes.map((item) => <li key={item.scene.id}><button type="button" onClick={() => onSelectScene(item.scene.id)} className={`w-full px-4 py-3.5 text-left transition ${selectedSceneId === item.scene.id ? 'bg-sky-400/[0.08] shadow-[inset_2px_0_0_#38bdf8]' : 'hover:bg-white/[0.03]'}`}><div className="flex items-start justify-between gap-3"><p className="min-w-0 truncate text-sm font-medium text-zinc-200">{item.scene.name}</p><StatusPill status={item.scene.status} /></div><p className="mt-1.5 truncate text-xs text-zinc-600">{item.active_job ? `${readable(item.active_job.stage)} · ${item.active_job.progress}%` : (item.evidence_status === 'ready' ? 'Evidence ready' : 'Awaiting processing')}</p></button></li>)}</ul>}
      </aside>
      <div className="min-w-0">{isSceneLoading && <WorkspaceMessage title="Loading scene details" detail="Reading the latest durable scene state..." icon={LoaderCircle} />}{sceneError && <WorkspaceMessage title="Scene unavailable" detail={sceneError.message} icon={AlertTriangle} />}{!isSceneLoading && !sceneError && !selectedScene && <WorkspaceMessage title="Select a scene" detail="Choose a scene to inspect its source, processing, artifacts, and evidence." icon={Layers3} />}{selectedScene && <SceneDetailPanel sceneDetail={selectedScene} events={events} onOpenPreview={onOpenPreview} onOpenPatch={onOpenPatch} onCancelJob={onCancelJob} onRetry={onRetry} actionPending={actionPending} actionError={actionError} userId={userId} projectId={projectId} onUploadStateChange={onUploadStateChange} />}</div>
    </section>
  );
}

function SceneDetailPanel({ sceneDetail, events, onOpenPreview, onOpenPatch, onCancelJob, onRetry, actionPending, actionError, userId, projectId, onUploadStateChange }) {
  const { scene, active_job: activeJob, latest_job: latestJob } = sceneDetail;
  const job = activeJob || latestJob;
  const retryable = ['failed', 'cancelled'].includes(String(scene.status));
  return (
    <div className="space-y-5">
      <section className="rounded-xl border border-white/[0.08] bg-[#111114] p-5">
        <div className="flex flex-col justify-between gap-4 sm:flex-row sm:items-start"><div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><h2 className="truncate text-xl font-semibold tracking-tight text-white">{scene.name}</h2><StatusPill status={scene.status} /></div><p className="mt-2 text-sm text-zinc-500">Created {formatDate(scene.created_at, true)} · {scene.sensor || 'Sensor metadata pending'}</p></div><div className="flex flex-wrap gap-2">{activeJob && <button type="button" disabled={actionPending} onClick={onCancelJob} className="rounded-lg border border-amber-400/25 bg-amber-400/10 px-3 py-2 text-xs font-semibold text-amber-200 transition hover:bg-amber-400/20 disabled:opacity-45">Cancel processing</button>}{retryable && <button type="button" disabled={actionPending} onClick={onRetry} className="inline-flex items-center gap-1.5 rounded-lg border border-sky-400/25 bg-sky-400/10 px-3 py-2 text-xs font-semibold text-sky-100 transition hover:bg-sky-400/20 disabled:opacity-45"><RefreshCw size={13} /> Retry processing</button>}</div></div>
        {actionError && <p className="mt-4 rounded-lg border border-red-500/25 bg-red-500/10 px-3 py-2 text-xs text-red-200">{actionError.message}</p>}
        {scene.failure_detail && <p className="mt-4 rounded-lg border border-red-500/25 bg-red-500/10 px-3 py-2 text-xs leading-5 text-red-200"><span className="font-semibold">{scene.failure_code || 'Processing failed'}:</span> {scene.failure_detail}</p>}
        <div className="mt-5 grid gap-3 sm:grid-cols-3"><Meta label="Acquisition" value={scene.acquisition_time ? formatDate(scene.acquisition_time, true) : 'Not provided'} /><Meta label="Polarizations" value={(scene.polarizations || []).join(', ') || 'Not provided'} /><Meta label="Patches" value={`${sceneDetail.patch_count} total · ${sceneDetail.preview_patch_count} previewable`} /></div>
      </section>

      {job && <JobStatusCard jobId={job.id} initialJob={job} userId={userId} onTerminal={onUploadStateChange} />}

      <div className="grid gap-5 2xl:grid-cols-2">
        <section className="rounded-xl border border-white/[0.08] bg-[#111114] p-5"><div className="flex items-center justify-between gap-3"><div><h3 className="text-sm font-semibold text-white">Overview preview</h3><p className="mt-1 text-xs text-zinc-500">Generated from the durable scene output.</p></div>{sceneDetail.overview ? <button type="button" onClick={() => onOpenPreview(sceneDetail.overview)} className="inline-flex items-center gap-1.5 rounded-lg border border-sky-400/25 bg-sky-400/10 px-3 py-2 text-xs font-semibold text-sky-200 hover:bg-sky-400/20"><Eye size={13} /> Open preview</button> : <StatusPill status={scene.status === 'ready' ? 'missing' : 'pending'} />}</div><p className="mt-5 text-xs leading-5 text-zinc-500">Preview URLs are generated only when opened and expire automatically. Source raster files remain private.</p></section>
        <section className="rounded-xl border border-white/[0.08] bg-[#111114] p-5"><div className="flex items-center gap-2"><ShieldCheck size={16} className="text-sky-300" /><div><h3 className="text-sm font-semibold text-white">Evidence record</h3><p className="mt-1 text-xs text-zinc-500">{readable(sceneDetail.evidence_status, 'missing')}</p></div></div><p className="mt-5 text-xs leading-5 text-zinc-500">Evidence distinguishes source metadata, land/water context, generated observations, and validated detector facts.</p></section>
      </div>

      <section className="rounded-xl border border-white/[0.08] bg-[#111114]"><div className="border-b border-white/[0.07] px-5 py-4"><h3 className="text-sm font-semibold text-white">Artifacts</h3><p className="mt-1 text-xs text-zinc-500">Durable records only; storage locations are never exposed to the browser.</p></div><ul className="divide-y divide-white/[0.06]">{sceneDetail.artifacts.length === 0 ? <li className="px-5 py-7 text-sm text-zinc-500">No durable artifacts have been created yet.</li> : sceneDetail.artifacts.map((artifact) => <li key={artifact.id} className="flex flex-wrap items-center justify-between gap-3 px-5 py-3"><div className="min-w-0"><p className="text-sm font-medium capitalize text-zinc-300">{readable(artifact.kind)}</p><p className="mt-1 text-xs text-zinc-600">{artifact.content_type || 'Unknown type'} · {formatBytes(artifact.size_bytes)}</p></div>{['overview', 'thumbnail', 'patch_preview'].includes(artifact.kind) && artifact.status === 'available' ? <button type="button" onClick={() => onOpenPreview(artifact)} className="inline-flex items-center gap-1.5 rounded-md border border-white/[0.1] px-2.5 py-1.5 text-xs font-semibold text-zinc-300 hover:bg-white/[0.06]"><Eye size={13} /> Preview</button> : <StatusPill status={artifact.status} />}</li>)}</ul></section>

      <section className="rounded-xl border border-white/[0.08] bg-[#111114]"><div className="border-b border-white/[0.07] px-5 py-4"><h3 className="text-sm font-semibold text-white">Previewable patches</h3><p className="mt-1 text-xs text-zinc-500">Patch bounds and SARCLIP provenance are loaded from the scene record.</p></div>{sceneDetail.patches.length === 0 ? <p className="px-5 py-7 text-sm text-zinc-500">No patch previews are available yet.</p> : <div className="grid gap-3 p-4 sm:grid-cols-2">{sceneDetail.patches.map((patch) => <button key={patch.id} type="button" onClick={() => onOpenPatch(patch.id)} className="rounded-lg border border-white/[0.08] bg-black/10 p-3 text-left transition hover:border-sky-400/35 hover:bg-sky-400/[0.04]"><div className="flex items-start justify-between gap-3"><p className="truncate text-xs font-semibold text-zinc-200">Patch {String(patch.id).slice(0, 8)}</p><MapPin size={14} className="shrink-0 text-sky-300" /></div><p className="mt-2 text-[11px] leading-5 text-zinc-500">Rows {patch.bounds.row_start}–{patch.bounds.row_end} · Cols {patch.bounds.col_start}–{patch.bounds.col_end}</p><p className="mt-1 text-[11px] text-zinc-600">{patch.model_name || 'Model pending'} {patch.model_version ? `· ${patch.model_version}` : ''}</p></button>)}</div>}</section>

      <section className="rounded-xl border border-white/[0.08] bg-[#111114]"><div className="border-b border-white/[0.07] px-5 py-4"><h3 className="text-sm font-semibold text-white">Processing history</h3><p className="mt-1 text-xs text-zinc-500">Durable events persist after navigation and API restarts.</p></div>{events.length === 0 ? <p className="px-5 py-7 text-sm text-zinc-500">No worker events have been recorded for this job yet.</p> : <ol className="divide-y divide-white/[0.06]">{events.map((event) => <li key={event.id} className="flex gap-3 px-5 py-3"><Clock3 size={15} className="mt-0.5 shrink-0 text-zinc-600" /><div><p className="text-xs font-semibold text-zinc-300">{readable(event.event_type)} <span className="font-normal text-zinc-600">· {readable(event.stage)}</span></p><p className="mt-1 text-[11px] text-zinc-600">{formatDate(event.created_at, true)} · {event.progress}%</p>{event.message && <p className="mt-1 text-xs text-zinc-500">{event.message}</p>}</div></li>)}</ol>}</section>

      <SceneUploadPanel projectId={projectId} scene={scene} userId={userId} onComplete={onUploadStateChange} onTerminal={onUploadStateChange} />
    </div>
  );
}

function Meta({ label, value }) { return <div className="rounded-lg border border-white/[0.07] bg-black/10 p-3"><p className="text-[11px] font-semibold uppercase tracking-[0.08em] text-zinc-600">{label}</p><p className="mt-1.5 truncate text-xs text-zinc-300" title={value}>{value}</p></div>; }

function EvidencePanel({ scene, evidenceQuery, onOpenPreview, onSelectScene }) {
  if (!scene) return <WorkspaceMessage title="Select a scene first" detail="Evidence is scoped to one private scene. Choose one from the Scenes panel." action={onSelectScene} icon={ShieldCheck} />;
  if (evidenceQuery.isPending) return <WorkspaceMessage title="Loading evidence" detail="Reading the current durable evidence record..." icon={LoaderCircle} />;
  if (evidenceQuery.isError) return <WorkspaceMessage title="Evidence unavailable" detail={evidenceQuery.error.message} action={() => evidenceQuery.refetch()} icon={AlertTriangle} />;
  const evidence = evidenceQuery.data;
  if (!evidence?.record) return <WorkspaceMessage title="No evidence record yet" detail={evidence?.status === 'unavailable' ? 'The evidence record is present but its private sidecar is unavailable. Reprocess the scene to rebuild it.' : 'Processing must complete before a provenance-aware evidence record is available.'} icon={ShieldCheck} />;
  const overview = scene.overview;
  return <section><div className="border-b border-white/[0.08] pb-6"><p className="text-[11px] font-bold uppercase tracking-[0.16em] text-sky-300">Evidence browser</p><h2 className="mt-2 text-2xl font-semibold tracking-tight text-white">{scene.scene.name}</h2><p className="mt-2 max-w-2xl text-sm leading-6 text-zinc-500">This M4 panel presents durable evidence only. Semantic retrieval and evidence-grounded chat are activated in M5; no mock search results are shown.</p></div><div className="mt-6 grid gap-4 xl:grid-cols-2">{evidence.record.sections.map((section) => <EvidenceCard key={section.kind} section={section} overview={overview} onOpenPreview={onOpenPreview} />)}</div>{evidence.record.limitations.length > 0 && <section className="mt-5 rounded-xl border border-amber-400/20 bg-amber-400/[0.06] p-4"><div className="flex items-center gap-2 text-sm font-semibold text-amber-100"><AlertTriangle size={16} /> Evidence limitations</div><ul className="mt-3 list-disc space-y-1 pl-5 text-xs leading-5 text-amber-100/70">{evidence.record.limitations.map((item) => <li key={item}>{item}</li>)}</ul></section>}</section>;
}

function EvidenceCard({ section, overview, onOpenPreview }) {
  const colors = { metadata: 'text-sky-200 border-sky-400/20 bg-sky-400/[0.05]', land_water_estimate: 'text-cyan-200 border-cyan-400/20 bg-cyan-400/[0.05]', model_observation: 'text-violet-200 border-violet-400/20 bg-violet-400/[0.05]', validated_detector_evidence: 'text-emerald-200 border-emerald-400/20 bg-emerald-400/[0.05]' };
  const objects = section.values?.objects;
  return <article className={`rounded-xl border p-5 ${colors[section.kind] || 'border-white/[0.08] bg-[#111114]'}`}><div className="flex items-start justify-between gap-4"><div><p className="text-[11px] font-bold uppercase tracking-[0.12em] opacity-70">{readable(section.kind)}</p><h3 className="mt-1 text-sm font-semibold text-white">{section.title}</h3></div>{section.source?.artifact_id && overview?.id === section.source.artifact_id && <button type="button" onClick={() => onOpenPreview(overview)} className="rounded-md border border-white/[0.12] bg-black/10 p-2 text-current hover:bg-black/20" aria-label="Open source preview"><Eye size={14} /></button>}</div>{section.kind === 'validated_detector_evidence' && Array.isArray(objects) ? <div className="mt-4 space-y-2">{objects.length === 0 ? <p className="text-xs leading-5 text-zinc-400">No validated detections were present in the approved detector sidecar.</p> : objects.map((object) => <div key={object.id || `${object.label}-${object.confidence}`} className="rounded-lg border border-white/[0.08] bg-black/10 p-3"><p className="text-xs font-semibold text-zinc-100">{object.label} <span className="font-normal text-zinc-500">· {(object.confidence * 100).toFixed(1)}%</span></p><p className="mt-1 text-[11px] text-zinc-500">Bounds: {Object.values(object.bounding_box_px || {}).join(', ')}</p></div>)}</div> : <dl className="mt-4 space-y-2">{Object.entries(section.values || {}).map(([key, value]) => <div key={key} className="grid grid-cols-[minmax(6rem,0.7fr)_minmax(0,1.3fr)] gap-3 text-xs"><dt className="capitalize text-zinc-500">{readable(key)}</dt><dd className="break-words text-zinc-200">{typeof value === 'object' ? JSON.stringify(value) : String(value)}</dd></div>)}</dl>}<p className="mt-4 text-[11px] leading-5 text-zinc-500">{section.limitations?.[0] || section.provenance?.source || 'Provenance retained with this scene.'}</p></article>;
}

function AskPanel() { return <WorkspaceMessage title="Ask arrives with M5" detail="This workspace will host project-scoped, history-preserving, evidence-cited chat. The legacy session chat is deliberately not used here because it does not meet the private project boundary." icon={Bot} />; }

function PreviewDialog({ artifact, grant, onClose }) { return <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/80 p-4" role="dialog" aria-modal="true" aria-labelledby="preview-title"><div className="flex max-h-[92vh] w-full max-w-5xl flex-col overflow-hidden rounded-2xl border border-white/[0.12] bg-[#111114] shadow-2xl"><div className="flex items-center justify-between border-b border-white/[0.08] px-4 py-3"><div className="min-w-0"><h2 id="preview-title" className="truncate text-sm font-semibold capitalize text-white">{readable(artifact.kind)} preview</h2><p className="mt-0.5 text-xs text-zinc-500">Private link expires {formatDate(grant.expires_at, true)}</p></div><button type="button" onClick={onClose} className="rounded-lg p-2 text-zinc-500 hover:bg-white/[0.06] hover:text-white" aria-label="Close preview"><X size={17} /></button></div><div className="min-h-0 overflow-auto bg-black p-3"><img src={grant.url} alt={`${readable(artifact.kind)} preview`} referrerPolicy="no-referrer" className="mx-auto max-h-[75vh] max-w-full object-contain" /></div></div></div>; }

function WorkspaceState({ title, detail, action }) { return <main className="grid min-h-screen place-items-center bg-[#09090b] p-6 text-center"><section><h1 className="text-lg font-semibold text-white">{title}</h1>{detail && <p className="mt-2 text-sm text-zinc-500">{detail}</p>}{action && <button type="button" onClick={action} className="mt-5 text-sm font-semibold text-sky-300 hover:text-sky-200">Return to dashboard</button>}</section></main>; }
