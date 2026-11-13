import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// The dashboard talks to the Rust API on the same origin in production
// (`/api/...`). In development this proxy forwards to the dev server from
// api/README.md, which listens on 3001 by default.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.VITE_API_PROXY ?? 'http://127.0.0.1:3001',
        changeOrigin: false,
      },
    },
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
});
