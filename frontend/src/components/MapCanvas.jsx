import { useRef, useEffect, useCallback, useState } from 'react'

// Warm black/gold occupancy-grid palette (matches the app's dashboard
// tokens — see dashboard/src/globals.css --bg/--gold) instead of a neutral
// greyscale, tuned dark to sit naturally inside the app's dark theme.
const UNK  = [26,  24,  21]
const FREE = [232, 222, 199]
const OCC  = [12,  10,  8]

// Converts world (wx,wy) to canvas pixel given map info and canvas layout.
function worldToCanvas(wx, wy, md, offX, offY, scale) {
  const col = (wx - md.origin.x) / md.resolution
  const row = md.height - 1 - (wy - md.origin.y) / md.resolution
  return [offX + col * scale, offY + row * scale]
}

export default function MapCanvas({
  mapData, robotPose, goalPose, plannedPath = [], labels = [], frontiers = [], clickable = false, onMapClick,
  // poseEstimateMode mirrors RViz's "2D Pose Estimate" tool — click-drag
  // instead of a plain click, since a pose needs a heading too, not just a
  // position. Mutually exclusive with `clickable`'s plain-click "add table"
  // behavior (see handleClick/handleMouseDown below).
  poseEstimateMode = false, onPoseEstimate,
}) {
  const canvasRef  = useRef(null)
  const offRef     = useRef(null)   // { img: ImageBitmap, md: mapData }
  const [drag, setDrag] = useState(null)   // { startWX, startWY, curWX, curWY } while dragging a pose estimate
  const [zoom, setZoom] = useState(1)   // multiplier on top of the fit-to-container base scale
  const [maximized, setMaximized] = useState(false)

  // Only one <canvas> is ever mounted (below) — maximizing swaps its actual
  // pixel resolution up, not just its CSS display size, so the popup is
  // genuinely sharper rather than a blown-up, blurrier version of the same
  // 760x520 bitmap. Same aspect ratio throughout so the fit-to-container
  // math above doesn't need to know which mode it's in.
  const canvasW = maximized ? 1520 : 760
  const canvasH = maximized ? 1040 : 520

  // Esc closes the popup, same as clicking the backdrop or the minimize button.
  useEffect(() => {
    if (!maximized) return
    const onKey = e => { if (e.key === 'Escape') setMaximized(false) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [maximized])

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

    // OffscreenCanvas is the fastest path for creating a bitmap, but isn't
    // universally available (older/embedded WebViews, some kiosk browsers) —
    // feature-detect before constructing it, not just branch on a method of
    // an instance that might never exist. A plain <canvas> works everywhere
    // that can render at all, so it's the fallback both when OffscreenCanvas
    // is entirely missing and if constructing/using it throws for any other
    // reason — without this, an unsupported browser left offRef.current
    // permanently null (map silently stuck on "Waiting for /map…" forever,
    // no matter how much map data actually arrived).
    const toBitmap = () => {
      const tmp = document.createElement('canvas')
      tmp.width = width; tmp.height = height
      tmp.getContext('2d').putImageData(imgData, 0, 0)
      offRef.current = { bmp: tmp, md: mapData }
    }
    if (typeof OffscreenCanvas === 'undefined') {
      toBitmap()
      return
    }
    try {
      const osc = new OffscreenCanvas(width, height)
      osc.getContext('2d').putImageData(imgData, 0, 0)
      if (osc.transferToImageBitmap) {
        createImageBitmap(osc)
          .then(bmp => { offRef.current = { bmp, md: mapData } })
          .catch(toBitmap)
      } else {
        toBitmap()
      }
    } catch {
      toBitmap()
    }
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
    const scale  = Math.min(W / md.width, H / md.height) * zoom
    const drawW  = md.width  * scale
    const drawH  = md.height * scale
    const offX   = (W - drawW) / 2
    const offY   = (H - drawH) / 2

    // Zoomed in, the map exceeds the canvas — clip to what's drawn rather
    // than letting it spill over the rounded corners/buttons.
    ctx.save()
    ctx.beginPath(); ctx.rect(0, 0, W, H); ctx.clip()
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

    // Planned path (gold dashed line) — ntfields_planner_node's own
    // ComputePathToPose result, republished on /plan (see App.jsx) purely
    // for this. Drawn before the robot/goal markers so they sit on top.
    if (plannedPath.length > 1) {
      ctx.save()
      ctx.setLineDash([8, 6])
      ctx.strokeStyle = '#e2b35c'
      ctx.lineWidth = 5
      ctx.lineJoin = 'round'
      ctx.lineCap = 'round'
      ctx.beginPath()
      plannedPath.forEach((p, i) => {
        const [px, py] = toC(p.x, p.y)
        if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py)
      })
      ctx.stroke()
      ctx.restore()
    }

    // Robot marker — a directional arrow (concave "chevron" tail, same
    // silhouette as RViz/Nav2's default pose arrow) so heading reads at a
    // glance instead of needing a separate indicator dot. Forward is -Y
    // before rotation, same convention the old chassis marker used.
    if (robotPose) {
      const [px, py] = toC(robotPose.x, robotPose.y)
      ctx.save()
      ctx.translate(px, py)
      ctx.rotate(-robotPose.theta)
      ctx.fillStyle = '#800000'
      ctx.strokeStyle = '#dedede'
      ctx.lineWidth = 1.5
      ctx.lineJoin = 'round'
      ctx.beginPath()
      ctx.moveTo(0, -15)    // tip — points in the direction of travel
      ctx.lineTo(9, 11)     // back-right
      ctx.lineTo(0, 5)      // concave notch at the back, makes it read as an arrow not a triangle
      ctx.lineTo(-9, 11)    // back-left
      ctx.closePath()
      ctx.fill()
      ctx.stroke()
      ctx.restore()
    }

    // Goal marker (blue) — set whenever a table action or "Go to kitchen"
    // is clicked (DashboardHome.jsx), cleared once that trip finishes. No
    // orientation, just a target point, so no rotation like the robot arrow.
    if (goalPose) {
      const [gx, gy] = toC(goalPose.x, goalPose.y)
      ctx.save()
      ctx.beginPath(); ctx.arc(gx, gy, 9, 0, Math.PI * 2)
      ctx.fillStyle = 'rgba(127,168,232,0.22)'; ctx.fill()
      ctx.strokeStyle = '#7fa8e8'; ctx.lineWidth = 2; ctx.stroke()
      ctx.beginPath(); ctx.arc(gx, gy, 3, 0, Math.PI * 2)
      ctx.fillStyle = '#7fa8e8'; ctx.fill()
      ctx.restore()
    }

    // Pose-estimate drag preview — a green arrow from where the click
    // started (position) to wherever the pointer currently is (heading),
    // same visual idea as RViz's own "2D Pose Estimate" tool.
    if (drag) {
      const [sx, sy] = toC(drag.startWX, drag.startWY)
      const [cx, cy] = toC(drag.curWX, drag.curWY)
      ctx.strokeStyle = '#3bf09b'; ctx.lineWidth = 3; ctx.lineCap = 'round'
      ctx.beginPath(); ctx.moveTo(sx, sy); ctx.lineTo(cx, cy); ctx.stroke()
      ctx.beginPath(); ctx.arc(sx, sy, 7, 0, Math.PI * 2)
      ctx.fillStyle = '#3bf09b'; ctx.fill()
    }

    // Bottom-left, not bottom-right — the zoom buttons now own that corner.
    if (clickable) {
      ctx.fillStyle = 'rgba(255,255,255,0.04)'
      ctx.font = '12px Inter,sans-serif'
      ctx.textAlign = 'left'; ctx.textBaseline = 'bottom'
      ctx.fillText('Click map to place label', 12, H - 10)
    }
    if (poseEstimateMode) {
      ctx.fillStyle = 'rgba(59,240,155,0.6)'
      ctx.font = '12px Inter,sans-serif'
      ctx.textAlign = 'left'; ctx.textBaseline = 'bottom'
      ctx.fillText('Click where Argo is, drag toward where it’s facing', 12, H - 10)
    }

    ctx.restore()
  })

  // Shared canvas-pixel → world-meters conversion — used by the plain
  // "add table" click and by the pose-estimate drag, which both need it.
  const eventToWorld = useCallback(e => {
    const canvas = canvasRef.current
    const rect   = canvas.getBoundingClientRect()
    const { md } = offRef.current
    const W = canvas.width, H = canvas.height
    const scale = Math.min(W / md.width, H / md.height) * zoom
    const offX  = (W - md.width  * scale) / 2
    const offY  = (H - md.height * scale) / 2

    const cssScaleX = canvas.width  / rect.width
    const cssScaleY = canvas.height / rect.height
    const cx = (e.clientX - rect.left) * cssScaleX - offX
    const cy = (e.clientY - rect.top)  * cssScaleY - offY

    const col = cx / scale
    const row = md.height - 1 - cy / scale
    return { wx: md.origin.x + col * md.resolution, wy: md.origin.y + row * md.resolution }
  }, [zoom])

  const handleClick = useCallback(e => {
    if (poseEstimateMode || !clickable || !onMapClick || !offRef.current) return
    onMapClick(eventToWorld(e))
  }, [clickable, onMapClick, poseEstimateMode, eventToWorld])

  const handleMouseDown = useCallback(e => {
    if (!poseEstimateMode || !offRef.current) return
    const { wx, wy } = eventToWorld(e)
    setDrag({ startWX: wx, startWY: wy, curWX: wx, curWY: wy })
  }, [poseEstimateMode, eventToWorld])

  const handleMouseMove = useCallback(e => {
    if (!poseEstimateMode || !drag || !offRef.current) return
    const { wx, wy } = eventToWorld(e)
    setDrag(d => d && { ...d, curWX: wx, curWY: wy })
  }, [poseEstimateMode, drag, eventToWorld])

  const handleMouseUp = useCallback(() => {
    if (!poseEstimateMode || !drag) return
    const { startWX, startWY, curWX, curWY } = drag
    const dx = curWX - startWX, dy = curWY - startWY
    // Too short a drag to mean anything as a heading — keep whatever
    // heading the robot already has instead of snapping it to a garbage
    // near-zero-length direction.
    const theta = Math.hypot(dx, dy) > 0.05 ? Math.atan2(dy, dx) : (robotPose?.theta ?? 0)
    onPoseEstimate?.({ wx: startWX, wy: startWY, theta })
    setDrag(null)
  }, [poseEstimateMode, drag, onPoseEstimate, robotPose])

  const ZOOM_MIN = 0.5, ZOOM_MAX = 4, ZOOM_STEP = 1.25
  const zoomIn  = useCallback(() => setZoom(z => Math.min(z * ZOOM_STEP, ZOOM_MAX)), [])
  const zoomOut = useCallback(() => setZoom(z => Math.max(z / ZOOM_STEP, ZOOM_MIN)), [])
  const zoomBtnStyle = {
    width: 30, height: 30, borderRadius: 8,
    background: 'rgba(255,255,255,0.08)', border: '1px solid rgba(255,255,255,0.16)',
    color: '#fdfbfa', fontSize: 17, fontWeight: 700, lineHeight: 1,
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    cursor: 'pointer', userSelect: 'none', backdropFilter: 'blur(8px)',
  }

  const canvasEl = (
    <canvas
      ref={canvasRef}
      width={canvasW}
      height={canvasH}
      onClick={handleClick}
      onMouseDown={handleMouseDown}
      onMouseMove={handleMouseMove}
      onMouseUp={handleMouseUp}
      onMouseLeave={() => poseEstimateMode && setDrag(null)}
      style={{
        width: '100%', height: '100%',
        borderRadius: 16,
        cursor: (clickable || poseEstimateMode) ? 'crosshair' : 'default',
        // Matches the map's own UNK (unexplored-area) gray above, rather
        // than a near-black that fought for contrast against it — the
        // letterboxed area around a non-square map, and anywhere the map
        // hasn't fit the canvas exactly, now reads as "no map here" the
        // same way unexplored cells inside the map already do, leaving
        // the actual (light) map surface as the only thing that pops.
        background: `rgb(${UNK.join(',')})`,
        display: 'block',
      }}
    />
  )

  const zoomButtons = (
    <div style={{ position: 'absolute', bottom: 10, right: 10, display: 'flex', flexDirection: 'column', gap: 6 }}>
      <button onClick={zoomIn} disabled={zoom >= ZOOM_MAX} title="Zoom in"
        style={{ ...zoomBtnStyle, opacity: zoom >= ZOOM_MAX ? 0.4 : 1, cursor: zoom >= ZOOM_MAX ? 'not-allowed' : 'pointer' }}>+</button>
      <button onClick={zoomOut} disabled={zoom <= ZOOM_MIN} title="Zoom out"
        style={{ ...zoomBtnStyle, opacity: zoom <= ZOOM_MIN ? 0.4 : 1, cursor: zoom <= ZOOM_MIN ? 'not-allowed' : 'pointer' }}>−</button>
    </div>
  )

  const maximizeBtn = (
    <button
      onClick={() => setMaximized(m => !m)}
      title={maximized ? 'Minimize map' : 'Maximize map'}
      style={{ ...zoomBtnStyle, position: 'absolute', top: 10, right: 10, fontSize: 15 }}
    >
      {maximized ? '⤡' : '⤢'}
    </button>
  )

  if (maximized) {
    return (
      <>
        {/* Inline slot stays empty (no second live canvas) while the
            popup owns the only mounted <canvas> — same ref, same draw
            effect, just a bigger backing resolution. */}
        <div style={{ position: 'relative', width: '100%', height: '100%' }} />
        <div
          onClick={() => setMaximized(false)}
          style={{
            position: 'fixed', inset: 0, zIndex: 200,
            background: 'rgba(4,3,6,0.6)', backdropFilter: 'blur(3px)',
          }}
        />
        <div
          style={{
            position: 'fixed', top: '50%', left: '50%', transform: 'translate(-50%, -50%)',
            width: '90vw', height: '85vh', zIndex: 201,
            borderRadius: 20, padding: 12, boxSizing: 'border-box',
            background: 'rgba(20,18,24,0.92)', border: '1px solid rgba(255,255,255,0.12)',
            boxShadow: '0 20px 60px rgba(0,0,0,0.5)',
          }}
        >
          <div style={{ position: 'relative', width: '100%', height: '100%' }}>
            {canvasEl}
            {zoomButtons}
            {maximizeBtn}
          </div>
        </div>
      </>
    )
  }

  return (
    <div style={{ position: 'relative', width: '100%', height: '100%' }}>
      {canvasEl}
      {zoomButtons}
      {maximizeBtn}
    </div>
  )
}
