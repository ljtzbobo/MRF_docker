import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig(({ mode }) => ({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: { '/api': loadEnv(mode, '.', 'MRF_').MRF_API_TARGET || 'http://127.0.0.1:8000' },
  },
}))
