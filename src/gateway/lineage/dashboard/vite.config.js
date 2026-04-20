import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
// Million.js: drop-in compiler that rewrites "hot" list-mapping components
// into an O(1) block-diff virtual DOM. Runs ahead of React with NO component
// code changes required — each component keeps its JSX and hooks. In auto
// mode the plugin picks candidates by heuristic (typically large .map()
// outputs — precisely our Sessions/Attempts/Playground tables). If the
// `million/compiler` package is not yet installed (e.g. fresh clone before
// npm install), the dynamic import fails at config load and Vite simply
// falls back to plain React, so this cannot block builds.
let million;
try {
  million = (await import('million/compiler')).default;
} catch {
  million = null;
}

export default defineConfig({
  plugins: [
    ...(million ? [million.vite({ auto: true })] : []),
    react(),
  ],
  base: '/lineage/',
  build: {
    outDir: '../static',
    // Clean stale bundles on each build. Previously left ~14 old JS files in
    // static/assets across deploys. `index.html` always points at the latest
    // hashed bundle, so the old files were only wasted bytes on disk.
    emptyOutDir: true,
    assetsDir: 'assets',
  },
  server: {
    proxy: {
      '/health': 'http://localhost:8000',
      '/metrics': 'http://localhost:8000',
      '/v1': 'http://localhost:8000',
    },
  },
});
