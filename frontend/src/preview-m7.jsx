/**
 * REVIEW HARNESS — not part of the app, safe to delete.
 *
 * Mounts the real CopernicusSearchPanel and AoiMapPicker against a mock API so
 * the new UI can be exercised with no backend, no Supabase auth and no CDSE
 * quota. Delete `frontend/preview-m7.html` and `frontend/src/preview-m7.jsx`
 * when you are done reviewing.
 */
import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import CopernicusSearchPanel from './components/CopernicusSearchPanel';
import './styles/index.css';

const PROVIDERS = {
  copernicus: {
    enabled: true,
    product_type: 'IW_GRDH_1S',
    polarisation_channels: 'VV&VH',
    max_results: 50,
    max_search_days: 90,
    max_aoi_sq_km: 250000,
  },
};

// Shaped exactly like the real POST /acquisitions/search response.
const RESULTS = [
  {
    product_id: '4f2a1c88-0001-4a3b-9c11-aa0000000001',
    name: 'S1A_IW_GRDH_1SDV_20260712T003512_20260712T003537_054321_069ABC_1F2D.SAFE',
    sensing_start: '2026-07-12T00:35:12Z',
    size_bytes: 1043654321,
    polarisation_channels: 'VV&VH',
    online: true,
    footprint: {
      type: 'Polygon',
      coordinates: [[[80.10, 12.90], [82.45, 13.30], [82.05, 14.85], [79.70, 14.45], [80.10, 12.90]]],
    },
  },
  {
    product_id: '4f2a1c88-0002-4a3b-9c11-aa0000000002',
    name: 'S1A_IW_GRDH_1SDV_20260630T003511_20260630T003536_054146_0695FE_7C4A.SAFE',
    sensing_start: '2026-06-30T00:35:11Z',
    size_bytes: 998244352,
    polarisation_channels: 'VV&VH',
    online: true,
    footprint: {
      type: 'Polygon',
      coordinates: [[[79.60, 13.40], [81.95, 13.80], [81.55, 15.35], [79.20, 14.95], [79.60, 13.40]]],
    },
  },
  {
    // The greyed-out case: in the long-term archive, so unselectable.
    product_id: '4f2a1c88-0003-4a3b-9c11-aa0000000003',
    name: 'S1C_IW_GRDH_1SDV_20260118T003509_20260118T003534_051897_0641B2_33E9.SAFE',
    sensing_start: '2026-01-18T00:35:09Z',
    size_bytes: 1071382528,
    polarisation_channels: 'VV&VH',
    online: false,
    footprint: {
      type: 'Polygon',
      coordinates: [[[80.55, 12.40], [82.90, 12.80], [82.50, 14.35], [80.15, 13.95], [80.55, 12.40]]],
    },
  },
];

const delay = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });

function makeMockApi(log, { enabled }) {
  return {
    acquisitions: {
      async providers() {
        await delay(250);
        return enabled ? PROVIDERS : { copernicus: { enabled: false, reason: 'not_configured' } };
      },
      async search(input) {
        log(`POST /acquisitions/search ${JSON.stringify(input)}`);
        await delay(900);
        return { items: RESULTS };
      },
      async start(input) {
        log(`POST /acquisitions ${JSON.stringify(input)}`);
        await delay(700);
        return { id: 'acq-1', status: 'queued' };
      },
    },
  };
}

function Harness() {
  const [enabled, setEnabled] = useState(true);
  const [lines, setLines] = useState([]);
  const [nonce, setNonce] = useState(0);
  const log = (line) => setLines((prev) => [...prev, line]);
  const api = makeMockApi(log, { enabled });

  return (
    <div className="min-h-screen bg-[#09090b] p-6 text-zinc-200">
      <div className="mx-auto max-w-3xl">
        <p className="text-[11px] font-bold uppercase tracking-[0.16em] text-sky-300">Review harness</p>
        <h1 className="mt-2 text-xl font-semibold tracking-tight text-white">
          Fetch from Copernicus
        </h1>
        <p className="mt-2 text-sm leading-6 text-zinc-500">
          The real panel and map, wired to mock data. Try: <strong className="text-zinc-300">Draw area</strong>,
          drag a box on the map, then <strong className="text-zinc-300">Search</strong>. The third
          result is deliberately in the long-term archive, so it renders muted and cannot be picked.
        </p>

        <label className="mt-4 flex items-center gap-2 text-xs text-zinc-400">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(event) => { setEnabled(event.target.checked); setNonce((n) => n + 1); }}
          />
          Provider configured (uncheck to see how it degrades when the server has no credentials)
        </label>

        <div className="mt-5">
          <CopernicusSearchPanel
            key={nonce}
            api={api}
            scene={{ id: 'scene-preview-1', name: 'Preview scene' }}
            onStarted={() => log('onStarted() -> workspace would refresh, panel unmounts')}
            onUseUpload={() => log('onUseUpload() -> ?source= cleared, upload panel would mount')}
          />
        </div>

        <div className="mt-5 rounded-xl border border-white/[0.08] bg-[#111114] p-4">
          <p className="text-xs font-semibold text-zinc-300">Calls the panel made</p>
          {lines.length === 0
            ? <p className="mt-2 text-[11px] text-zinc-600">Nothing yet.</p>
            : <ol className="mt-2 space-y-1">{lines.map((line, index) => (
              <li key={index} className="break-all font-mono text-[10px] leading-4 text-zinc-500">{line}</li>
            ))}</ol>}
        </div>
      </div>
    </div>
  );
}

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
createRoot(document.getElementById('root')).render(
  <QueryClientProvider client={queryClient}>
    <Harness />
  </QueryClientProvider>,
);
