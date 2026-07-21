import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
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
    esbuildOptions: {
      loader: {
        '.js': 'jsx',
      },
    },
  },
});
