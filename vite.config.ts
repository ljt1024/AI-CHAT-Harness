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
      '/api': process.env.AGENTFORGE_API_TARGET || 'http://127.0.0.1:8000',
      '/ws': { target: process.env.AGENTFORGE_API_TARGET || 'http://127.0.0.1:8000', ws: true },
    },
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src')
    }
  }
})
