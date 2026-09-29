import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.')
  return {
    plugins: [vue()],
    server: {
      proxy: {
        '/v1': env.VITE_API_PROXY_TARGET ?? 'http://127.0.0.1:8080',
      },
    },
  }
})
