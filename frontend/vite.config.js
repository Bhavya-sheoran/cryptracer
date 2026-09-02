import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Plain JS/JSX project. There is no tsconfig and no .tsx anywhere by design;
// ESLint is the static gate instead of tsc.
export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    watch: { usePolling: true }, // reliable HMR from a bind mount on Windows/Docker
  },
});
