import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // Backend CORS_ORIGINS default (backend/.env.example) matches this port.
    port: 5180,
    strictPort: true,
  },
})
