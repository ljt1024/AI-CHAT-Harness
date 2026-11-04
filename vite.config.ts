import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import svgr from 'vite-plugin-svgr'
import { cpSync, existsSync, mkdirSync } from 'node:fs'
import path from 'path'

// pdf.js fetches its CMap tables, its standard fonts and its wasm decoders by
// URL while it renders, so they have to be served as files next to the app
// instead of being bundled. Copying them here keeps 3 MB of third-party data out
// of the repo and makes `vite dev` and a bare `vite build` agree on the location.
function preparePdfjsAssets() {
  const source = path.resolve(__dirname, 'node_modules/pdfjs-dist')
  const target = path.resolve(__dirname, 'public/pdfjs')
  if (!existsSync(source)) return
  mkdirSync(target, { recursive: true })
  for (const folder of ['cmaps', 'standard_fonts', 'wasm', 'iccs']) {
    const from = path.join(source, folder)
    if (existsSync(from)) cpSync(from, path.join(target, folder), { recursive: true })
  }
}

preparePdfjsAssets()

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), svgr()],
  server: {
    port: 5174,
    strictPort: true,
    proxy: {
      // changeOrigin stays false so the backend sees the browser's own Host.
      // The session cookie is same-origin, and the API rejects a write whose
      // Origin names a different host, so rewriting Host here would make every
      // mutating request look cross-origin in development.
      '/api': { target: process.env.AGENTFORGE_API_TARGET || 'http://127.0.0.1:8000', changeOrigin: false },
      '/ws': { target: process.env.AGENTFORGE_API_TARGET || 'http://127.0.0.1:8000', ws: true, changeOrigin: false },
    },
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src')
    }
  }
})
