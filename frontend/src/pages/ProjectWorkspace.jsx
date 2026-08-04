import { Suspense, lazy, useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  AlertTriangle, ArrowLeft, Clock3, Eye, FileImage, FileUp, Info, Layers3,
  LoaderCircle, MapPin, MessageSquare, Plus, Radar, RefreshCw, Satellite, Search,
  ShieldCheck, X,
} from 'lucide-react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { useAuth } from '../auth/AuthProvider';
import JobStatusCard, { isTerminalJobStatus } from '../components/JobStatusCard';
import SceneUploadPanel from '../components/SceneUploadPanel';
import EvidenceSearchPanel from '../components/EvidenceSearchPanel';
import GroundedChatPanel from '../components/GroundedChatPanel';

// Lazy so Vite code-splits maplibre (~230 kB gzipped) out of the main bundle.
// Only a user who chooses the Copernicus path ever downloads it.
const CopernicusSearchPanel = lazy(() => import('../components/CopernicusSearchPanel'));

// Three verbs, not four panels. "Overview" listed the same scenes as "Scenes"
// behind a second click and four counters, so a first-time user met a summary of
// a list before they met the list. Its counts now sit above the scene list where
// they describe something visible.
const TABS = [
  { id: 'scenes', label: 'Scenes', icon: Layers3 },
  { id: 'evidence', label: 'Search', icon: Search },
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
  // Old links carrying ?tab=overview still resolve, to the list they summarised.
  const tab = TABS.some((item) => item.id === params.get('tab')) ? params.get('tab') : 'scenes';
  const sceneId = params.get('scene') || null;
  const patchId = params.get('patch') || null;
  const conversationId = params.get('conversation') || null;
  // Which source the user picked for a scene that does not have one yet. No
  // scenes column is needed: the upload panel already hides itself as soon as
  // a job exists, and processing_jobs.stage identifies the path after that.
  const source = params.get('source') === 'copernicus' ? 'copernicus' : null;
  const update = (next) => {
    const value = new URLSearchParams(params);
    Object.entries(next).forEach(([key, entry]) => {
      if (entry === null || entry === undefined || entry === '') value.delete(key);
      else value.set(key, entry);
    });
    setParams(value, { replace: true });
  };
  return { tab, sceneId, patchId, conversationId, source, update };
}

export default function ProjectWorkspace() {
  const { projectId } = useParams();
  const { api, user } = useAuth();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const location = useWorkspaceLocation();
  const [isAddingScene, setIsAddingScene] = useState(false);
  const [sceneName, setSceneName] = useState('');
  const [sourceMode, setSourceMode] = useState('upload');
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

  // Shared cache key with CopernicusSearchPanel, so opening the modal and then
  // the panel costs one request, not two.
  const providersQuery = useQuery({
    queryKey: ['acquisition-providers'],
    queryFn: ({ signal }) => api.acquisitions.providers({ signal }),
    enabled: Boolean(user?.id),
    staleTime: Infinity,
    retry: false,
  });
  const copernicusEnabled = providersQuery.data?.copernicus?.enabled === true;

  const createScene = useMutation({
    mutationFn: (name) => api.scenes.create(projectId, { name }),
    onSuccess: (scene) => {
      setSceneName('');
      setIsAddingScene(false);
      location.update({
        tab: 'scenes',
        scene: scene?.id || null,
        patch: null,
        source: sourceMode === 'copernicus' ? 'copernicus' : null,
      });
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
  // Clear the source choice when moving to another scene: it belongs to the
  // scene it was picked for, and carrying it over would silently show the
  // Copernicus panel for a scene the user meant to upload to.
  const selectScene = (sceneId) => location.update({ tab: 'scenes', scene: sceneId, patch: null, source: null });
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

        {location.tab === 'scenes' && (
          <ScenesPanel
            api={api}
            counts={workspace?.counts || {}}
            onAskAboutScene={(sceneId) => location.update({ tab: 'ask', scene: sceneId, patch: null })}
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
            sourceMode={location.source}
            onUseUpload={() => location.update({ source: null })}
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
              <div><h2 id="new-scene-title" className="text-base font-semibold text-white">Add a scene</h2><p className="mt-1 text-sm text-zinc-500">Name it first, then choose where the SAR file comes from.</p></div>
              <button type="button" onClick={() => setIsAddingScene(false)} className="rounded-lg p-1 text-zinc-500 hover:bg-white/[0.06] hover:text-white" aria-label="Close"><X size={17} /></button>
            </div>
            <label htmlFor="scene-name" className="mt-5 block text-xs font-semibold text-zinc-300">Scene name</label>
            <input id="scene-name" autoFocus value={sceneName} maxLength={160} onChange={(event) => setSceneName(event.target.value)} placeholder="e.g. S1A_IW_20240605" className="mt-2 w-full rounded-lg border border-white/[0.1] bg-[#09090b] px-3 py-2.5 text-sm text-white outline-none placeholder:text-zinc-600 focus:border-sky-400/60 focus:ring-2 focus:ring-sky-400/10" />

            <fieldset className="mt-5">
              <legend className="text-xs font-semibold text-zinc-300">Where is the file?</legend>
              <div className="mt-2 space-y-2">
                <SourceOption
                  icon={FileUp}
                  title="Upload from my computer"
                  detail="A Sentinel-1 GRD ZIP or one/two GeoTIFFs you already have."
                  selected={sourceMode === 'upload'}
                  onSelect={() => setSourceMode('upload')}
                />
                <SourceOption
                  icon={Satellite}
                  title="Fetch from Copernicus"
                  detail={copernicusEnabled
                    ? 'Draw an area and a date range; the server downloads the scene.'
                    : 'Unavailable on this server right now.'}
                  selected={sourceMode === 'copernicus'}
                  disabled={!copernicusEnabled}
                  onSelect={() => setSourceMode('copernicus')}
                />
              </div>
            </fieldset>

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

function SourceOption({ icon: Icon, title, detail, selected, disabled, onSelect }) {
  return (
    <button
      type="button"
      onClick={onSelect}
      disabled={disabled}
      aria-pressed={selected}
      className={`flex w-full items-start gap-3 rounded-lg border px-3.5 py-3 text-left transition ${
        disabled
          ? 'cursor-not-allowed border-white/[0.06] bg-white/[0.01] opacity-55'
          : selected
            ? 'border-sky-400/50 bg-sky-400/[0.08]'
            : 'border-white/[0.1] bg-[#09090b] hover:border-sky-400/30 hover:bg-sky-400/[0.04]'
      }`}
    >
      <span className={`mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-lg border ${selected ? 'border-sky-400/30 bg-sky-400/10 text-sky-300' : 'border-white/[0.08] bg-white/[0.03] text-zinc-500'}`}>
        <Icon size={14} />
      </span>
      <span className="min-w-0">
        <span className="block text-xs font-semibold text-zinc-100">{title}</span>
        <span className="mt-0.5 block text-[11px] leading-5 text-zinc-500">{detail}</span>
      </span>
    </button>
  );
}

// Counts sit above the list they describe rather than in a tab of their own.
// "Needs attention" is the only one a user acts on, so it is the only one that
// changes colour; four equally loud tiles teach a reader to scan past all four.
function SceneCounts({ counts }) {
  const total = counts.total || 0;
  const working = (counts.queued || 0) + (counts.processing || 0) + (counts.uploading || 0);
  const ready = counts.ready || 0;
  const failed = counts.failed || 0;
  if (!total) return null;
  const items = [
    ['Scenes', total, 'text-zinc-200'],
    ['Ready', ready, 'text-emerald-300'],
    ...(working ? [['Processing', working, 'text-sky-300']] : []),
    ...(failed ? [['Needs attention', failed, 'text-amber-300']] : []),
  ];
  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-2 px-4 py-3 text-xs">
      {items.map(([label, value, tone]) => (
        <span key={label} className="inline-flex items-baseline gap-1.5">
          <span className={`text-sm font-semibold ${tone}`}>{value}</span>
          <span className="text-zinc-500">{label}</span>
        </span>
      ))}
    </div>
  );
}

function ScenesPanel({ api, counts, scenes, selectedSceneId, selectedScene, isSceneLoading, sceneError, events, onSelectScene, onOpenPreview, onOpenPatch, onAddScene, onCancelJob, onRetry, onAskAboutScene, actionPending, actionError, userId, projectId, sourceMode, onUseUpload, onUploadStateChange }) {
  return (
    <section className="grid gap-5 xl:grid-cols-[minmax(17rem,0.8fr)_minmax(0,1.7fr)]">
      <aside className="overflow-hidden rounded-xl border border-white/[0.08] bg-[#111114] xl:sticky xl:top-24 xl:max-h-[calc(100vh-8rem)] xl:overflow-y-auto">
        <div className="flex items-center justify-between border-b border-white/[0.07] px-4 py-3"><h2 className="text-sm font-semibold text-white">Scenes</h2><button type="button" onClick={onAddScene} className="inline-flex items-center gap-1.5 rounded-lg border border-sky-400/25 bg-sky-400/10 px-2.5 py-1.5 text-xs font-semibold text-sky-200 transition hover:bg-sky-400/20"><Plus size={14} /> Add</button></div>
        <div className="border-b border-white/[0.07]"><SceneCounts counts={counts} /></div>
        {scenes.length === 0 ? <div className="p-7 text-center"><FileImage className="mx-auto text-zinc-600" size={23} /><p className="mt-3 text-sm font-medium text-zinc-300">No scenes yet</p><button type="button" onClick={onAddScene} className="mt-3 text-xs font-semibold text-sky-300 hover:text-sky-200">Add your first scene</button></div> : <ul className="divide-y divide-white/[0.06]">{scenes.map((item) => <li key={item.scene.id}><button type="button" onClick={() => onSelectScene(item.scene.id)} className={`w-full px-4 py-3.5 text-left transition ${selectedSceneId === item.scene.id ? 'bg-sky-400/[0.08] shadow-[inset_2px_0_0_#38bdf8]' : 'hover:bg-white/[0.03]'}`}><div className="flex items-start justify-between gap-3"><p className="min-w-0 truncate text-sm font-medium text-zinc-200">{item.scene.name}</p><StatusPill status={item.scene.status} /></div><p className="mt-1.5 truncate text-xs text-zinc-600">{item.active_job ? `${readable(item.active_job.stage)} · ${item.active_job.progress}%` : (item.evidence_status === 'ready' ? 'Evidence ready' : 'Awaiting processing')}</p></button></li>)}</ul>}
      </aside>
      <div className="min-w-0">{isSceneLoading && <WorkspaceMessage title="Loading scene details" detail="Loading this scene..." icon={LoaderCircle} />}{sceneError && <WorkspaceMessage title="Scene unavailable" detail={sceneError.message} icon={AlertTriangle} />}{!isSceneLoading && !sceneError && !selectedScene && <WorkspaceMessage title="Select a scene" detail="Pick a scene to see its imagery, progress and evidence." icon={Layers3} />}{selectedScene && <SceneDetailPanel api={api} onAskAboutScene={onAskAboutScene} sceneDetail={selectedScene} events={events} onOpenPreview={onOpenPreview} onOpenPatch={onOpenPatch} onCancelJob={onCancelJob} onRetry={onRetry} actionPending={actionPending} actionError={actionError} userId={userId} projectId={projectId} sourceMode={sourceMode} onUseUpload={onUseUpload} onUploadStateChange={onUploadStateChange} />}</div>
    </section>
  );
}

// The overview is shown, not offered. It used to sit behind an "Open preview"
// button inside a card that described the picture in words, so the single most
// informative thing about a scene was one click away and easy to miss.
function SceneOverviewImage({ api, artifact, onOpenPreview }) {
  const artifactId = artifact?.id || null;
  const [grantAttempt, setGrantAttempt] = useState(0);
  const previewQuery = useQuery({
    queryKey: ['scene-overview-preview', artifactId, grantAttempt],
    enabled: Boolean(api && artifactId),
    queryFn: ({ signal }) => api.artifacts.preview(artifactId, { signal }),
    // Grants last 90 seconds; hold one for less than that or the <img> is handed
    // a dead URL over a file that is perfectly intact.
    staleTime: 45_000,
    gcTime: 45_000,
    refetchOnMount: 'always',
    refetchOnWindowFocus: true,
    retry: 2,
  });
  if (!artifactId) return null;
  const url = previewQuery.data?.url;
  return (
    <button
      type="button"
      onClick={() => onOpenPreview(artifact)}
      className="block w-full cursor-zoom-in overflow-hidden rounded-xl border border-white/[0.08] bg-black/30"
      title="Open full size"
    >
      {url ? (
        <img
          key={`${artifactId}-${grantAttempt}`}
          src={url}
          alt="Scene overview"
          className="block max-h-[26rem] w-full object-contain"
          onError={() => setGrantAttempt((attempt) => (attempt < 2 ? attempt + 1 : attempt))}
        />
      ) : (
        <div className="flex h-48 items-center justify-center gap-2 text-xs text-zinc-600">
          {previewQuery.isError ? 'Preview unavailable' : <><LoaderCircle size={14} className="animate-spin" /> Loading preview</>}
        </div>
      )}
    </button>
  );
}

function SceneDetailPanel({ api, sceneDetail, events, onOpenPreview, onOpenPatch, onCancelJob, onRetry, onAskAboutScene, actionPending, actionError, userId, projectId, sourceMode, onUseUpload, onUploadStateChange }) {
  const { scene, active_job: activeJob, latest_job: latestJob } = sceneDetail;
  const job = activeJob || latestJob;
  const retryable = ['failed', 'cancelled'].includes(String(scene.status));
  const isReady = String(scene.status) === 'ready';
  const needsSource = !isReady && !activeJob;

  return (
    <div className="space-y-5">
      <section className="rounded-xl border border-white/[0.08] bg-[#111114] p-5">
        <div className="flex flex-col justify-between gap-4 sm:flex-row sm:items-start">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="truncate text-xl font-semibold tracking-tight text-white">{scene.name}</h2>
              <StatusPill status={scene.status} />
            </div>
            <p className="mt-2 text-sm text-zinc-500">
              {scene.sensor || 'Sensor pending'}
              {scene.acquisition_time ? ` · captured ${formatDate(scene.acquisition_time)}` : ''}
              {(scene.polarizations || []).length ? ` · ${(scene.polarizations || []).join('/')}` : ''}
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            {isReady && (
              <button type="button" onClick={() => onAskAboutScene(scene.id)} className="inline-flex items-center gap-1.5 rounded-lg bg-sky-500 px-3 py-2 text-xs font-semibold text-white transition hover:bg-sky-400">
                <MessageSquare size={13} /> Ask about this scene
              </button>
            )}
            {activeJob && <button type="button" disabled={actionPending} onClick={onCancelJob} className="rounded-lg border border-amber-400/25 bg-amber-400/10 px-3 py-2 text-xs font-semibold text-amber-200 transition hover:bg-amber-400/20 disabled:opacity-45">Stop</button>}
            {retryable && <button type="button" disabled={actionPending} onClick={onRetry} className="inline-flex items-center gap-1.5 rounded-lg border border-sky-400/25 bg-sky-400/10 px-3 py-2 text-xs font-semibold text-sky-100 transition hover:bg-sky-400/20 disabled:opacity-45"><RefreshCw size={13} /> Try again</button>}
          </div>
        </div>
        {actionError && <p className="mt-4 rounded-lg border border-red-500/25 bg-red-500/10 px-3 py-2 text-xs text-red-200">{actionError.message}</p>}
        {scene.failure_detail && <p className="mt-4 rounded-lg border border-red-500/25 bg-red-500/10 px-3 py-2 text-xs leading-5 text-red-200"><span className="font-semibold">{readable(scene.failure_code) || 'Processing failed'}:</span> {scene.failure_detail}</p>}
        {sceneDetail.overview && <div className="mt-5"><SceneOverviewImage api={api} artifact={sceneDetail.overview} onOpenPreview={onOpenPreview} /></div>}
      </section>

      {job && <JobStatusCard jobId={job.id} initialJob={job} userId={userId} onTerminal={onUploadStateChange} />}

      {/* Getting a source in is the whole task when a scene has no raster yet,
          and noise once it does. Exactly one of these two panels is mounted:
          they share no storage, and the Copernicus one needs none because its
          idempotency key is durable in scene_acquisitions. */}
      {needsSource && (sourceMode === 'copernicus'
        ? (
          <Suspense fallback={<section className="rounded-xl border border-white/[0.08] bg-[#111114] p-5"><p className="flex items-center gap-2 text-xs text-zinc-500"><LoaderCircle size={14} className="animate-spin" /> Loading the Copernicus browser...</p></section>}>
            <CopernicusSearchPanel api={api} scene={scene} onStarted={onUploadStateChange} onUseUpload={onUseUpload} />
          </Suspense>
        )
        : <SceneUploadPanel projectId={projectId} scene={scene} userId={userId} onComplete={onUploadStateChange} onTerminal={onUploadStateChange} />)}

      {/* Everything a user consults rather than reads. Sizes, pixel bounds and a
          worker event log diagnose a bad run; they do not help use a good one. */}
      <details className="group rounded-xl border border-white/[0.08] bg-[#111114]">
        <summary className="flex cursor-pointer list-none items-center gap-2 px-5 py-3.5 text-sm font-semibold text-zinc-300 transition hover:text-white">
          <Info size={15} className="text-zinc-500" />
          Technical details
          <span className="ml-auto text-xs font-normal text-zinc-600 group-open:hidden">show</span>
          <span className="ml-auto hidden text-xs font-normal text-zinc-600 group-open:inline">hide</span>
        </summary>

        <div className="space-y-5 border-t border-white/[0.07] p-5">
          <div className="grid gap-3 sm:grid-cols-3">
            <Meta label="Captured" value={scene.acquisition_time ? formatDate(scene.acquisition_time, true) : 'Not provided'} />
            <Meta label="Polarizations" value={(scene.polarizations || []).join(', ') || 'Not provided'} />
            <Meta label="Patches" value={`${sceneDetail.patch_count} total · ${sceneDetail.preview_patch_count} previewable`} />
          </div>

          <div>
            <h4 className="text-xs font-semibold text-zinc-300">Files</h4>
            <ul className="mt-2 divide-y divide-white/[0.06] rounded-lg border border-white/[0.07]">
              {sceneDetail.artifacts.length === 0 ? <li className="px-4 py-5 text-xs text-zinc-500">Nothing generated yet.</li> : sceneDetail.artifacts.map((artifact) => (
                <li key={artifact.id} className="flex flex-wrap items-center justify-between gap-3 px-4 py-2.5">
                  <div className="min-w-0">
                    <p className="text-xs font-medium capitalize text-zinc-300">{readable(artifact.kind)}</p>
                    <p className="mt-0.5 text-[11px] text-zinc-600">{artifact.content_type || 'Unknown type'} · {formatBytes(artifact.size_bytes)}</p>
                  </div>
                  {(['overview', 'thumbnail', 'patch_preview'].includes(artifact.kind) || String(artifact.content_type || '').startsWith('image/')) && artifact.status === 'available'
                    ? <button type="button" onClick={() => onOpenPreview(artifact)} className="inline-flex items-center gap-1.5 rounded-md border border-white/[0.1] px-2.5 py-1.5 text-[11px] font-semibold text-zinc-300 hover:bg-white/[0.06]"><Eye size={12} /> View</button>
                    : <StatusPill status={artifact.status} />}
                </li>
              ))}
            </ul>
          </div>

          {sceneDetail.patches.length > 0 && (
            <div>
              <h4 className="text-xs font-semibold text-zinc-300">Patches</h4>
              <div className="mt-2 grid gap-2 sm:grid-cols-2">
                {sceneDetail.patches.map((patch) => (
                  <button key={patch.id} type="button" onClick={() => onOpenPatch(patch.id)} className="rounded-lg border border-white/[0.08] bg-black/10 p-2.5 text-left transition hover:border-sky-400/35 hover:bg-sky-400/[0.04]">
                    <div className="flex items-start justify-between gap-3">
                      <p className="truncate text-[11px] font-semibold text-zinc-200">Patch {String(patch.id).slice(0, 8)}</p>
                      <MapPin size={13} className="shrink-0 text-sky-300" />
                    </div>
                    <p className="mt-1.5 text-[11px] leading-5 text-zinc-500">Rows {patch.bounds.row_start}–{patch.bounds.row_end} · Cols {patch.bounds.col_start}–{patch.bounds.col_end}</p>
                  </button>
                ))}
              </div>
            </div>
          )}

          <div>
            <h4 className="text-xs font-semibold text-zinc-300">Processing history</h4>
            {events.length === 0 ? <p className="mt-2 text-xs text-zinc-500">No events recorded yet.</p> : (
              <ol className="mt-2 divide-y divide-white/[0.06] rounded-lg border border-white/[0.07]">
                {events.map((event) => (
                  <li key={event.id} className="flex gap-3 px-4 py-2.5">
                    <Clock3 size={14} className="mt-0.5 shrink-0 text-zinc-600" />
                    <div>
                      <p className="text-[11px] font-semibold text-zinc-300">{readable(event.event_type)} <span className="font-normal text-zinc-600">· {readable(event.stage)}</span></p>
                      <p className="mt-0.5 text-[11px] text-zinc-600">{formatDate(event.created_at, true)} · {event.progress}%</p>
                      {event.message && <p className="mt-0.5 text-[11px] text-zinc-500">{event.message}</p>}
                    </div>
                  </li>
                ))}
              </ol>
            )}
          </div>

          {!needsSource && <SceneUploadPanel projectId={projectId} scene={scene} userId={userId} onComplete={onUploadStateChange} onTerminal={onUploadStateChange} />}
        </div>
      </details>
    </div>
  );
}

function Meta({ label, value }) { return <div className="rounded-lg border border-white/[0.07] bg-black/10 p-3"><p className="text-[11px] font-semibold uppercase tracking-[0.08em] text-zinc-600">{label}</p><p className="mt-1.5 truncate text-xs text-zinc-300" title={value}>{value}</p></div>; }

function EvidencePanel({ scene, evidenceQuery, onOpenPreview, onSelectScene }) {
  if (!scene) return <WorkspaceMessage title="Select a scene first" detail="Evidence belongs to a single scene. Pick one from the Scenes tab." action={onSelectScene} icon={ShieldCheck} />;
  if (evidenceQuery.isPending) return <WorkspaceMessage title="Loading evidence" detail="Reading this scene's evidence..." icon={LoaderCircle} />;
  if (evidenceQuery.isError) return <WorkspaceMessage title="Evidence unavailable" detail={evidenceQuery.error.message} action={() => evidenceQuery.refetch()} icon={AlertTriangle} />;
  const evidence = evidenceQuery.data;
  if (!evidence?.record) return <WorkspaceMessage title="No evidence record yet" detail={evidence?.status === 'unavailable' ? 'The evidence record exists but its detail file is missing. Reprocess the scene to rebuild it.' : 'Evidence appears once processing finishes.'} icon={ShieldCheck} />;
  const overview = scene.overview;
  return <section><div className="border-b border-white/[0.08] pb-6"><p className="text-[11px] font-bold uppercase tracking-[0.16em] text-sky-300">Evidence</p><h2 className="mt-2 text-2xl font-semibold tracking-tight text-white">{scene.scene.name}</h2><p className="mt-2 max-w-2xl text-sm leading-6 text-zinc-500">Everything here comes from this scene's own evidence record.</p></div><div className="mt-6 grid gap-4 xl:grid-cols-2">{evidence.record.sections.map((section) => <EvidenceCard key={section.kind} section={section} overview={overview} onOpenPreview={onOpenPreview} />)}</div>{evidence.record.limitations.length > 0 && <section className="mt-5 rounded-xl border border-amber-400/20 bg-amber-400/[0.06] p-4"><div className="flex items-center gap-2 text-sm font-semibold text-amber-100"><AlertTriangle size={16} /> Evidence limitations</div><ul className="mt-3 list-disc space-y-1 pl-5 text-xs leading-5 text-amber-100/70">{evidence.record.limitations.map((item) => <li key={item}>{item}</li>)}</ul></section>}</section>;
}

function EvidenceCard({ section, overview, onOpenPreview }) {
  const colors = { metadata: 'text-sky-200 border-sky-400/20 bg-sky-400/[0.05]', land_water_estimate: 'text-cyan-200 border-cyan-400/20 bg-cyan-400/[0.05]', model_observation: 'text-violet-200 border-violet-400/20 bg-violet-400/[0.05]', validated_detector_evidence: 'text-emerald-200 border-emerald-400/20 bg-emerald-400/[0.05]' };
  const objects = section.values?.objects;
  return <article className={`rounded-xl border p-5 ${colors[section.kind] || 'border-white/[0.08] bg-[#111114]'}`}><div className="flex items-start justify-between gap-4"><div><p className="text-[11px] font-bold uppercase tracking-[0.12em] opacity-70">{readable(section.kind)}</p><h3 className="mt-1 text-sm font-semibold text-white">{section.title}</h3></div>{section.source?.artifact_id && overview?.id === section.source.artifact_id && <button type="button" onClick={() => onOpenPreview(overview)} className="rounded-md border border-white/[0.12] bg-black/10 p-2 text-current hover:bg-black/20" aria-label="Open source preview"><Eye size={14} /></button>}</div>{section.kind === 'validated_detector_evidence' && Array.isArray(objects) ? <div className="mt-4 space-y-2">{objects.length === 0 ? <p className="text-xs leading-5 text-zinc-400">No validated detections were present in the approved detector sidecar.</p> : objects.map((object) => <div key={object.id || `${object.label}-${object.confidence}`} className="rounded-lg border border-white/[0.08] bg-black/10 p-3"><p className="text-xs font-semibold text-zinc-100">{object.label} <span className="font-normal text-zinc-500">· {(object.confidence * 100).toFixed(1)}%</span></p><p className="mt-1 text-[11px] text-zinc-500">Bounds: {Object.values(object.bounding_box_px || {}).join(', ')}</p></div>)}</div> : <dl className="mt-4 space-y-2">{Object.entries(section.values || {}).map(([key, value]) => <div key={key} className="grid grid-cols-[minmax(6rem,0.7fr)_minmax(0,1.3fr)] gap-3 text-xs"><dt className="capitalize text-zinc-500">{readable(key)}</dt><dd className="break-words text-zinc-200">{typeof value === 'object' ? JSON.stringify(value) : String(value)}</dd></div>)}</dl>}<p className="mt-4 text-[11px] leading-5 text-zinc-500">{section.limitations?.[0] || section.provenance?.source || 'Provenance retained with this scene.'}</p></article>;
}

function PreviewDialog({ artifact, grant, onClose }) { return <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/80 p-4" role="dialog" aria-modal="true" aria-labelledby="preview-title"><div className="flex max-h-[92vh] w-full max-w-5xl flex-col overflow-hidden rounded-2xl border border-white/[0.12] bg-[#111114] shadow-2xl"><div className="flex items-center justify-between border-b border-white/[0.08] px-4 py-3"><div className="min-w-0"><h2 id="preview-title" className="truncate text-sm font-semibold capitalize text-white">{readable(artifact.kind)} preview</h2><p className="mt-0.5 text-xs text-zinc-500">Private link expires {formatDate(grant.expires_at, true)}</p></div><button type="button" onClick={onClose} className="rounded-lg p-2 text-zinc-500 hover:bg-white/[0.06] hover:text-white" aria-label="Close preview"><X size={17} /></button></div><div className="min-h-0 overflow-auto bg-black p-3"><img src={grant.url} alt={`${readable(artifact.kind)} preview`} referrerPolicy="no-referrer" className="mx-auto max-h-[75vh] max-w-full object-contain" /></div></div></div>; }

function WorkspaceState({ title, detail, action }) { return <main className="grid min-h-screen place-items-center bg-[#09090b] p-6 text-center"><section><h1 className="text-lg font-semibold text-white">{title}</h1>{detail && <p className="mt-2 text-sm text-zinc-500">{detail}</p>}{action && <button type="button" onClick={action} className="mt-5 text-sm font-semibold text-sky-300 hover:text-sky-200">Return to dashboard</button>}</section></main>; }
