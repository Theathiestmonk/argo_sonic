import { useRef, useEffect, useCallback } from 'react'

// True neutral greyscale (classic occupancy-grid look), tuned dark to sit
// naturally inside the app's dark theme instead of a stark white rectangle.
const UNK  = [50,  50,  50]
const FREE = [222, 222, 222]
const OCC  = [16,  16,  16]

// Converts world (wx,wy) to canvas pixel given map info and canvas layout.
function worldToCanvas(wx, wy, md, offX, offY, scale) {
  const col = (wx - md.origin.x) / md.resolution
  const row = md.height - 1 - (wy - md.origin.y) / md.resolution
  return [offX + col * scale, offY + row * scale]
}

export default function MapCanvas({ mapData, robotPose, labels = [], frontiers = [], clickable = false, onMapClick }) {
  const canvasRef  = useRef(null)
  const offRef     = useRef(null)   // { img: ImageBitmap, md: mapData }

  // Rebuild the offscreen bitmap whenever the map data changes.
  useEffect(() => {
    if (!mapData) return
    const { width, height, data } = mapData
    const imgData = new ImageData(width, height)

    for (let row = 0; row < height; row++) {
      const canvasRow = height - 1 - row   // flip: ROS row-0 = bottom
      for (let col = 0; col < width; col++) {
        const val = data[row * width + col]
        const px  = (canvasRow * width + col) * 4
        const c   = val === -1 ? UNK : val <= 25 ? FREE : OCC
        imgData.data[px]     = c[0]
        imgData.data[px + 1] = c[1]
        imgData.data[px + 2] = c[2]
        imgData.data[px + 3] = 255
      }
    }

    // OffscreenCanvas is the fastest path for creating a bitmap.
    const osc = new OffscreenCanvas(width, height)
    osc.getContext('2d').putImageData(imgData, 0, 0)
    osc.transferToImageBitmap
      ? createImageBitmap(osc).then(bmp => { offRef.current = { bmp, md: mapData } })
      : (() => {
          // Fallback for Safari
          const tmp = document.createElement('canvas')
          tmp.width = width; tmp.height = height
          tmp.getContext('2d').putImageData(imgData, 0, 0)
          offRef.current = { bmp: tmp, md: mapData }
        })()
  }, [mapData])

  // Redraw canvas on every relevant prop change.
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    const W = canvas.width
    const H = canvas.height

    ctx.clearRect(0, 0, W, H)

    if (!offRef.current) {
      ctx.fillStyle = 'rgba(90,90,90,0.18)'
      ctx.fillRect(0, 0, W, H)
      ctx.fillStyle = 'rgba(160,160,160,0.55)'
      ctx.font = '14px Inter,sans-serif'
      ctx.textAlign = 'center'
      ctx.textBaseline = 'middle'
      ctx.fillText('Waiting for /map…', W / 2, H / 2)
      return
    }

    const { bmp, md } = offRef.current
    const scale  = Math.min(W / md.width, H / md.height)
    const drawW  = md.width  * scale
    const drawH  = md.height * scale
    const offX   = (W - drawW) / 2
    const offY   = (H - drawH) / 2

    ctx.drawImage(bmp, offX, offY, drawW, drawH)

    const toC = (wx, wy) => worldToCanvas(wx, wy, md, offX, offY, scale)

    // Frontier dots (blue)
    frontiers.forEach(f => {
      const [px, py] = toC(f.x, f.y)
      ctx.beginPath(); ctx.arc(px, py, 5, 0, Math.PI * 2)
      ctx.fillStyle = 'rgba(127,168,232,0.35)'; ctx.fill()
      ctx.strokeStyle = '#7fa8e8'; ctx.lineWidth = 1.5; ctx.stroke()
    })

    // Labels (green pins)
    labels.forEach(l => {
      const [px, py] = toC(l.wx, l.wy)
      ctx.beginPath(); ctx.arc(px, py, 9, 0, Math.PI * 2)
      ctx.fillStyle = 'rgba(59,240,155,0.18)'; ctx.fill()
      ctx.strokeStyle = '#3bf09b'; ctx.lineWidth = 2; ctx.stroke()
      ctx.fillStyle = '#3bf09b'
      ctx.font = 'bold 11px Inter,sans-serif'
      ctx.textAlign = 'center'; ctx.textBaseline = 'bottom'
      ctx.fillText(l.name, px, py - 11)
    })

    // Robot (gold arrow)
    if (robotPose) {
      const [px, py] = toC(robotPose.x, robotPose.y)
      ctx.save()
      ctx.translate(px, py)
      ctx.rotate(-robotPose.theta)
      ctx.beginPath(); ctx.arc(0, 0, 11, 0, Math.PI * 2)
      ctx.fillStyle = 'rgba(226,179,92,0.22)'; ctx.fill()
      ctx.strokeStyle = '#ffd485'; ctx.lineWidth = 2; ctx.stroke()
      ctx.beginPath(); ctx.moveTo(0, -11); ctx.lineTo(-6, 5); ctx.lineTo(6, 5); ctx.closePath()
      ctx.fillStyle = '#ffd485'; ctx.fill()
      ctx.restore()
    }

    if (clickable) {
      ctx.fillStyle = 'rgba(255,255,255,0.04)'
      ctx.font = '12px Inter,sans-serif'
      ctx.textAlign = 'right'; ctx.textBaseline = 'bottom'
      ctx.fillText('Click map to place label', W - 12, H - 10)
    }
  })

  const handleClick = useCallback(e => {
    if (!clickable || !onMapClick || !offRef.current) return
    const canvas = canvasRef.current
    const rect   = canvas.getBoundingClientRect()
    const { md } = offRef.current
    const W = canvas.width, H = canvas.height
    const scale = Math.min(W / md.width, H / md.height)
    const offX  = (W - md.width  * scale) / 2
    const offY  = (H - md.height * scale) / 2

    const cssScaleX = canvas.width  / rect.width
    const cssScaleY = canvas.height / rect.height
    const cx = (e.clientX - rect.left) * cssScaleX - offX
    const cy = (e.clientY - rect.top)  * cssScaleY - offY

    const col  = cx / scale
    const row  = md.height - 1 - cy / scale
    const wx   = md.origin.x + col * md.resolution
    const wy   = md.origin.y + row * md.resolution
    onMapClick({ wx, wy })
  }, [clickable, onMapClick])

  return (
    <canvas
      ref={canvasRef}
      width={760}
      height={520}
      onClick={handleClick}
      style={{
        width: '100%', height: '100%',
        borderRadius: 16,
        cursor: clickable ? 'crosshair' : 'default',
        background: '#08060e',
        display: 'block',
      }}
    />
  )
}
