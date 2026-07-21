import { useEffect, useMemo, useState } from 'react';
import { AlertTriangle, MapPin, Search, ShieldCheck, Sparkles } from 'lucide-react';

function readable(value, fallback = 'Not available') {
  if (!value) return fallback;
  return String(value).replace(/_/g, ' ');
}

function toIsoDate(value, endOfDay = false) {
  if (!value) return null;
  const date = new Date(`${value}T${endOfDay ? '23:59:59.999' : '00:00:00.000'}Z`);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}

function boundsText(bounds) {
  if (!bounds) return 'Spatial bounds unavailable';
  return `Rows ${bounds.row_start}–${bounds.row_end} · Cols ${bounds.col_start}–${bounds.col_end}`;
}

export default function EvidenceSearchPanel({ api, projectId, scenes, selectedSceneId, onSelectScene, onOpenPatch }) {
  const [query, setQuery] = useState('');
  const [scopeSceneId, setScopeSceneId] = useState('');
  const [sensor, setSensor] = useState('');
  const [polarization, setPolarization] = useState('');
  const [fromDate, setFromDate] = useState('');
  const [toDate, setToDate] = useState('');
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [isSearching, setIsSearching] = useState(false);

  const readyScenes = useMemo(
    () => scenes.map((item) => item.scene).filter((scene) => scene?.status === 'ready'),
    [scenes],
  );
  const sensors = useMemo(
    () => [...new Set(readyScenes.map((scene) => scene.sensor).filter(Boolean))].sort(),
    [readyScenes],
  );
  const polarizations = useMemo(
    () => [...new Set(readyScenes.flatMap((scene) => scene.polarizations || []).filter(Boolean))].sort(),
    [readyScenes],
  );

  useEffect(() => {
    // Preserve an explicit project-wide choice. A selected scene only seeds
    // the picker before the analyst has made a choice.
    if (!scopeSceneId && selectedSceneId && readyScenes.some((scene) => scene.id === selectedSceneId)) {
      setScopeSceneId(selectedSceneId);
    }
  }, [selectedSceneId, readyScenes, scopeSceneId]);

  const submit = async (event) => {
    event.preventDefault();
    const normalized = query.trim();
    if (!normalized || isSearching) return;
    setError(null);
    setIsSearching(true);
    try {
      const data = await api.evidence.search({
        project_id: projectId,
        scene_id: scopeSceneId || null,
        query: normalized,
        limit: 8,
        filters: {
          sensor: sensor || null,
          polarization: polarization || null,
          acquisition_from: toIsoDate(fromDate),
          acquisition_to: toIsoDate(toDate, true),
          ready_only: true,
        },
      });
      setResult(data);
    } catch (nextError) {
      setError(nextError);
    } finally {
      setIsSearching(false);
    }
  };

  const stateTone = {
    results: 'border-sky-400/20 bg-sky-400/[0.06] text-sky-100',
    weak: 'border-amber-400/25 bg-amber-400/[0.08] text-amber-100',
    empty: 'border-zinc-700 bg-zinc-900/70 text-zinc-300',
  };

  return (
    <section>
      <div className="border-b border-white/[0.08] pb-6">
        <p className="text-[11px] font-bold uppercase tracking-[0.16em] text-sky-300">Evidence search</p>
        <h2 className="mt-2 text-2xl font-semibold tracking-tight text-white">Search authorized SAR patches</h2>
        <p className="mt-2 max-w-3xl text-sm leading-6 text-zinc-500">Retrieval stays inside the selected private project and optional scene scope. Results are evidence cards, not detections or model conclusions.</p>
      </div>

      <form onSubmit={submit} className="mt-6 rounded-xl border border-white/[0.08] bg-[#111114] p-4 sm:p-5">
        <label htmlFor="evidence-query" className="text-xs font-semibold text-zinc-200">What evidence are you looking for?</label>
        <div className="mt-2 flex flex-col gap-2 sm:flex-row">
          <div className="relative min-w-0 flex-1"><Search className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-zinc-600" size={16} /><input id="evidence-query" value={query} onChange={(event) => setQuery(event.target.value)} maxLength={1000} placeholder="e.g. compact bright structures near shoreline" className="w-full rounded-lg border border-white/[0.1] bg-black/20 py-2.5 pl-9 pr-3 text-sm text-white outline-none placeholder:text-zinc-600 focus:border-sky-400/55 focus:ring-2 focus:ring-sky-400/10" /></div>
          <button type="submit" disabled={!query.trim() || isSearching || readyScenes.length === 0} className="inline-flex items-center justify-center gap-2 rounded-lg bg-sky-500 px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-sky-400 disabled:cursor-not-allowed disabled:opacity-45"><Search size={15} /> {isSearching ? 'Searching…' : 'Search evidence'}</button>
        </div>
        <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
          <Filter label="Scope"><select value={scopeSceneId} onChange={(event) => setScopeSceneId(event.target.value)} className="select-control"><option value="">Entire project</option>{readyScenes.map((scene) => <option key={scene.id} value={scene.id}>{scene.name}</option>)}</select></Filter>
          <Filter label="Sensor"><select value={sensor} onChange={(event) => setSensor(event.target.value)} className="select-control"><option value="">Any sensor</option>{sensors.map((value) => <option key={value} value={value}>{value}</option>)}</select></Filter>
          <Filter label="Polarization"><select value={polarization} onChange={(event) => setPolarization(event.target.value)} className="select-control"><option value="">Any polarization</option>{polarizations.map((value) => <option key={value} value={value}>{value}</option>)}</select></Filter>
          <Filter label="Acquired after"><input type="date" value={fromDate} onChange={(event) => setFromDate(event.target.value)} className="select-control" /></Filter>
          <Filter label="Acquired before"><input type="date" value={toDate} onChange={(event) => setToDate(event.target.value)} className="select-control" /></Filter>
        </div>
        {readyScenes.length === 0 && <p className="mt-4 text-xs text-amber-200">A scene must finish processing before private patch evidence can be searched.</p>}
      </form>

      {error && <section className="mt-5 rounded-xl border border-red-500/25 bg-red-500/[0.08] p-4 text-sm text-red-100"><div className="flex gap-2"><AlertTriangle size={17} className="mt-0.5 shrink-0" /><div><p className="font-semibold">Evidence search is unavailable</p><p className="mt-1 text-red-100/75">{error.message}</p></div></div></section>}
      {result && <section className="mt-5">
        <div className={`rounded-xl border p-4 ${stateTone[result.retrieval_state] || stateTone.empty}`}><div className="flex gap-3"><span className="mt-0.5"><Sparkles size={17} /></span><div><p className="text-sm font-semibold capitalize">{readable(result.retrieval_state)} retrieval</p><p className="mt-1 text-xs leading-5 opacity-80">{result.message}</p></div></div></div>
        {result.retrieval_state === 'weak' && <p className="mt-3 text-xs leading-5 text-amber-200/80">Weak semantic matches are shown for inspection only. They are insufficient evidence for a confident answer.</p>}
        {result.cards?.length > 0 && <div className="mt-4 grid gap-3 lg:grid-cols-2">{result.cards.map((card) => <button key={card.patch_id} type="button" onClick={() => { onSelectScene(card.scene_id); onOpenPatch(card.patch_id, card.scene_id); }} className="rounded-xl border border-white/[0.08] bg-[#111114] p-4 text-left transition hover:border-sky-400/35 hover:bg-sky-400/[0.04]"><div className="flex items-start justify-between gap-3"><div className="min-w-0"><p className="truncate text-sm font-semibold text-zinc-100">{card.scene_name}</p><p className="mt-1 text-xs text-zinc-500">Patch {String(card.patch_id).slice(0, 8)} · score {Number(card.retrieval_score).toFixed(3)}</p></div><MapPin size={16} className="shrink-0 text-sky-300" /></div><p className="mt-4 text-xs text-zinc-400">{boundsText(card.bounds)}</p><p className="mt-2 text-[11px] leading-5 text-zinc-600">{card.citation?.why_provided || 'Authorized evidence patch.'}</p><div className="mt-3 inline-flex items-center gap-1.5 text-xs font-semibold text-sky-300"><ShieldCheck size={13} /> Open authorized patch</div></button>)}</div>}
      </section>}
    </section>
  );
}

function Filter({ label, children }) {
  return <label className="block text-[11px] font-semibold uppercase tracking-[0.08em] text-zinc-500"><span>{label}</span><span className="mt-1.5 block">{children}</span></label>;
}
