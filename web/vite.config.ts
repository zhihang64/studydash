import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

/**
 * 构建产物直接落到 Python 包里（``src/studydash/web/static``），
 * 这样打包 exe 时不需要额外的拷贝步骤——PyInstaller 只要带上这个目录就行。
 *
 * ``base`` 用 ``/``：服务由 FastAPI 挂在根路径上，前端路由是 history 模式，
 * 深链接靠后端的 SPA 回退兜住。
 */
export default defineConfig({
  plugins: [vue()],
  base: '/',
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    outDir: fileURLToPath(new URL('../src/studydash/web/static', import.meta.url)),
    emptyOutDir: true,
    target: 'es2020',
    sourcemap: false,
    // 单个 chunk 超过 500 kB 时 vite 会一直唠叨。这个应用总量本来就小，
    // 但把 vue 单独拆出去能让升级前端时用户少下一遍。
    chunkSizeWarningLimit: 700,
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ['vue', 'vue-router', 'pinia'],
        },
      },
    },
  },
  server: {
    // 开发时把 /api 转给后端：另开一个终端跑 `studydash web --no-open`
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8788',
        changeOrigin: true,
      },
    },
  },
})
