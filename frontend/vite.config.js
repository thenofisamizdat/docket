import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The Docket board SPA. Served by the backend under /docket, so built asset URLs
// are prefixed accordingly. `npm run build` writes straight into the Python
// package's served bundle (src/docket_dev/web/dist), so the shipped UI is always
// rebuilt from this source — no manual copy, no drift. In dev we proxy /api → a
// local `docket serve` (default :8011).
export default defineConfig({
  base: '/docket/',
  plugins: [react()],
  build: {
    outDir: '../src/docket_dev/web/dist',
    emptyOutDir: true,
  },
  server: {
    port: 5175,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8011', changeOrigin: true },
    },
  },
})
