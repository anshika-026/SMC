import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  return {
    plugins: [react()],
    // "/" for its own host; "/smc/" when served under tatv.dewin.in (see
    // .env.production and src/lib/appBase.js).
    base: env.VITE_BASE_PATH || '/',
    server: {
      // Fixed, non-default port — deco-vision (a separate, unrelated project)
      // runs its own dev server on 5173 on this same machine.
      port: 5180,
      strictPort: true,
    },
  }
})
