import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    // The backend has no CORS headers yet (Day 3, docs/TODO.md), so a
    // cross-origin fetch from the dev server would fail its preflight.
    // Proxying keeps the browser's request same-origin; production instead
    // sets VITE_API_URL to the deployed backend host.
    proxy: { '/api': 'http://localhost:8000' },
  },
})
