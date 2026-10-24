import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { viteSingleFile } from 'vite-plugin-singlefile'

// 产物为单个 index.html（内联 JS/CSS），由 mw console 直接 serve，
// 与现有 console_ui.py 的部署方式完全一致（零额外静态目录）。
export default defineConfig({
  plugins: [react(), viteSingleFile()],
  base: './',
  build: {
    outDir: 'dist',
    assetsInlineLimit: 100000000,
  },
  server: {
    port: 5199,
    proxy: {
      // 本地开发时把 API 打到远端控制台，前端改动即时可见
      '/api': { target: 'http://127.0.0.1:8700', changeOrigin: true },
      '/health': { target: 'http://127.0.0.1:8700', changeOrigin: true },
    },
  },
})
