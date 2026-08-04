import { useMemo, useRef, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import {
  Archive, CircleAlert, Download, Info, LoaderCircle, Satellite, Search,
} from 'lucide-react';
import AoiMapPicker from './AoiMapPicker';
import { createClientRequestId } from '../utils/helpers';

// Kilometres per degree of latitude, near enough for a UI hint. The server
// recomputes the real area with a haversine and is the one that decides.
const KM_PER_DEGREE = 111.32;

function isoDate(date) {
  return date.toISOString().slice(0, 10);
}

function defaultRange() {
  const end = new Date();
  const start = new Date(end.getTime() - 30 * 24 * 60 * 60_000);
  return { start: isoDate(start), end: isoDate(end) };
}

function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes <= 0) return 'Unknown size';
  const units = ['KB', 'MB', 'GB', 'TB'];
  let next = bytes / 1024;
  let index = 0;
  while (next >= 1024 && index < units.length - 1) {
    next /= 1024;
    index += 1;
  }
  return `${next.toFixed(next >= 10 || index === 0 ? 0 : 1)} ${units[index]}`;
}

function formatSensed(value) {
  if (!value) return 'Date unknown';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'Date unknown';
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(date);
}

function approximateAreaSqKm(bbox) {
  if (!bbox) return 0;
  const midLatitude = ((bbox.south + bbox.north) / 2) * (Math.PI / 180);
  const width = Math.abs(bbox.east - bbox.west) * KM_PER_DEGREE * Math.cos(midLatitude);
  const height = Math.abs(bbox.north - bbox.south) * KM_PER_DEGREE;
  return width * height;
}

function daysBetween(start, end) {
  const from = new Date(`${start}T00:00:00Z`).getTime();
  const to = new Date(`${end}T00:00:00Z`).getTime();
  if (!Number.isFinite(from) || !Number.isFinite(to)) return null;
  return Math.round((to - from) / 86_400_000);
}

function Notice({ tone = 'info', icon: Icon = Info, children }) {
  const tones = {
    info: 'border-white/[0.08] bg-white/[0.02] text-zinc-400',
    warn: 'border-amber-400/25 bg-amber-400/[0.07] text-amber-100',
    error: 'border-red-500/25 bg-red-500/10 text-red-200',
  };
  return (
    <p className={`flex items-start gap-2 rounded-lg border px-3 py-2.5 text-xs leading-5 ${tones[tone]}`}>
      <Icon size={14} className="mt-0.5 shrink-0" />
      <span>{children}</span>
    </p>
  );
}

/**
 * The Copernicus alternative to uploading a file by hand.
 *
 * This panel and SceneUploadPanel are never mounted at the same time and share
 * no storage: the upload panel's sessionStorage recovery keys are read only in
 * its own initialisers, and this path needs no sessionStorage at all because
 * durable idempotency lives in scene_acquisitions.client_request_id.
 */
export default function CopernicusSearchPanel({ api, scene, onStarted, onUseUpload }) {
  const range = useMemo(defaultRange, []);
  const [bbox, setBbox] = useState(null);
  const [startDate, setStartDate] = useState(range.start);
  const [endDate, setEndDate] = useState(range.end);
  const [selectedId, setSelectedId] = useState(null);

  // One id per product choice, stable across retries of that same choice, so a
  // lost response replays the original acquisition rather than starting a
  // second ~1 GB download.
  const requestIdRef = useRef({ productId: null, value: null });

  const providersQuery = useQuery({
    queryKey: ['acquisition-providers'],
    queryFn: ({ signal }) => api.acquisitions.providers({ signal }),
    staleTime: Infinity,
    // An older API without this route 404s; fail straight to the unavailable
    // state instead of making the user wait out three retries.
    retry: false,
  });
  const provider = providersQuery.data?.copernicus;

  const search = useMutation({
    mutationFn: (input) => api.acquisitions.search(input),
    onSuccess: () => setSelectedId(null),
  });
  const start = useMutation({
    mutationFn: (input) => api.acquisitions.start(input),
    onSuccess: () => onStarted?.(),
  });

  const results = search.data?.items || [];
  const selected = results.find((item) => item.product_id === selectedId) || null;

  const areaSqKm = approximateAreaSqKm(bbox);
  const spanDays = daysBetween(startDate, endDate);
  const maxArea = provider?.max_aoi_sq_km ?? 250_000;
  const maxDays = provider?.max_search_days ?? 90;

  const areaTooLarge = Boolean(bbox) && areaSqKm > maxArea;
  const spanTooLong = spanDays !== null && spanDays > maxDays;
  const rangeInverted = spanDays !== null && spanDays < 0;
  const canSearch = Boolean(bbox) && !areaTooLarge && !spanTooLong && !rangeInverted && !search.isPending;

  const submitSearch = (event) => {
    event.preventDefault();
    if (!canSearch) return;
    search.mutate({
      west: bbox.west,
      south: bbox.south,
      east: bbox.east,
      north: bbox.north,
      start_date: startDate,
      end_date: endDate,
      limit: provider?.max_results ?? undefined,
    });
  };

  const startFetch = () => {
    if (!selected || !selected.online || start.isPending) return;
    if (requestIdRef.current.productId !== selected.product_id) {
      requestIdRef.current = { productId: selected.product_id, value: createClientRequestId() };
    }
    start.mutate({
      scene_id: scene.id,
      product_id: selected.product_id,
      product_name: selected.name,
      client_request_id: requestIdRef.current.value,
    });
  };

  if (providersQuery.isPending) {
    return (
      <section className="rounded-xl border border-white/[0.08] bg-[#111114] p-5">
        <p className="flex items-center gap-2 text-xs text-zinc-500">
          <LoaderCircle size={14} className="animate-spin" /> Checking Copernicus availability...
        </p>
      </section>
    );
  }

  if (!provider?.enabled) {
    const reasons = {
      not_configured: 'Copernicus fetch is not configured on this server.',
      schema_not_applied: 'Copernicus fetch is unavailable until its database migration is applied.',
      disabled: 'Copernicus fetch is switched off on this server.',
    };
    return (
      <section className="rounded-xl border border-white/[0.08] bg-[#111114] p-5">
        <h3 className="text-sm font-semibold text-white">Fetch from Copernicus</h3>
        <div className="mt-3">
          <Notice tone="warn" icon={CircleAlert}>
            {reasons[provider?.reason] || 'Copernicus fetch is unavailable right now.'}
          </Notice>
        </div>
        <button type="button" onClick={onUseUpload} className="mt-3 text-xs font-semibold text-sky-300 transition hover:text-sky-200">
          Upload a file instead
        </button>
      </section>
    );
  }

  return (
    <section className="rounded-xl border border-white/[0.08] bg-[#111114] p-5">
      <div className="flex items-start gap-3">
        <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl border border-sky-400/25 bg-sky-400/10 text-sky-300">
          <Satellite size={17} />
        </span>
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-white">Fetch from Copernicus</h3>
          <p className="mt-1 text-xs leading-5 text-zinc-500">
            Draw the area you care about and pick a date range. We only list{' '}
            {provider.product_type} scenes with {provider.polarisation_channels}, because that is
            what this pipeline can process.
          </p>
          <button type="button" onClick={onUseUpload} className="mt-2 text-[11px] font-semibold text-sky-300 transition hover:text-sky-200">
            Upload a file instead
          </button>
        </div>
      </div>

      <form onSubmit={submitSearch} className="mt-5 space-y-4">
        <AoiMapPicker
          value={bbox}
          onChange={setBbox}
          footprints={results}
          selectedId={selectedId}
          onSelect={(productId) => {
            const match = results.find((item) => item.product_id === productId);
            if (match?.online) setSelectedId(productId);
          }}
        />

        <div className="grid gap-3 sm:grid-cols-[1fr_1fr_auto] sm:items-end">
          <div>
            <label htmlFor="cdse-start" className="block text-[11px] font-semibold text-zinc-400">From</label>
            <input
              id="cdse-start"
              type="date"
              value={startDate}
              max={endDate}
              onChange={(event) => setStartDate(event.target.value)}
              className="mt-1.5 w-full rounded-lg border border-white/[0.1] bg-[#09090b] px-3 py-2 text-xs text-white outline-none focus:border-sky-400/60 focus:ring-2 focus:ring-sky-400/10"
            />
          </div>
          <div>
            <label htmlFor="cdse-end" className="block text-[11px] font-semibold text-zinc-400">To</label>
            <input
              id="cdse-end"
              type="date"
              value={endDate}
              min={startDate}
              onChange={(event) => setEndDate(event.target.value)}
              className="mt-1.5 w-full rounded-lg border border-white/[0.1] bg-[#09090b] px-3 py-2 text-xs text-white outline-none focus:border-sky-400/60 focus:ring-2 focus:ring-sky-400/10"
            />
          </div>
          <button
            type="submit"
            disabled={!canSearch}
            className="inline-flex items-center justify-center gap-1.5 rounded-lg bg-sky-500 px-4 py-2 text-xs font-semibold text-white transition hover:bg-sky-400 disabled:cursor-not-allowed disabled:opacity-45"
          >
            {search.isPending ? <><LoaderCircle size={13} className="animate-spin" /> Searching</> : <><Search size={13} /> Search</>}
          </button>
        </div>

        {bbox && !areaTooLarge && (
          <p className="text-[11px] text-zinc-600">
            Area about {Math.round(areaSqKm).toLocaleString()} km².
          </p>
        )}
        {areaTooLarge && (
          <Notice tone="warn" icon={CircleAlert}>
            That area is about {Math.round(areaSqKm).toLocaleString()} km². Draw a box under{' '}
            {Math.round(maxArea).toLocaleString()} km².
          </Notice>
        )}
        {spanTooLong && (
          <Notice tone="warn" icon={CircleAlert}>
            Search a window of at most {maxDays} days.
          </Notice>
        )}
        {rangeInverted && (
          <Notice tone="warn" icon={CircleAlert}>The end date must not precede the start date.</Notice>
        )}
        {search.error && (
          <Notice tone="error" icon={CircleAlert}>{search.error.message}</Notice>
        )}
      </form>

      {search.isSuccess && (
        <div className="mt-5">
          <div className="flex items-center justify-between gap-3">
            <h4 className="text-xs font-semibold text-zinc-300">
              {results.length === 0 ? 'No scenes found' : `${results.length} scene${results.length === 1 ? '' : 's'} found`}
            </h4>
          </div>

          {results.length === 0 ? (
            <p className="mt-2 text-xs leading-5 text-zinc-500">
              Nothing matched. Try a wider date range or a different area.
            </p>
          ) : (
            <>
              <div className="mt-2">
                <Notice>
                  Sentinel-1 ships whole ~250×170 km frames. Each result{' '}
                  <span className="font-semibold text-zinc-300">covers</span> your area — it is not
                  cropped to it.
                </Notice>
              </div>
              <ul className="mt-3 space-y-2">
                {results.map((item) => {
                  const isSelected = item.product_id === selectedId;
                  const offline = !item.online;
                  return (
                    <li key={item.product_id}>
                      <button
                        type="button"
                        onClick={() => !offline && setSelectedId(item.product_id)}
                        disabled={offline}
                        title={offline ? 'This scene is in the long-term archive and must be ordered in the Copernicus Browser before it can be downloaded.' : undefined}
                        className={`w-full rounded-lg border px-3.5 py-3 text-left transition ${
                          offline
                            ? 'cursor-not-allowed border-white/[0.06] bg-white/[0.01] opacity-55'
                            : isSelected
                              ? 'border-sky-400/50 bg-sky-400/[0.08]'
                              : 'border-white/[0.08] bg-black/10 hover:border-sky-400/30 hover:bg-sky-400/[0.04]'
                        }`}
                      >
                        <div className="flex items-start justify-between gap-3">
                          <p className="min-w-0 break-all text-[11px] font-semibold text-zinc-200">{item.name}</p>
                          {offline && (
                            <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-zinc-600 bg-zinc-800 px-2 py-0.5 text-[10px] font-semibold text-zinc-300">
                              <Archive size={10} /> Long-term archive
                            </span>
                          )}
                        </div>
                        <p className="mt-1.5 text-[11px] text-zinc-500">
                          {formatSensed(item.sensing_start)} · {formatBytes(item.size_bytes)} ·{' '}
                          {item.polarisation_channels}
                        </p>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </>
          )}
        </div>
      )}

      {selected && (
        <div className="mt-5 rounded-lg border border-white/[0.08] bg-black/20 p-4">
          <p className="text-[11px] font-semibold uppercase tracking-[0.1em] text-zinc-500">Selected scene</p>
          <p className="mt-1.5 break-all text-xs font-medium text-zinc-200">{selected.name}</p>
          <p className="mt-1 text-[11px] text-zinc-500">
            {formatBytes(selected.size_bytes)} · downloads on the server, so you can close this tab.
          </p>
          {start.error && (
            <div className="mt-3"><Notice tone="error" icon={CircleAlert}>{start.error.message}</Notice></div>
          )}
          <button
            type="button"
            onClick={startFetch}
            disabled={start.isPending || start.isSuccess}
            className="mt-3 inline-flex items-center gap-1.5 rounded-lg bg-sky-500 px-3.5 py-2 text-xs font-semibold text-white transition hover:bg-sky-400 disabled:cursor-not-allowed disabled:opacity-45"
          >
            {start.isPending
              ? <><LoaderCircle size={13} className="animate-spin" /> Starting...</>
              : <><Download size={13} /> Fetch this scene</>}
          </button>
        </div>
      )}
    </section>
  );
}
