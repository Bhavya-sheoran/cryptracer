import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Plain JS/JSX project. There is no tsconfig and no .tsx anywhere by design;
// ESLint is the static gate instead of tsc.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './src/test/setup.js',
    // Excluded explicitly: without this, vitest walks node_modules and tries
    // to run other packages' test files.
    include: ['src/**/*.test.{js,jsx}'],
  },
  server: {
    host: true,
    port: 5173,
    watch: { usePolling: true }, // reliable HMR from a bind mount on Windows/Docker
    // The dashboard calls /api/v1/... as a relative path so it works unchanged
    // behind Caddy's https origin. This proxy is what makes the same relative
    // path work when the dev server is hit directly on 5174.
    proxy: {
      '/api': {
        target: 'http://backend:8000',
        changeOrigin: true,
        ws: true, // the alert socket rides the same prefix
      },
    },
  },
});
