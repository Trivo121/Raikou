import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// maplibre-gl v6 starts its worker from a file next to its own module. Bundled into
// the app chunk, "next to it" is /assets/, where no worker exists, so in production
// every GeoJSON layer (the drawn area, its handles, the search footprints) silently
// never renders, while the dev server, which still serves maplibre from node_modules,
// works. Ship the worker and the shared chunk it imports under /maplibre/; the
// production build points maplibre at them (see AoiMapPicker.jsx).
const MAPLIBRE_WORKER_FILES = ['maplibre-gl-worker.mjs', 'maplibre-gl-shared.mjs'];

function maplibreWorker() {
  const dist = path.join(path.dirname(createRequire(import.meta.url).resolve('maplibre-gl/package.json')), 'dist');
  return {
    name: 'maplibre-worker',
    apply: 'build',
    generateBundle() {
      MAPLIBRE_WORKER_FILES.forEach((file) => {
        this.emitFile({ type: 'asset', fileName: `maplibre/${file}`, source: readFileSync(path.join(dist, file)) });
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), maplibreWorker()],
  // Expose the former CRA public variables during the one-time migration.
  // Only VITE_* and REACT_APP_* names are browser-visible; backend-only
  // SUPABASE_SERVICE_KEY values remain unavailable to Vite bundles.
  envPrefix: ['VITE_', 'REACT_APP_'],
  // The codebase uses JSX in .js files. Keeping this explicit avoids a
  // disruptive file-extension migration while moving from Create React App.
  esbuild: {
    include: /src\/.*\.(js|jsx)$/,
    loader: 'jsx',
  },
  optimizeDeps: {
    exclude: ['maplibre-gl'],
    esbuildOptions: {
      loader: {
        '.js': 'jsx',
      },
    },
  },
});
