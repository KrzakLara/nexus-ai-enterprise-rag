import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Same-origin API in development: /api is proxied to the backend, so the HttpOnly session cookie
// is first-party and no CORS is involved (backend-for-frontend). VITE_PROXY_TARGET: the API as
// seen from the Vite process (http://api:8000 inside Docker Compose).
const target = process.env.VITE_PROXY_TARGET || 'http://localhost:8000';

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0', port: 5173,
    proxy: { '/api': { target, changeOrigin: false, xfwd: true } },
  },
});
