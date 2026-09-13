import { useRef, useEffect, useState } from 'react'
import { ros } from '../ros'

// Camera topics to try - ordered by priority
const CAMERA_TOPICS = [
  // HP60C RGB/Color camera (raw Image format)
  { topic: '/ascamera_hp60c/camera_publisher/rgb0/image', type: 'sensor_msgs/Image', format: 'rgb' },

  // Compressed RGB cameras (fallback)
  { topic: '/camera/image_raw/compressed', type: 'sensor_msgs/CompressedImage', format: 'rgb_compressed' },
  { topic: '/usb_cam/image_raw/compressed', type: 'sensor_msgs/CompressedImage', format: 'rgb_compressed' },
  { topic: '/rgb/image_raw/compressed', type: 'sensor_msgs/CompressedImage', format: 'rgb_compressed' },

  // Depth cameras
  { topic: '/ascamera_hp60c/camera_publisher/depth0/image_raw', type: 'sensor_msgs/Image', format: 'depth' },
  { topic: '/depth_filtered', type: 'sensor_msgs/Image', format: 'depth' },
  { topic: '/camera/depth/image', type: 'sensor_msgs/Image', format: 'depth' },
]

export default function CameraFeed() {
  const canvasRef = useRef(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [activeTopic, setActiveTopic] = useState(null)
  const [debugInfo, setDebugInfo] = useState('')
  const subRef = useRef(null)
  const foundRef = useRef(false)

  const addDebug = (msg) => {
    console.log('[CameraFeed]', msg)
    setDebugInfo(prev => prev ? prev + '\n' + msg : msg)
  }

  useEffect(() => {
    if (!canvasRef.current) return

    if (!ros || typeof ros.isConnected !== 'function' || !ros.isConnected()) {
      addDebug('Not connected to ROS')
      return
    }

    addDebug('Starting camera feed subscription...')
    const canvas = canvasRef.current
    const ctx = canvas.getContext('2d')
    if (!ctx) {
      addDebug('Canvas context not available')
      return
    }

    let activeSubscription = null

    // Handle raw RGB/grayscale Image message
    const handleRawImage = (msg, isDepth = false) => {
      try {
        const { width, height, data, encoding } = msg

        addDebug(`Received ${isDepth ? 'depth' : 'RGB'} frame: ${width}x${height}, encoding=${encoding}`)

        if (!data || !width || !height) {
          addDebug('Invalid image data')
          return
        }

        // Convert data to Uint8Array if needed (roslib might send as string or array)
        let bytes
        if (typeof data === 'string') {
          // Base64 encoded
          const binaryString = atob(data)
          bytes = new Uint8Array(binaryString.length)
          for (let i = 0; i < binaryString.length; i++) {
            bytes[i] = binaryString.charCodeAt(i)
          }
        } else if (Array.isArray(data)) {
          bytes = new Uint8Array(data)
        } else if (data instanceof Uint8Array) {
          bytes = data
        } else {
          addDebug(`Unknown data type: ${typeof data}`)
          return
        }

        const imageData = ctx.createImageData(width, height)
        const imgData = imageData.data

        // Handle different encodings
        if (encoding === 'rgb8') {
          // RGB8: 3 bytes per pixel
          for (let i = 0; i < bytes.length; i += 3) {
            const pixelIdx = (i / 3) * 4
            imgData[pixelIdx] = bytes[i]         // R
            imgData[pixelIdx + 1] = bytes[i + 1] // G
            imgData[pixelIdx + 2] = bytes[i + 2] // B
            imgData[pixelIdx + 3] = 255          // A
          }
        } else if (encoding === 'bgr8') {
          // BGR8: 3 bytes per pixel (OpenCV format - HP60C uses this)
          for (let i = 0; i < bytes.length; i += 3) {
            const pixelIdx = (i / 3) * 4
            imgData[pixelIdx] = bytes[i + 2]     // R (from B position)
            imgData[pixelIdx + 1] = bytes[i + 1] // G
            imgData[pixelIdx + 2] = bytes[i]     // B (from R position)
            imgData[pixelIdx + 3] = 255          // A
          }
        } else if (encoding === 'mono8' || encoding === '8UC1') {
          // Grayscale/Depth: 1 byte per pixel
          for (let i = 0; i < bytes.length; i++) {
            const pixelIdx = i * 4
            imgData[pixelIdx] = bytes[i]         // R
            imgData[pixelIdx + 1] = bytes[i]     // G
            imgData[pixelIdx + 2] = bytes[i]     // B
            imgData[pixelIdx + 3] = 255          // A
          }
        } else if (encoding === '16UC1' || encoding === 'mono16') {
          // 16-bit depth: 2 bytes per pixel
          for (let i = 0; i < bytes.length; i += 2) {
            const pixelIdx = (i / 2) * 4
            const depthValue = (bytes[i + 1] << 8) | bytes[i]
            // Scale 16-bit depth to 0-255
            const normalized = Math.min(255, Math.floor((depthValue / 65535) * 255))
            imgData[pixelIdx] = normalized
            imgData[pixelIdx + 1] = normalized
            imgData[pixelIdx + 2] = normalized
            imgData[pixelIdx + 3] = 255
          }
        } else {
          addDebug(`Unsupported encoding: ${encoding}`)
          return
        }

        ctx.putImageData(imageData, 0, 0)
        addDebug(`Drew frame to canvas`)
      } catch (err) {
        addDebug(`Error handling image: ${err.message}`)
      }
    }

    // Handle compressed image
    const handleCompressedImage = (msg) => {
      try {
        if (!msg.data || !msg.data.length) {
          addDebug('No compressed image data')
          return
        }

        const binaryString = atob(msg.data)
        const bytes = new Uint8Array(binaryString.length)
        for (let i = 0; i < binaryString.length; i++) {
          bytes[i] = binaryString.charCodeAt(i)
        }
        const blob = new Blob([bytes], { type: msg.format || 'image/jpeg' })
        const url = URL.createObjectURL(blob)

        const img = new Image()
        img.onload = () => {
          drawImage(ctx, img, canvas)
          URL.revokeObjectURL(url)
        }
        img.onerror = () => {
          addDebug('Failed to load compressed image')
          URL.revokeObjectURL(url)
        }
        img.src = url
      } catch (err) {
        addDebug(`Error handling compressed image: ${err.message}`)
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
        addDebug(`Trying topic: ${topic} (${type})`)

        const rosSubscriber = ros.topic(topic, type, { queue_size: 1 })

        let messageCount = 0
        const subscription = rosSubscriber?.subscribe(msg => {
          try {
            messageCount++

            // Mark as found on first message
            if (!foundRef.current && messageCount === 1) {
              foundRef.current = true
              setActiveTopic(`${topic.split('/').pop()}`)
              setError(null)
              addDebug(`✓ Connected to ${topic}`)
            }

            setLoading(false)

            // Route to appropriate handler based on format
            if (format === 'rgb' || format === 'depth') {
              handleRawImage(msg, format === 'depth')
            } else if (format === 'rgb_compressed') {
              handleCompressedImage(msg)
            }
          } catch (err) {
            addDebug(`Message handler error: ${err.message}`)
          }
        })

        activeSubscription = subscription

        // If this is not the last topic, try the next one after a timeout
        if (index < CAMERA_TOPICS.length - 1) {
          setTimeout(() => {
            if (!foundRef.current) {
              addDebug(`No messages on ${topic}, trying next...`)
              subscription?.unsubscribe()
              tryTopic(CAMERA_TOPICS[index + 1], index + 1)
            }
          }, 500)
        } else if (!foundRef.current) {
          // Last topic and still not found
          addDebug('No camera topics found - check robot connection')
          setError('No camera feed available')
          setLoading(false)
        }
      } catch (err) {
        addDebug(`Topic subscription error: ${err.message}`)
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
    <div style={{ position: 'relative', width: '100%', height: '100%', background: '#000', overflow: 'hidden', margin: 0, padding: 0 }}>
      <canvas
        ref={canvasRef}
        width={640}
        height={480}
        style={{
          display: 'block',
          width: '100%',
          height: '100%',
          objectFit: 'contain',
          margin: 0,
          padding: 0,
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
          flexDirection: 'column',
          gap: 8,
        }}>
          <div>Waiting for camera feed...</div>
          <div style={{ fontSize: 10, color: 'var(--muted)', opacity: 0.7, maxWidth: 300, textAlign: 'center' }}>
            {debugInfo && debugInfo.split('\n').slice(-2).join('\n')}
          </div>
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
