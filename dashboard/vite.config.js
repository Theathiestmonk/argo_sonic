import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Separate app from frontend/ (the robot-local operator UI, port 3000) —
// this one is meant to be deployed remotely, never installed on a robot.
export default defineConfig({
  plugins: [react()],
  server:  { port: 5173, host: '0.0.0.0' },
  preview: { port: 5173, host: '0.0.0.0' },
})
