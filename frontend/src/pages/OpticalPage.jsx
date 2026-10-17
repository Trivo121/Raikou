import { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  CircleAlert,
  Clock3,
  Download,
  Info,
  Layers,
  LoaderCircle,
  MousePointerClick,
  Play,
  Plus,
  RefreshCw,
  Satellite,
  ScanSearch,
  Search,
  Trees,
  XCircle,
} from 'lucide-react';
import { useApi, useAuth } from '../auth/AuthProvider';
import AoiMapPicker from '../components/AoiMapPicker';
import GroundedChatPanel from '../components/GroundedChatPanel';
import { createClientRequestId } from '../utils/helpers';

/* ─── Constants ────────────────────────────────────────────────────────────── */

const TERMINAL = new Set(['ready', 'failed', 'cancelled']);

// New runs map one clear date. Seasonal-stack runs made earlier still open and read as before.
const RUN_MODE = 'single_date';

// Map layers a finished run offers, in switcher order. Keys are the public
// artifact names from GET /optical/runs/{id}.
const LAYERS = [
  { key: 'true_color', label: 'True colour' },
  { key: 'lulc', label: 'Land cover' },
  { key: 'worldcover2021', label: 'WorldCover 2021' },
  { key: 'score', label: 'Score' },
  { key: 'model_view', label: 'Model view' },
];
const LIVE_SIMILARITY = 'similarity_live';
const VIRIDIS = 'linear-gradient(to right, #440154, #3b528b, #21918c, #5ec962, #fde725)';
const MAGMA = 'linear-gradient(to right, #000004, #51127c, #b73779, #fc8961, #fcfdbf)';

const DISABLED_REASONS = {
  disabled: 'Optical land-cover runs are switched off on this server.',
  not_configured: 'Copernicus access is not configured on this server.',
  schema_not_applied: 'Optical runs are unavailable until their database migration is applied.',
  model_unavailable: 'The land-cover model is not installed on this server yet.',
};

const CHAT_COPY = {
  eyebrow: 'Ask this map',
  title: 'Questions about this land-cover run',
  description: 'Answers are composed from this run’s record: class areas, agreement with WorldCover 2021, imagery and limitations. Questions about objects, ownership or security are declined.',
  placeholder: 'Ask how much forest there is, how it compares with WorldCover 2021, or how far to trust it… (Enter to send)',
  emptyTitle: 'Ask about this map',
  emptyText: 'Every number in an answer comes from this run’s record, and it says so when the record cannot answer.',
};

/* ─── Helpers ──────────────────────────────────────────────────────────────── */

function isoDay(date) {
  return date.toISOString().slice(0, 10);
}

function daysAgo(days) {
  const date = new Date();
  date.setUTCDate(date.getUTCDate() - days);
  return isoDay(date);
}

function formatDay(value) {
  if (!value) return 'Unknown date';
  const date = new Date(value);
  return Number.isNaN(date.valueOf())
    ? String(value).slice(0, 10)
    : date.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
}

function num(value, digits = 1) {
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits }) : '—';
}

function spanKm(bbox) {
  if (!bbox) return null;
  const rad = Math.PI / 180;
  const midLat = ((bbox.north + bbox.south) / 2) * rad;
  return {
    width: Math.abs((bbox.east - bbox.west) * rad * 6371 * Math.cos(midLat)),
    height: Math.abs((bbox.north - bbox.south) * rad * 6371),
  };
}

function boxOfCorners(corners) {
  if (!Array.isArray(corners) || corners.length !== 4) return null;
  const lons = corners.map((c) => c[0]);
  const lats = corners.map((c) => c[1]);
  return { west: Math.min(...lons), south: Math.min(...lats), east: Math.max(...lons), north: Math.max(...lats) };
}

function suggestSingle(items) {
  const best = items
    .filter((item) => item.online && item.covers_aoi !== false)
    .sort((a, b) => (a.cloud_cover ?? 100) - (b.cloud_cover ?? 100))[0];
  return best ? [best.product_id] : [];
}

/* ─── Small pieces ─────────────────────────────────────────────────────────── */

function Notice({ tone = 'info', icon: Icon = Info, children }) {
  const tones = {
    info: 'border-white/[0.08] bg-white/[0.02] text-zinc-400',
    warn: 'border-amber-400/25 bg-amber-400/[0.07] text-amber-100',
    error: 'border-red-500/25 bg-red-500/10 text-red-200',
    good: 'border-emerald-400/25 bg-emerald-400/[0.07] text-emerald-100',
  };
  return (
    <div className={`flex items-start gap-2 rounded-lg border px-3 py-2.5 text-xs leading-5 ${tones[tone]}`}>
      <Icon size={14} className="mt-0.5 shrink-0" />
      <div className="min-w-0">{children}</div>
    </div>
  );
}

function Card({ title, icon: Icon, action, children, className = '' }) {
  return (
    <section className={`rounded-xl border border-white/[0.08] bg-[#111114] p-5 ${className}`}>
      {(title || action) && (
        <div className="mb-4 flex items-center justify-between gap-3">
          <h2 className="flex items-center gap-2 text-sm font-semibold text-white">
            {Icon && <Icon size={15} className="text-sky-300" />} {title}
          </h2>
          {action}
        </div>
      )}
      {children}
    </section>
  );
}

function StatusChip({ status }) {
  const value = String(status || 'queued');
  const tone = value === 'ready'
    ? 'border-emerald-400/30 bg-emerald-400/10 text-emerald-200'
    : value === 'failed'
      ? 'border-red-400/30 bg-red-400/10 text-red-200'
      : value === 'cancelled'
        ? 'border-zinc-600 bg-zinc-800 text-zinc-300'
        : 'border-sky-400/30 bg-sky-400/10 text-sky-200';
  const Icon = value === 'ready' ? CheckCircle2 : value === 'failed' ? CircleAlert : value === 'cancelled' ? XCircle : value === 'queued' ? Clock3 : LoaderCircle;
  return (
    <span className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-semibold capitalize ${tone}`}>
      <Icon size={10} className={value === 'processing' || value === 'validating' ? 'animate-spin' : ''} /> {value}
    </span>
  );
}

function Legend({ layer, classes }) {
  if (layer === 'lulc' || layer === 'worldcover2021') {
    return (
      <div className="flex flex-wrap gap-x-4 gap-y-1.5">
        {(classes || []).map((c) => (
          <span key={c.name} className="inline-flex items-center gap-1.5 text-[11px] text-zinc-400">
            <span className="h-3 w-3 rounded-sm border border-black/40" style={{ background: c.color || '#71717a' }} /> {c.name}
          </span>
        ))}
        {layer === 'worldcover2021' && <span className="text-[11px] text-zinc-500">ESA WorldCover 2021 v200, remapped to the same five classes (CC BY 4.0).</span>}
      </div>
    );
  }
  if (layer === 'score' || layer === LIVE_SIMILARITY) {
    const isScore = layer === 'score';
    return (
      <div>
        <div className="h-2.5 w-full max-w-sm rounded-full" style={{ background: isScore ? VIRIDIS : MAGMA }} />
        <div className="mt-1 flex max-w-sm justify-between text-[10px] text-zinc-500">
          <span>{isScore ? '0%' : '−0.2'}</span>
          <span>{isScore ? 'calibrated score' : 'looks similar to the model'}</span>
          <span>{isScore ? '100%' : '1.0'}</span>
        </div>
        <p className="mt-1 text-[11px] text-zinc-500">
          {isScore
            ? 'Probability that a pixel agrees with the WorldCover-2021-derived class on held-out Indian tiles. Not field-verified accuracy.'
            : 'How alike each 40 m cell looks to the model compared with the clicked spot. Not a class, a probability or a measurement.'}
        </p>
      </div>
    );
  }
  if (layer === 'model_view') {
    return <p className="text-[11px] text-zinc-500">The model’s top three feature directions as colour. Areas it sees alike share a colour; the colours themselves mean nothing fixed.</p>;
  }
  if (layer === 'true_color') {
    return <p className="text-[11px] text-zinc-500">Sentinel-2 true colour (B04, B03, B02) of the clearest date the model used.</p>;
  }
  return <p className="text-[11px] text-zinc-500">Pick a layer above the map.</p>;
}

/* ─── New run ──────────────────────────────────────────────────────────────── */

function NewRunPanel({ api, providers, projectId, onStarted }) {
  const [bbox, setBbox] = useState(null);
  const [startDate, setStartDate] = useState(daysAgo(90));
  const [endDate, setEndDate] = useState(isoDay(new Date()));
  const [maxCloud, setMaxCloud] = useState(Number(providers?.default_max_cloud ?? 20));
  const [selected, setSelected] = useState([]);
  const [name, setName] = useState('');
  const requestRef = useRef({ key: null, value: null });

  const search = useMutation({ mutationFn: (input) => api.optical.search(input) });
  const start = useMutation({
    mutationFn: (input) => api.optical.startRun(input),
    onSuccess: (data) => onStarted?.(data?.run?.scene_id),
  });

  const items = search.data?.items || [];
  const cap = providers?.max_side_km_single;
  const span = spanKm(bbox);
  const tooBig = Boolean(span && cap && (span.width > cap || span.height > cap));
  const grid = search.data?.grid_single_date;
  const cost = grid ? grid.processing_units_per_date * Math.max(1, selected.length) : null;
  const budget = providers?.budget;
  const left = budget ? Math.max(0, budget.user_month_cap - budget.user_used) : null;
  const overBudget = cost !== null && left !== null && cost > left;

  // A new search or box resets the pick to the suggestion.
  useEffect(() => {
    if (!search.isSuccess) return;
    setSelected(suggestSingle(items));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search.data]);

  const toggle = (productId) => {
    const item = items.find((i) => i.product_id === productId);
    if (!item?.online) return;
    setSelected((current) => (current.includes(productId) ? current.filter((id) => id !== productId) : [productId]));
  };

  const submitSearch = (event) => {
    event.preventDefault();
    if (!bbox || tooBig || search.isPending) return;
    search.mutate({
      west: bbox.west, south: bbox.south, east: bbox.east, north: bbox.north,
      start_date: startDate, end_date: endDate, max_cloud: maxCloud,
    });
  };

  const startRun = () => {
    if (!bbox || !selected.length || start.isPending || !grid || overBudget) return;
    // One id per (box, project, dates) choice, stable across retries of that choice.
    const key = JSON.stringify([bbox, RUN_MODE, projectId, [...selected].sort()]);
    if (requestRef.current.key !== key) requestRef.current = { key, value: createClientRequestId() };
    start.mutate({
      aoi: { west: bbox.west, south: bbox.south, east: bbox.east, north: bbox.north },
      mode: RUN_MODE,
      // Without a project the backend uses the caller's default optical project.
      ...(projectId ? { project_id: projectId } : {}),
      product_ids: selected,
      client_request_id: requestRef.current.value,
      ...(name.trim() ? { name: name.trim().slice(0, 120) } : {}),
    });
  };

  return (
    <Card title="New land-cover run" icon={Satellite}>
      <form onSubmit={submitSearch} className="space-y-4">
        <AoiMapPicker
          value={bbox}
          onChange={(next) => { setBbox(next); search.reset(); setSelected([]); }}
          footprints={items}
          selectedId={selected[selected.length - 1] || null}
          onSelect={toggle}
          heightClass="h-[24rem]"
          helpText={(s) => (
            <span>
              Roughly <span className="font-mono text-zinc-300">{s.width.toFixed(1)} x {s.height.toFixed(1)} km</span>. Mapped at
              10 m on its own UTM grid: up to {providers?.max_side_km_single ?? 24} km a side.
            </span>
          )}
        />

        <div className="grid gap-3 sm:grid-cols-[1fr_1fr_1fr_auto] sm:items-end">
          <label className="text-[11px] font-semibold text-zinc-400">From
            <input type="date" value={startDate} max={endDate} onChange={(e) => setStartDate(e.target.value)} className="mt-1.5 w-full rounded-lg border border-white/[0.1] bg-[#09090b] px-3 py-2 text-xs text-white outline-none focus:border-sky-400/60" />
          </label>
          <label className="text-[11px] font-semibold text-zinc-400">To
            <input type="date" value={endDate} min={startDate} onChange={(e) => setEndDate(e.target.value)} className="mt-1.5 w-full rounded-lg border border-white/[0.1] bg-[#09090b] px-3 py-2 text-xs text-white outline-none focus:border-sky-400/60" />
          </label>
          <label className="text-[11px] font-semibold text-zinc-400">Max cloud {maxCloud}%
            <input type="range" min="0" max="60" step="5" value={maxCloud} onChange={(e) => setMaxCloud(Number(e.target.value))} className="mt-3 w-full accent-sky-400" />
          </label>
          <button
            type="submit"
            disabled={!bbox || tooBig || search.isPending}
            className="inline-flex items-center justify-center gap-1.5 rounded-lg bg-sky-500 px-4 py-2 text-xs font-semibold text-white transition hover:bg-sky-400 disabled:cursor-not-allowed disabled:opacity-45"
          >
            {search.isPending ? <><LoaderCircle size={13} className="animate-spin" /> Searching</> : <><Search size={13} /> Find dates</>}
          </button>
        </div>

        {!bbox && <Notice>Draw the area you want mapped. Indian areas only: the model was trained and checked there.</Notice>}
        {tooBig && (
          <Notice tone="warn" icon={CircleAlert}>
            This area is about {span.width.toFixed(1)} x {span.height.toFixed(1)} km. A single-date run takes at most {cap} km a side.
          </Notice>
        )}
        {search.error && <Notice tone="error" icon={CircleAlert}>{search.error.message}</Notice>}
      </form>

      {search.isSuccess && (
        <div className="mt-5 space-y-3">
          {(search.data?.notes || []).map((note) => <Notice key={note} tone="warn" icon={CircleAlert}>{note}</Notice>)}
          <div className="flex items-center justify-between">
            <h3 className="text-xs font-semibold text-zinc-300">
              {items.length ? `${items.length} Sentinel-2 date${items.length === 1 ? '' : 's'} found` : 'No clear dates found'}
            </h3>
            <span className="text-[11px] text-zinc-500">Pick one.</span>
          </div>
          {items.length === 0 && <p className="text-xs text-zinc-500">Try a wider date range or a higher cloud limit.</p>}
          <ul className="max-h-72 space-y-1.5 overflow-y-auto pr-1">
            {items.map((item) => {
              const picked = selected.includes(item.product_id);
              return (
                <li key={item.product_id}>
                  <button
                    type="button"
                    onClick={() => toggle(item.product_id)}
                    disabled={!item.online}
                    className={`flex w-full items-center justify-between gap-3 rounded-lg border px-3 py-2 text-left transition disabled:cursor-not-allowed disabled:opacity-50 ${picked ? 'border-sky-400/50 bg-sky-400/[0.08]' : 'border-white/[0.08] bg-black/10 hover:border-sky-400/30'}`}
                  >
                    <span className="min-w-0">
                      <span className="block text-xs font-semibold text-zinc-100">{formatDay(item.sensing_start)}</span>
                      <span className="mt-0.5 block text-[11px] text-zinc-500">
                        {item.platform || 'Sentinel-2'} · tile {item.tile_id || '?'} · {num(item.cloud_cover, 1)}% cloud on the tile
                        {item.covers_aoi === false ? ' · covers only part of your area' : ''}
                        {!item.online ? ' · in the long-term archive' : ''}
                      </span>
                    </span>
                    {picked && <CheckCircle2 size={15} className="shrink-0 text-sky-300" />}
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {search.isSuccess && selected.length > 0 && (
        <div className="mt-5 rounded-lg border border-white/[0.08] bg-black/20 p-4">
          <p className="text-[11px] font-semibold uppercase tracking-[0.1em] text-zinc-500">This run</p>
          {grid ? (
            <p className="mt-1.5 text-xs text-zinc-300">
              {selected.length} date{selected.length === 1 ? '' : 's'} · {grid.width} x {grid.height} px ({num(grid.width_km, 2)} x {num(grid.height_km, 2)} km) at 10 m ·
              about <span className="font-semibold text-white">{num(cost, 1)} processing units</span>
              {left !== null && <> of your {num(left, 0)} left this month</>}
            </p>
          ) : (
            <p className="mt-1.5 text-xs text-amber-200">This area cannot be mapped; see the note above.</p>
          )}
          <label className="mt-3 block text-[11px] font-semibold text-zinc-400">Name (optional)
            <input value={name} onChange={(e) => setName(e.target.value)} maxLength={120} placeholder="e.g. Visakhapatnam coast, 2026" className="mt-1.5 w-full rounded-lg border border-white/[0.1] bg-[#09090b] px-3 py-2 text-xs text-white outline-none placeholder:text-zinc-600 focus:border-sky-400/60" />
          </label>
          {overBudget && <div className="mt-3"><Notice tone="warn" icon={CircleAlert}>That is more than your remaining allowance this month. Draw a smaller area or use fewer dates.</Notice></div>}
          {start.error && <div className="mt-3"><Notice tone="error" icon={CircleAlert}>{start.error.message}</Notice></div>}
          <button
            type="button"
            onClick={startRun}
            disabled={!grid || overBudget || start.isPending || start.isSuccess}
            className="mt-3 inline-flex items-center gap-1.5 rounded-lg bg-sky-500 px-3.5 py-2 text-xs font-semibold text-white transition hover:bg-sky-400 disabled:cursor-not-allowed disabled:opacity-45"
          >
            {start.isPending ? <><LoaderCircle size={13} className="animate-spin" /> Starting…</> : <><Play size={13} /> Map this area</>}
          </button>
          <p className="mt-2 text-[11px] text-zinc-500">Runs on the server: you can close this tab. A 10 km area takes about four minutes.</p>
        </div>
      )}
    </Card>
  );
}

/* ─── A run ────────────────────────────────────────────────────────────────── */

function ClassTable({ record }) {
  const classes = [...(record?.lulc?.classes || [])].sort((a, b) => (b.share_pct || 0) - (a.share_pct || 0));
  if (!classes.length) return null;
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[34rem] text-left text-xs">
        <thead className="text-[10px] uppercase tracking-wider text-zinc-500">
          <tr>
            <th className="py-2 pr-3 font-semibold">Class</th>
            <th className="py-2 pr-3 text-right font-semibold">Share</th>
            <th className="py-2 pr-3 text-right font-semibold">km²</th>
            <th className="py-2 pr-3 text-right font-semibold">ha</th>
            <th className="py-2 pr-3 text-right font-semibold">Mean score</th>
            <th className="py-2 pr-3 text-right font-semibold">WorldCover 2021 km²</th>
            <th className="py-2 text-right font-semibold">Same class in 2021</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-white/[0.05] text-zinc-300">
          {classes.map((c) => (
            <tr key={c.class_name}>
              <td className="py-2 pr-3 font-medium text-zinc-100">{c.class_name}</td>
              <td className="py-2 pr-3 text-right font-mono">{num(c.share_pct, 1)}%</td>
              <td className="py-2 pr-3 text-right font-mono">{num(c.area_sqkm, 2)}</td>
              <td className="py-2 pr-3 text-right font-mono">{num(c.area_hectares, 0)}</td>
              <td className="py-2 pr-3 text-right font-mono">{c.mean_score_pct == null ? '—' : `${num(c.mean_score_pct, 0)}%`}</td>
              <td className="py-2 pr-3 text-right font-mono">{num(c.worldcover_2021_area_sqkm, 2)}</td>
              <td className="py-2 text-right font-mono">{c.same_class_in_worldcover_2021_pct == null ? '—' : `${num(c.same_class_in_worldcover_2021_pct, 0)}%`}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TrustPanel({ record }) {
  const evaluation = record?.lulc?.evaluation || {};
  const agreement = Number(evaluation.agreement_overall);
  const limitations = record?.limitations || [];
  return (
    <div className="space-y-3">
      {Number.isFinite(agreement) && (
        <Notice tone="good" icon={CheckCircle2}>
          On held-out Indian tiles the model agrees with WorldCover-2021-derived labels on{' '}
          <span className="font-semibold">{num(100 * agreement, 1)}%</span> of pixels
          ({evaluation.applies_to === 'seasonal_stack' ? 'three-season' : 'single-date'} benchmark, mean IoU {num(evaluation.mean_iou, 2)}).
          That is agreement with a reference map, not field-verified accuracy.
        </Notice>
      )}
      {limitations.length > 0 && (
        <details className="group rounded-lg border border-white/[0.08] bg-black/15 px-3 py-2.5">
          <summary className="cursor-pointer list-none text-xs font-semibold text-zinc-300">
            Read before using this map <span className="font-normal text-zinc-600 group-open:hidden">· {limitations.length} notes</span>
          </summary>
          <ul className="mt-2 list-disc space-y-1.5 pl-4 text-[11px] leading-5 text-zinc-400">
            {limitations.map((item) => <li key={item}>{item}</li>)}
          </ul>
        </details>
      )}
    </div>
  );
}

function SpectralTable({ record }) {
  const rows = (record?.spectral?.rows || []).filter((r) => (r.clear_pixels || 0) > 0);
  if (!rows.length) return null;
  return (
    <details className="group rounded-lg border border-white/[0.08] bg-black/15 px-3 py-2.5">
      <summary className="cursor-pointer list-none text-xs font-semibold text-zinc-300">
        Spectral indices per class <span className="font-normal text-zinc-600">· NDVI, MNDWI, NDBI medians over clear pixels</span>
      </summary>
      <div className="mt-2 overflow-x-auto">
        <table className="w-full min-w-[30rem] text-left text-[11px]">
          <thead className="text-[10px] uppercase tracking-wider text-zinc-500">
            <tr><th className="py-1.5 pr-3">Date</th><th className="py-1.5 pr-3">Class</th><th className="py-1.5 pr-3 text-right">NDVI</th><th className="py-1.5 pr-3 text-right">MNDWI</th><th className="py-1.5 pr-3 text-right">NDBI</th><th className="py-1.5 text-right">Clear km²</th></tr>
          </thead>
          <tbody className="divide-y divide-white/[0.05] text-zinc-300">
            {rows.map((r) => (
              <tr key={`${r.date}-${r.class_id}`} className={r.used_by_model ? '' : 'text-zinc-500'}>
                <td className="py-1.5 pr-3 font-mono">{r.date}{r.used_by_model ? '' : ' *'}</td>
                <td className="py-1.5 pr-3">{r.class_name}</td>
                <td className="py-1.5 pr-3 text-right font-mono">{num(r.ndvi_median, 2)}</td>
                <td className="py-1.5 pr-3 text-right font-mono">{num(r.mndwi_median, 2)}</td>
                <td className="py-1.5 pr-3 text-right font-mono">{num(r.ndbi_median, 2)}</td>
                <td className="py-1.5 text-right font-mono">{num(r.clear_area_sqkm, 2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="mt-2 text-[10px] text-zinc-600">* not used by the model (too cloudy). They describe the image on that date, not field measurements.</p>
      </div>
    </details>
  );
}

function SimilarityPanel({ active, onToggle, result, library, onSearchLibrary, libraryPending, error, pending, onOpenRun, available, onReprocess, reprocessPending }) {
  if (!available) {
    return (
      <Notice tone="warn" icon={CircleAlert}>
        This run was made before similarity search existed.{' '}
        <button type="button" onClick={onReprocess} disabled={reprocessPending} className="font-semibold text-amber-200 underline underline-offset-2 disabled:opacity-50">
          {reprocessPending ? 'Reprocessing…' : 'Reprocess it'}
        </button>{' '}
        to add it. The stored imagery is reused, so no processing units are spent.
      </Notice>
    );
  }
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={onToggle}
          aria-pressed={active}
          className={`inline-flex items-center gap-1.5 rounded-lg border px-3 py-2 text-xs font-semibold transition ${active ? 'border-pink-400/50 bg-pink-400/10 text-pink-100' : 'border-white/[0.1] text-zinc-300 hover:bg-white/[0.05]'}`}
        >
          <MousePointerClick size={13} /> {active ? 'Click the map… (on)' : 'Find similar areas'}
        </button>
        {result?.seed && (
          <button
            type="button"
            onClick={onSearchLibrary}
            disabled={libraryPending}
            className="inline-flex items-center gap-1.5 rounded-lg border border-white/[0.1] px-3 py-2 text-xs font-semibold text-zinc-300 transition hover:bg-white/[0.05] disabled:opacity-45"
          >
            {libraryPending ? <LoaderCircle size={13} className="animate-spin" /> : <ScanSearch size={13} />} Search my other runs
          </button>
        )}
        {pending && <span className="flex items-center gap-1.5 text-[11px] text-zinc-500"><LoaderCircle size={12} className="animate-spin" /> Comparing…</span>}
      </div>
      {error && <Notice tone="error" icon={CircleAlert}>{error.message}</Notice>}
      {result?.areas?.length > 0 && (
        <p className="text-[11px] leading-5 text-zinc-400">
          Looks similar to the model:{' '}
          {result.areas.map((a) => `${num(a.area_sqkm, 2)} km² (${num(a.share_pct, 0)}%) at ≥ ${a.threshold}`).join(' · ')}.
          {result.basis?.energy_captured != null && ` Heatmap from ${result.basis.dims} feature directions keeping ${num(100 * result.basis.energy_captured, 0)}% of the model’s signal.`}
        </p>
      )}
      {library && (
        <div className="rounded-lg border border-white/[0.08] bg-black/15 p-3">
          <p className="text-[11px] font-semibold text-zinc-300">Most similar 640 m windows in your other runs</p>
          {(library.notes || []).map((note) => <p key={note} className="mt-1 text-[11px] text-zinc-500">{note}</p>)}
          <ul className="mt-2 space-y-1.5">
            {(library.matches || []).map((m) => (
              <li key={`${m.scene_id}-${m.center_lonlat?.join(',')}`}>
                <button type="button" onClick={() => onOpenRun(m.scene_id)} className="flex w-full items-center justify-between gap-3 rounded-md border border-white/[0.06] px-2.5 py-1.5 text-left text-[11px] transition hover:border-sky-400/30">
                  <span className="min-w-0 truncate text-zinc-200">{m.scene_name || 'Optical run'} · {m.dominant_class || 'mixed'}</span>
                  <span className="shrink-0 font-mono text-zinc-400">{num(m.score, 2)}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function RunView({ api, sceneId, providers, userId, onOpenRun }) {
  const queryClient = useQueryClient();
  const [activeLayer, setActiveLayer] = useState('lulc');
  const [similarMode, setSimilarMode] = useState(false);
  const [seed, setSeed] = useState(null);
  const [heatmap, setHeatmap] = useState(null);
  const [library, setLibrary] = useState(null);
  // The page mounts one RunView per run (key={sceneId}), so all of this
  // starts fresh when another run is opened.
  const [conversationId, setConversationId] = useState(null);

  const runQuery = useQuery({
    queryKey: ['optical-run', userId, sceneId],
    queryFn: ({ signal }) => api.optical.getRun(sceneId, { signal }),
    enabled: Boolean(userId && sceneId),
    refetchInterval: (query) => (TERMINAL.has(query.state.data?.status) ? false : 4000),
  });
  const run = runQuery.data;
  const record = run?.record || null;
  const ready = run?.status === 'ready';

  const overlaysQuery = useQuery({
    queryKey: ['optical-overlays', sceneId, run?.job_id, Object.keys(run?.artifacts || {}).length],
    enabled: Boolean(ready && run?.corners_lonlat),
    // Grants live 90 s; maplibre starts loading every image as soon as it is
    // added, so one batch per visit is enough.
    staleTime: 60_000,
    gcTime: 60_000,
    refetchOnWindowFocus: false,
    queryFn: async ({ signal }) => {
      const wanted = LAYERS.filter((layer) => run.artifacts?.[layer.key]);
      const grants = await Promise.all(wanted.map((layer) => api.artifacts
        .preview(run.artifacts[layer.key], { signal })
        .then((grant) => ({ id: layer.key, label: layer.label, url: grant.url, corners: run.corners_lonlat }))
        .catch(() => null)));
      return grants.filter(Boolean);
    },
  });

  const overlays = useMemo(() => {
    const base = overlaysQuery.data || [];
    return heatmap ? [...base, { id: LIVE_SIMILARITY, label: 'Looks similar', url: heatmap.image.data_url, corners: heatmap.image.corners_lonlat }] : base;
  }, [overlaysQuery.data, heatmap]);

  const similar = useMutation({
    mutationFn: (input) => api.optical.similar(sceneId, input),
    onSuccess: (data) => {
      setHeatmap(data);
      setLibrary(null);
      setActiveLayer(LIVE_SIMILARITY);
    },
  });
  const librarySearch = useMutation({
    mutationFn: (input) => api.optical.similar(sceneId, input),
    onSuccess: setLibrary,
  });
  const download = useMutation({
    mutationFn: (artifactId) => api.artifacts.download(artifactId),
    onSuccess: (grant) => { if (grant?.url) window.location.assign(grant.url); },
  });
  const reprocess = useMutation({
    mutationFn: () => api.scenes.reprocess(sceneId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['optical-run', userId, sceneId] });
      queryClient.invalidateQueries({ queryKey: ['optical-runs', userId] });
    },
  });

  const openPreview = async (artifact) => {
    if (!artifact?.id) return;
    try {
      const grant = await api.artifacts.preview(artifact.id);
      if (grant?.url) window.open(grant.url, '_blank', 'noopener');
    } catch {
      // The figure above stays usable; a failed new-tab preview is not worth a modal.
    }
  };

  const classes = providers?.model?.classes || [];
  const focusBox = useMemo(() => boxOfCorners(run?.corners_lonlat), [run?.corners_lonlat]);

  if (runQuery.isLoading) {
    return <Card><p className="flex items-center gap-2 text-xs text-zinc-500"><LoaderCircle size={14} className="animate-spin" /> Loading this run…</p></Card>;
  }
  if (runQuery.isError) {
    return <Card><Notice tone="error" icon={AlertTriangle}>{runQuery.error.message}</Notice></Card>;
  }
  if (!run) return null;

  const progress = Math.max(0, Math.min(100, Number(run.job_progress) || 0));

  return (
    <div className="space-y-5">
      <Card
        title={run.name}
        icon={Trees}
        action={<StatusChip status={run.status} />}
      >
        <p className="text-xs text-zinc-500">
          {run.mode === 'seasonal_stack' ? 'Seasonal stack' : 'Single date'} · {(run.dates || []).map(formatDay).join(', ') || 'dates pending'}
          {run.grid ? ` · ${run.grid.width} x ${run.grid.height} px at 10 m` : ''}
          {run.processing_units != null ? ` · ${num(run.processing_units, 2)} PU` : run.estimated_processing_units != null ? ` · about ${num(run.estimated_processing_units, 1)} PU` : ''}
        </p>
        {!TERMINAL.has(run.status) && (
          <div className="mt-3">
            <div className="h-1.5 w-full overflow-hidden rounded-full bg-white/[0.06]">
              <div className="h-full rounded-full bg-sky-400 transition-all" style={{ width: `${progress}%` }} />
            </div>
            <p className="mt-1.5 text-[11px] text-zinc-500">{progress}% · {String(run.job_stage || 'queued').replace(/_/g, ' ')}. This page updates by itself; you can also close it.</p>
          </div>
        )}
        {run.status === 'failed' && (
          <div className="mt-3 space-y-2">
            <Notice tone="error" icon={CircleAlert}>{run.failure_detail || 'This run failed.'}{run.failure_code ? ` (${run.failure_code})` : ''}</Notice>
            <button type="button" onClick={() => reprocess.mutate()} disabled={reprocess.isPending} className="inline-flex items-center gap-1.5 rounded-lg border border-white/[0.1] px-3 py-2 text-xs font-semibold text-zinc-300 hover:bg-white/[0.05] disabled:opacity-45">
              <RefreshCw size={13} /> Try again with the same imagery
            </button>
            {reprocess.error && <Notice tone="error" icon={CircleAlert}>{reprocess.error.message}</Notice>}
          </div>
        )}
      </Card>

      {ready && (
        <>
          <Card
            title="Map"
            icon={Layers}
            action={(
              <div className="flex items-center gap-2">
                {run.artifacts?.bundle_zip && (
                  <button
                    type="button"
                    onClick={() => download.mutate(run.artifacts.bundle_zip)}
                    disabled={download.isPending}
                    className="inline-flex items-center gap-1.5 rounded-lg bg-sky-500 px-3 py-1.5 text-xs font-semibold text-white transition hover:bg-sky-400 disabled:opacity-45"
                  >
                    {download.isPending ? <LoaderCircle size={13} className="animate-spin" /> : <Download size={13} />} GIS bundle
                  </button>
                )}
              </div>
            )}
          >
            <AoiMapPicker
              value={null}
              drawable={false}
              overlays={overlays}
              activeOverlayId={activeLayer}
              onOverlayChange={setActiveLayer}
              clickMode={similarMode}
              onMapClick={(point) => { setSeed(point); similar.mutate({ lon: point.lng, lat: point.lat, scope: 'run' }); }}
              marker={seed}
              focusBox={focusBox}
              heightClass="h-[30rem]"
            />
            <div className="mt-3"><Legend layer={activeLayer} classes={classes} /></div>
            {overlaysQuery.isError && <div className="mt-3"><Notice tone="warn" icon={CircleAlert}>The map layers could not be loaded. The numbers below are unaffected.</Notice></div>}
            {download.error && <div className="mt-3"><Notice tone="error" icon={CircleAlert}>{download.error.message}</Notice></div>}
            <div className="mt-4">
              <SimilarityPanel
                available={Boolean(record?.similarity?.available)}
                active={similarMode}
                onToggle={() => setSimilarMode((value) => !value)}
                result={heatmap}
                pending={similar.isPending}
                error={similar.error || librarySearch.error}
                library={library}
                libraryPending={librarySearch.isPending}
                onSearchLibrary={() => seed && librarySearch.mutate({ lon: seed.lng, lat: seed.lat, scope: 'library', limit: 10 })}
                onOpenRun={onOpenRun}
                onReprocess={() => reprocess.mutate()}
                reprocessPending={reprocess.isPending}
              />
            </div>
          </Card>

          <Card title="Land cover" icon={Trees}>
            {record ? (
              <div className="space-y-4">
                <ClassTable record={record} />
                <TrustPanel record={record} />
                <SpectralTable record={record} />
              </div>
            ) : (
              <Notice icon={Info}>The scene record is not readable yet. Refresh in a moment.</Notice>
            )}
          </Card>

          <GroundedChatPanel
            api={api}
            userId={userId}
            projectId={run.project_id}
            scenes={[]}
            selectedSceneId={run.scene_id}
            conversationSceneId={run.scene_id}
            conversationId={conversationId}
            onConversationChange={setConversationId}
            onOpenPatch={() => undefined}
            onOpenPreview={openPreview}
            onOpenScene={onOpenRun}
            copy={CHAT_COPY}
          />
        </>
      )}
    </div>
  );
}

/* ─── Runs list ────────────────────────────────────────────────────────────── */

function RunsList({ runs, isLoading, error, selectedId, onSelect, onNew }) {
  return (
    <Card
      title="Your runs"
      icon={Layers}
      action={(
        <button type="button" onClick={onNew} className="inline-flex items-center gap-1 rounded-md border border-sky-400/25 bg-sky-400/10 px-2 py-1 text-[11px] font-semibold text-sky-200 hover:bg-sky-400/20">
          <Plus size={12} /> New
        </button>
      )}
    >
      {isLoading && <p className="flex items-center gap-2 text-xs text-zinc-500"><LoaderCircle size={13} className="animate-spin" /> Loading…</p>}
      {error && <Notice tone="error" icon={CircleAlert}>{error.message}</Notice>}
      {!isLoading && !error && runs.length === 0 && <p className="text-xs leading-5 text-zinc-500">No runs yet. Draw an area to make your first land-cover map.</p>}
      <ul className="space-y-1.5">
        {runs.map((run) => (
          <li key={run.scene_id}>
            <button
              type="button"
              onClick={() => onSelect(run.scene_id)}
              className={`w-full rounded-lg border px-3 py-2.5 text-left transition ${run.scene_id === selectedId ? 'border-sky-400/50 bg-sky-400/[0.08]' : 'border-white/[0.07] hover:border-white/[0.15]'}`}
            >
              <div className="flex items-start justify-between gap-2">
                <span className="line-clamp-2 text-xs font-medium text-zinc-100">{run.name}</span>
                <StatusChip status={run.status} />
              </div>
              <p className="mt-1 text-[11px] text-zinc-500">
                {run.mode === 'seasonal_stack' ? 'Seasonal' : 'Single date'} · {(run.dates || []).join(', ') || 'pending'}
              </p>
            </button>
          </li>
        ))}
      </ul>
    </Card>
  );
}

/* ─── Page ─────────────────────────────────────────────────────────────────── */

export default function OpticalPage() {
  const api = useApi();
  const { user } = useAuth();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { sceneId } = useParams();
  // /optical?project=<id> works inside one project; plain /optical shows every run.
  const [searchParams] = useSearchParams();
  const projectId = searchParams.get('project') || null;

  const providersQuery = useQuery({
    queryKey: ['optical-providers', user?.id],
    queryFn: ({ signal }) => api.optical.providers({ signal }),
    enabled: Boolean(user?.id),
    staleTime: 60_000,
    retry: false,
  });
  const runsQuery = useQuery({
    queryKey: ['optical-runs', user?.id, projectId],
    queryFn: ({ signal }) => api.optical.listRuns({ signal, projectId }),
    enabled: Boolean(user?.id),
    refetchInterval: (query) => ((query.state.data || []).some((run) => !TERMINAL.has(run.status)) ? 8000 : false),
  });
  const projectQuery = useQuery({
    queryKey: ['optical-project', user?.id, projectId],
    queryFn: () => api.projects.get(projectId),
    enabled: Boolean(user?.id && projectId),
    staleTime: 60_000,
    retry: false,
  });
  const providers = providersQuery.data;
  const runs = runsQuery.data || [];

  const openRun = (id) => navigate(`${id ? `/optical/${id}` : '/optical'}${projectId ? `?project=${projectId}` : ''}`);

  return (
    <div className="min-h-screen bg-[#09090b] font-['Inter'] text-[13px] text-zinc-400 selection:bg-[#0088ff]/30">
      <header className="sticky top-0 z-10 border-b border-white/[0.06] bg-[#09090b]/85 backdrop-blur-md">
        <div className="mx-auto flex max-w-7xl items-center justify-between gap-4 px-5 py-3.5">
          <div className="flex min-w-0 items-center gap-3">
            <Link to="/dashboard" className="grid h-8 w-8 place-items-center rounded-lg border border-white/[0.08] text-zinc-400 hover:text-white" aria-label="Back to projects">
              <ArrowLeft size={15} />
            </Link>
            <div className="min-w-0">
              <h1 className="text-[15px] font-semibold tracking-tight text-white">Optical land cover</h1>
              <p className="truncate text-[11px] text-zinc-500">
                {projectQuery.data?.name && <><span className="font-medium text-zinc-300">{projectQuery.data.name}</span> · </>}
                Sentinel-2 · Raikou OlmoEarth India model · five classes at 10 m · compared with ESA WorldCover 2021
              </p>
            </div>
          </div>
          {providers?.budget && (
            <span className="hidden shrink-0 rounded-md border border-white/[0.08] px-2.5 py-1 text-[11px] text-zinc-400 sm:inline">
              {num(providers.budget.user_used, 0)} / {num(providers.budget.user_month_cap, 0)} PU this month
            </span>
          )}
        </div>
      </header>

      <main className="mx-auto grid max-w-7xl gap-5 px-5 py-6 lg:grid-cols-[minmax(0,1fr)_19rem]">
        <div className="min-w-0 space-y-5">
          {providersQuery.isPending && <Card><p className="flex items-center gap-2 text-xs text-zinc-500"><LoaderCircle size={14} className="animate-spin" /> Checking availability…</p></Card>}
          {providersQuery.isError && <Card><Notice tone="error" icon={CircleAlert}>Optical runs are unavailable on this server: {providersQuery.error.message}</Notice></Card>}
          {providers && !providers.enabled && (
            <Card><Notice tone="warn" icon={CircleAlert}>{DISABLED_REASONS[providers.reason] || 'Optical runs are unavailable right now.'} Existing runs stay readable.</Notice></Card>
          )}
          {sceneId ? (
            <RunView key={sceneId} api={api} sceneId={sceneId} providers={providers} userId={user?.id} onOpenRun={openRun} />
          ) : (
            providers?.enabled && (
              <NewRunPanel
                api={api}
                providers={providers}
                projectId={projectId}
                onStarted={(id) => {
                  queryClient.invalidateQueries({ queryKey: ['optical-runs', user?.id] });
                  queryClient.invalidateQueries({ queryKey: ['optical-providers', user?.id] });
                  if (id) openRun(id);
                }}
              />
            )
          )}
          <p className="px-1 text-[11px] leading-5 text-zinc-600">
            Non-commercial research and teaching tool. The model is fine-tuned from OlmoEarth v1.2 (Ai2) under the OlmoEarth Artifact
            License, which bars military, surveillance, policing and extractive uses. Imagery: Copernicus Sentinel-2. Reference: ESA
            WorldCover 2021 (CC BY 4.0).
          </p>
        </div>
        <aside className="space-y-5 lg:sticky lg:top-20 lg:self-start">
          <RunsList
            runs={runs}
            isLoading={runsQuery.isLoading}
            error={runsQuery.error}
            selectedId={sceneId}
            onSelect={openRun}
            onNew={() => openRun(null)}
          />
        </aside>
      </main>
    </div>
  );
}
