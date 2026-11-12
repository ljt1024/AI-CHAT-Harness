import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import svgr from 'vite-plugin-svgr'
import { cpSync, existsSync, mkdirSync, readFileSync } from 'node:fs'
import path from 'path'

// pdf.js fetches its CMap tables, its standard fonts, its wasm decoders and its
// worker by URL while it renders, so they have to be served as files next to the
// app instead of being bundled. Copying them here keeps 3 MB of third-party data
// out of the repo and makes `vite dev` and a bare `vite build` agree on the
// location.
//
// The worker belongs in this group even though Vite could fingerprint it: it is
// referenced by a URL we write by hand, and its content only changes when the
// library changes, so a content hash is the *same* string across every fix that
// does not touch the bytes -- a corrected Content-Type would ship under a URL
// browsers already hold (cache: immutable, 30 days). The version query is the
// cache key instead; see ADR-0023.
function preparePdfjsAssets() {
  const source = path.resolve(__dirname, 'node_modules/pdfjs-dist')
  const target = path.resolve(__dirname, 'public/pdfjs')
  if (!existsSync(source)) return
  mkdirSync(target, { recursive: true })
  for (const folder of ['cmaps', 'standard_fonts', 'wasm', 'iccs']) {
    const from = path.join(source, folder)
    if (existsSync(from)) cpSync(from, path.join(target, folder), { recursive: true })
  }
  const worker = path.join(source, 'build/pdf.worker.min.mjs')
  if (existsSync(worker)) cpSync(worker, path.join(target, 'pdf.worker.min.mjs'))
}

function pdfjsVersion() {
  try {
    const manifest = path.resolve(__dirname, 'node_modules/pdfjs-dist/package.json')
    return JSON.parse(readFileSync(manifest, 'utf8')).version as string
  } catch {
    return '0'
  }
}

preparePdfjsAssets()

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), svgr()],
  define: {
    __PDFJS_VERSION__: JSON.stringify(pdfjsVersion()),
  },
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
