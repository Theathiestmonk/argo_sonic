import { useRef, useEffect, useState } from 'react'
import { ros } from '../ros'

// Camera topics to try - ordered by priority
const CAMERA_TOPICS = [
  // RGB/Color cameras
  { topic: '/camera/image_raw/compressed', type: 'sensor_msgs/CompressedImage', format: 'rgb' },
  { topic: '/usb_cam/image_raw/compressed', type: 'sensor_msgs/CompressedImage', format: 'rgb' },
  { topic: '/rgb/image_raw/compressed', type: 'sensor_msgs/CompressedImage', format: 'rgb' },
  { topic: '/oak/rgb/preview/image_raw', type: 'sensor_msgs/CompressedImage', format: 'rgb' },
  { topic: '/ascamera_hp60c/camera_publisher/rgb0/image', type: 'sensor_msgs/CompressedImage', format: 'rgb' },

  // Depth visualizations
  { topic: '/depth_filtered', type: 'sensor_msgs/Image', format: 'depth' },
  { topic: '/camera/depth/image', type: 'sensor_msgs/Image', format: 'depth' },
  { topic: '/ascamera_hp60c/camera_publisher/depth0/image', type: 'sensor_msgs/Image', format: 'depth' },
]

export default function CameraFeed() {
  const canvasRef = useRef(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [activeTopic, setActiveTopic] = useState(null)
  const subRef = useRef(null)
  const foundRef = useRef(false)

  useEffect(() => {
    if (!canvasRef.current || !ros?.connection?.isConnected) return

    const canvas = canvasRef.current
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    let activeSubscription = null

    // Handle RGB compressed image
    const handleCompressedImage = (msg) => {
      try {
        if (!msg.data || !msg.data.length) return

        // Decode base64 image data
        const binaryString = atob(msg.data)
        const bytes = new Uint8Array(binaryString.length)
        for (let i = 0; i < binaryString.length; i++) {
          bytes[i] = binaryString.charCodeAt(i)
        }
        const blob = new Blob([bytes], { type: msg.format || 'image/jpeg' })
        const url = URL.createObjectURL(blob)

        // Draw image on canvas
        const img = new Image()
        img.onload = () => {
          drawImage(ctx, img, canvas)
          URL.revokeObjectURL(url)
        }
        img.onerror = () => {
          URL.revokeObjectURL(url)
        }
        img.src = url
      } catch (err) {
        // Silently ignore individual frame errors
      }
    }

    // Handle depth image (grayscale)
    const handleDepthImage = (msg) => {
      try {
        if (!msg.data || !msg.step) return

        const width = msg.width
        const height = msg.height
        const imgData = ctx.createImageData(width, height)
        const data = imgData.data

        // Convert depth data to grayscale visualization
        for (let i = 0; i < msg.data.length; i += 2) {
          // Depth is 16-bit, so read 2 bytes at a time
          const depthValue = (msg.data[i + 1] << 8) | msg.data[i]

          // Scale depth to 0-255 (assuming depth range 0-3000mm)
          const normalized = Math.min(255, Math.floor((depthValue / 3000) * 255))

          const pixelIndex = (i / 2) * 4
          data[pixelIndex] = normalized     // R
          data[pixelIndex + 1] = normalized // G
          data[pixelIndex + 2] = normalized // B
          data[pixelIndex + 3] = 255        // A
        }

        ctx.putImageData(imgData, 0, 0)
      } catch (err) {
        // Silently ignore individual frame errors
      }
    }

    const drawImage = (ctx, img, canvas) => {
      const canvasWidth = canvas.width
      const canvasHeight = canvas.height
      const scale = Math.min(canvasWidth / img.width, canvasHeight / img.height)
      const x = (canvasWidth - img.width * scale) / 2
      const y = (canvasHeight - img.height * scale) / 2

      ctx.fillStyle = '#000'
      ctx.fillRect(0, 0, canvasWidth, canvasHeight)
      ctx.drawImage(img, x, y, img.width * scale, img.height * scale)
    }

    // Try each camera topic until we find one that works
    const tryTopic = async (topicConfig, index) => {
      if (foundRef.current) return

      try {
        const { topic, type, format } = topicConfig
        const rosSubscriber = ros.topic(topic, type, { queue_size: 1 })

        let messageCount = 0
        const subscription = rosSubscriber?.subscribe(msg => {
          try {
            messageCount++

            // Mark as found on first message
            if (!foundRef.current && messageCount === 1) {
              foundRef.current = true
              setActiveTopic(`${topic} (${format})`)
              setError(null)
            }

            setLoading(false)

            // Route to appropriate handler based on format
            if (format === 'rgb') {
              handleCompressedImage(msg)
            } else if (format === 'depth') {
              handleDepthImage(msg)
            }
          } catch (err) {
            // Silently ignore individual frame errors
          }
        })

        activeSubscription = subscription

        // If this is not the last topic, try the next one after a timeout
        if (index < CAMERA_TOPICS.length - 1) {
          setTimeout(() => {
            if (!foundRef.current) {
              subscription?.unsubscribe()
              tryTopic(CAMERA_TOPICS[index + 1], index + 1)
            }
          }, 300)
        } else if (!foundRef.current) {
          // Last topic and still not found
          setError('No camera feed available. Ensure camera is connected and driver is running.')
          setLoading(false)
        }
      } catch (err) {
        // Try next topic on error
        if (index < CAMERA_TOPICS.length - 1) {
          setTimeout(() => {
            tryTopic(CAMERA_TOPICS[index + 1], index + 1)
          }, 100)
        } else {
          setError('Camera not available')
          setLoading(false)
        }
      }
    }

    // Start with the first topic
    tryTopic(CAMERA_TOPICS[0], 0)

    return () => {
      activeSubscription?.unsubscribe()
    }
  }, [])

  return (
    <div style={{ position: 'relative', width: '100%', height: '100%', background: '#000', borderRadius: 12, overflow: 'hidden' }}>
      <canvas
        ref={canvasRef}
        width={760}
        height={200}
        style={{
          display: 'block',
          width: '100%',
          height: '100%',
          objectFit: 'contain',
        }}
      />
      {loading && (
        <div style={{
          position: 'absolute',
          top: 0,
          left: 0,
          right: 0,
          bottom: 0,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          background: 'rgba(0,0,0,0.5)',
          fontSize: 12,
          color: 'var(--muted)',
          zIndex: 10,
        }}>
          Waiting for camera feed...
        </div>
      )}
      {error && (
        <div style={{
          position: 'absolute',
          top: 0,
          left: 0,
          right: 0,
          bottom: 0,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          background: 'rgba(200,0,0,0.2)',
          fontSize: 11,
          color: 'var(--muted)',
          padding: 12,
          textAlign: 'center',
          zIndex: 10,
        }}>
          {error}
        </div>
      )}
    </div>
  )
}
