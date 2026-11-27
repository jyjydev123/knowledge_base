import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

/**
 * 代理只服务于开发：/api → 本机 8000，省掉 CORS 这一层噪声。
 *
 * ⚠️ 两个必须记住的点：
 *   1. **壳里没有这层代理**（Capacitor 直接加载打包好的静态文件），
 *      所以生产/手机上要用 VITE_API_BASE 指后端绝对地址；
 *   2. SSE 必须关掉代理缓冲，否则流式回答会被攒成一坨，
 *      现象是"等十几秒然后一次性全出来"—— 看起来像后端慢，其实是这里。
 */
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    host: '0.0.0.0', // 手机在同一局域网时可以直接访问 http://<电脑IP>:5173
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      '/health': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
