import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  optimizeDeps: {
    include: ['roslib'],
  },
  server:  { port: 3000, host: '0.0.0.0', allowedHosts: ['.trycloudflare.com'] },
    // accessible from all network devices, plus Cloudflare quick-tunnel URLs
  preview: { port: 3000, host: '0.0.0.0', allowedHosts: ['.trycloudflare.com'] },  // same for production preview
})
