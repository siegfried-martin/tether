import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'

// In dev, `npm run dev` proxies the API to the backend on TETHER_PORT, read from
// the repo-root .env (the same file the backend reads). Cookies are shared across
// localhost ports.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '..', 'TETHER_')
  const api = `http://localhost:${env.TETHER_PORT || '8000'}`
  return {
    plugins: [react()],
    server: {
      proxy: { '/api': api, '/auth': api, '/accounts': api },
    },
  }
})
