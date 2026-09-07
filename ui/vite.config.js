import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  build: {
    // Copie ensuite dans sortilege/web/static au build Docker.
    outDir: 'dist',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    // En developpement seulement : l'API tourne sur son propre port. En
    // production il n'y a qu'un seul port, FastAPI sert l'UI compilee.
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8117',
        changeOrigin: true,
      },
    },
  },
})
