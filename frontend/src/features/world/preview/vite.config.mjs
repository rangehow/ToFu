/** Isolated build of the office preview (fixture data only); the application's own build graph is untouched.
 * Output goes to WORLD_PREVIEW_OUT or the repo-local .cache. */
import { defineConfig } from 'vite';
import { fileURLToPath } from 'node:url';

export default defineConfig({
  root: fileURLToPath(new URL('.', import.meta.url)),
  base: './',
  cacheDir: fileURLToPath(new URL('../../../../../.cache/world-preview/vite', import.meta.url)),
  build: {
    outDir: process.env.WORLD_PREVIEW_OUT || fileURLToPath(new URL('../../../../../.cache/world-preview/dist', import.meta.url)),
    emptyOutDir: true,
    assetsInlineLimit: 0,
    target: 'es2022',
    reportCompressedSize: false,
  },
  worker: { format: 'es' },
  optimizeDeps: { noDiscovery: true, include: [] },
});
