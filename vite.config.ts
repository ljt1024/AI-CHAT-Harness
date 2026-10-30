import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import svgr from 'vite-plugin-svgr'
import path from 'path'

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
