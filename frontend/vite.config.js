import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // Forward the backend health probe to FastAPI in development.
      '/health': 'http://localhost:8000',
    },
  },
});
