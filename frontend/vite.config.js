import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  optimizeDeps: {
    include: ['roslib'],
  },
  server:  { port: 3000, host: '0.0.0.0' },  // accessible from all network devices
  preview: { port: 3000, host: '0.0.0.0' },  // same for production preview
})
