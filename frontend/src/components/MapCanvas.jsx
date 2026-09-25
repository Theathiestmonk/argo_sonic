import { useRef, useEffect, useCallback, useState } from 'react'

// Inverted palette: black free space, white walls
const UNK  = [20,  20,  20]
const FREE = [20,  20,  20]
const OCC  = [240, 240, 240]

// Patrol violet — deliberately not the goal's blue or the planned path's
// gold, so a running patrol is distinguishable from a one-shot trip.
const PATROL = '#b98cf5'

// Converts world (wx,wy) to canvas pixel given map info and canvas layout.
function worldToCanvas(wx, wy, md, offX, offY, scale) {
  const col = (wx - md.origin.x) / md.resolution
  const row = md.height - 1 - (wy - md.origin.y) / md.resolution
  return [offX + col * scale, offY + row * scale]
}

// Render 3D extrusions for all edges in the occupancy grid
function renderPseudo3DWalls(ctx, mapImg, md, offX, offY, scale, W, H) {
  if (!mapImg || scale < 1) return

  ctx.save()

  const wallHeight = Math.min(scale * 0.4, 15)
  const tiltAngle = 0.35

  // Create a temporary canvas to read pixel data safely
  const tempCanvas = document.createElement('canvas')
  tempCanvas.width = md.width
  tempCanvas.height = md.height
  const tempCtx = tempCanvas.getContext('2d')

  // Draw the map image at 1:1 scale to read pixel data
  tempCtx.drawImage(mapImg, 0, 0, md.width, md.height)

  try {
    const imgData = tempCtx.getImageData(0, 0, md.width, md.height)
    const data = imgData.data

    // First pass: draw side faces
    for (let row = 0; row < md.height - 1; row++) {
      for (let col = 0; col < md.width - 1; col++) {
        const idx = (row * md.width + col) * 4
        const idxBelow = ((row + 1) * md.width + col) * 4
        const idxRight = (row * md.width + col + 1) * 4

        const brightness = data[idx]
        const brightBelow = data[idxBelow]
        const brightRight = data[idxRight]

        const px = offX + col * scale
        const py = offY + row * scale

        // Horizontal edges (side faces)
        if (Math.abs(brightness - brightBelow) > 30) {
          const y = py
          const x1 = px
          const x2 = px + scale

          const isDarkToLight = brightness > brightBelow
          ctx.fillStyle = isDarkToLight ? 'rgba(220,220,220,0.45)' : 'rgba(200,200,200,0.55)'
          ctx.strokeStyle = 'rgba(255,255,255,0.6)'
          ctx.lineWidth = 0.8

          ctx.beginPath()
          ctx.moveTo(x1, y)
          ctx.lineTo(x2, y)
          ctx.lineTo(x2 + wallHeight * Math.sin(tiltAngle), y - wallHeight * Math.cos(tiltAngle))
          ctx.lineTo(x1 + wallHeight * Math.sin(tiltAngle), y - wallHeight * Math.cos(tiltAngle))
          ctx.closePath()
          ctx.fill()
          ctx.stroke()
        }

        // Vertical edges (side faces)
        if (Math.abs(brightness - brightRight) > 30) {
          const x = px + scale
          const y1 = py
          const y2 = py + scale

          const isDarkToLight = brightness > brightRight
          ctx.fillStyle = isDarkToLight ? 'rgba(210,210,210,0.5)' : 'rgba(190,190,190,0.6)'
          ctx.strokeStyle = 'rgba(255,255,255,0.65)'
          ctx.lineWidth = 0.8

          ctx.beginPath()
          ctx.moveTo(x, y1)
          ctx.lineTo(x, y2)
          ctx.lineTo(x + wallHeight * Math.sin(tiltAngle), y2 - wallHeight * Math.cos(tiltAngle))
          ctx.lineTo(x + wallHeight * Math.sin(tiltAngle), y1 - wallHeight * Math.cos(tiltAngle))
          ctx.closePath()
          ctx.fill()
          ctx.stroke()
        }
      }
    }

    // Second pass: draw top faces to fill gaps and create solid structure
    for (let row = 0; row < md.height - 1; row++) {
      for (let col = 0; col < md.width - 1; col++) {
        const idx = (row * md.width + col) * 4
        const idxBelow = ((row + 1) * md.width + col) * 4
        const idxRight = (row * md.width + col + 1) * 4
        const idxDiag = ((row + 1) * md.width + col + 1) * 4

        const brightness = data[idx]
        const brightBelow = data[idxBelow]
        const brightRight = data[idxRight]
        const brightDiag = data[idxDiag]

        const px = offX + col * scale
        const py = offY + row * scale

        // Draw top face for this cell if it has edges
        const hasHEdge = Math.abs(brightness - brightBelow) > 30
        const hasVEdge = Math.abs(brightness - brightRight) > 30

        if (hasHEdge || hasVEdge) {
          const topLeft = [px + wallHeight * Math.sin(tiltAngle), py - wallHeight * Math.cos(tiltAngle)]
          const topRight = [px + scale + wallHeight * Math.sin(tiltAngle), py - wallHeight * Math.cos(tiltAngle)]
          const topRightB = [px + scale + wallHeight * Math.sin(tiltAngle), py + scale - wallHeight * Math.cos(tiltAngle)]
          const topLeftB = [px + wallHeight * Math.sin(tiltAngle), py + scale - wallHeight * Math.cos(tiltAngle)]

          ctx.fillStyle = 'rgba(240,240,240,0.35)'
          ctx.strokeStyle = 'rgba(255,255,255,0.4)'
          ctx.lineWidth = 0.5

          ctx.beginPath()
          ctx.moveTo(topLeft[0], topLeft[1])
          ctx.lineTo(topRight[0], topRight[1])
          ctx.lineTo(topRightB[0], topRightB[1])
          ctx.lineTo(topLeftB[0], topLeftB[1])
          ctx.closePath()
          ctx.fill()
          ctx.stroke()
        }
      }
    }
  } catch (e) {
    console.log('Wall rendering: could not access image data (expected on remote images)')
  }

  ctx.restore()
}

export default function MapCanvas({
  mapData, costmapData, robotPose, goalPose, plannedPath = [], labels = [], frontiers = [], clickable = false, onMapClick,
  poseEstimateMode = false, onPoseEstimate,
  goalSetMode = false, onGoalSet,
  patrolSetMode = false, onPatrolSet,
  patrolRoute = null,
  tableMarkers = [], // Array of {key, name, x, y} for table locations
  onTableMarkerClick, // Callback when a table marker is clicked
  driveTelemetry = {}, // { speed, wheels: {l,r} }
  sensorDistances = {}, // { lidar, depth }
}) {
  const dragMode = poseEstimateMode || goalSetMode || patrolSetMode
  const canvasRef  = useRef(null)
  const offRef     = useRef(null)   // { img: ImageBitmap, md: mapData }
  const [drag, setDrag] = useState(null)   // { startWX, startWY, curWX, curWY } while dragging a pose estimate
  const [hoveredTableKey, setHoveredTableKey] = useState(null)   // Track which table is being hovered
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

  // Very conservative isolated-cell filter: only remove completely isolated speckles
  // (a cell surrounded by all opposite state). This preserves walls, corners, and geometry.
  const filterIsolatedCells = (grid, width, height) => {
    const filtered = new Uint8Array(grid)
    for (let i = 1; i < height - 1; i++) {
      for (let j = 1; j < width - 1; j++) {
        const idx = i * width + j
        const val = grid[idx]
        const isOccupied = val > 25

        // Count 3x3 neighbors with opposite state
        let oppositeCount = 0
        for (let di = -1; di <= 1; di++) {
          for (let dj = -1; dj <= 1; dj++) {
            if (di === 0 && dj === 0) continue
            const nIdx = (i + di) * width + (j + dj)
            const nVal = grid[nIdx]
            const nIsOccupied = nVal > 25
            if (nIsOccupied !== isOccupied) oppositeCount++
          }
        }

        // Only remove if ALL 8 neighbors are opposite (completely isolated speckle)
        if (oppositeCount === 8) {
          filtered[idx] = isOccupied ? 0 : 50
        }
      }
    }
    return filtered
  }

  // Rebuild the offscreen bitmap whenever the map data changes.
  useEffect(() => {
    if (!mapData) return
    const { width, height, data } = mapData

    // Apply very conservative filter: only remove completely isolated cells
    const cleaned = filterIsolatedCells(data, width, height)

    const imgData = new ImageData(width, height)

    for (let row = 0; row < height; row++) {
      const canvasRow = height - 1 - row   // flip: ROS row-0 = bottom
      for (let col = 0; col < width; col++) {
        const val = cleaned[row * width + col]
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

    // Draw map without interpolation (cleaned occupancy grid)
    ctx.imageSmoothingEnabled = false
    ctx.drawImage(bmp, offX, offY, drawW, drawH)

    // Render 3D wall extrusions (disabled for clean straight lines)
    // renderPseudo3DWalls(ctx, bmp, md, offX, offY, scale, W, H)

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

    // Patrol route — home and goal joined by a dashed line, with the leg
    // currently being driven drawn solid. Violet so it reads as its own
    // thing next to the blue one-shot goal marker and the gold planned path.
    if (patrolRoute?.home && patrolRoute?.goal) {
      const [hx, hy] = toC(patrolRoute.home.x, patrolRoute.home.y)
      const [gx2, gy2] = toC(patrolRoute.goal.x, patrolRoute.goal.y)
      ctx.save()
      ctx.strokeStyle = PATROL
      ctx.lineWidth = 3
      ctx.lineCap = 'round'
      ctx.setLineDash(patrolRoute.leg === 'dwell' ? [2, 6] : [10, 7])
      ctx.beginPath(); ctx.moveTo(hx, hy); ctx.lineTo(gx2, gy2); ctx.stroke()
      ctx.setLineDash([])

      // Home: a square, so it never reads as "another goal" at a glance.
      ctx.fillStyle = 'rgba(185,140,245,0.22)'
      ctx.fillRect(hx - 8, hy - 8, 16, 16)
      ctx.lineWidth = 2
      ctx.strokeRect(hx - 8, hy - 8, 16, 16)
      ctx.fillStyle = PATROL
      ctx.font = 'bold 10px Inter,sans-serif'
      ctx.textAlign = 'center'; ctx.textBaseline = 'bottom'
      ctx.fillText('HOME', hx, hy - 11)

      // Far end: a circle, matching the goal marker's shape vocabulary.
      ctx.beginPath(); ctx.arc(gx2, gy2, 9, 0, Math.PI * 2)
      ctx.fillStyle = 'rgba(185,140,245,0.22)'; ctx.fill()
      ctx.strokeStyle = PATROL; ctx.lineWidth = 2; ctx.stroke()
      ctx.fillStyle = PATROL
      ctx.fillText('PATROL', gx2, gy2 - 12)
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
      ctx.rotate(-robotPose.theta + Math.PI / 2)
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

    // Table/Location markers — golden numbered circles for saved waypoints
    if (tableMarkers && tableMarkers.length > 0) {
      ctx.save()
      tableMarkers.forEach(table => {
        const [tx, ty] = toC(table.x, table.y)
        const isHovered = hoveredTableKey === table.key
        const radius = isHovered ? 16 : 12
        const glowRadius = isHovered ? 20 : 14

        // Glow effect when hovered
        if (isHovered) {
          ctx.fillStyle = 'rgba(226,179,92,0.15)'
          ctx.beginPath(); ctx.arc(tx, ty, glowRadius, 0, Math.PI * 2)
          ctx.fill()
        }

        // Main circle marker - yellow
        ctx.fillStyle = 'rgba(255,215,0,0.5)'
        ctx.strokeStyle = isHovered ? '#FFD700' : 'rgba(255,215,0,0.9)'
        ctx.lineWidth = isHovered ? 3 : 2
        ctx.beginPath(); ctx.arc(tx, ty, radius, 0, Math.PI * 2)
        ctx.fill()
        ctx.stroke()

        // Table number text - inside circle only
        ctx.fillStyle = '#ffffff'
        ctx.font = isHovered ? 'bold 14px Inter,sans-serif' : 'bold 12px Inter,sans-serif'
        ctx.textAlign = 'center'
        ctx.textBaseline = 'middle'
        const tableNum = table.name.replace('Table ', '').replace('table ', '')
        ctx.fillText(tableNum, tx, ty)
      })
      ctx.restore()
    }

    // Drag preview — an arrow from where the click started (position) to
    // wherever the pointer currently is (heading), same visual idea as
    // RViz's own click-drag tools. Green for pose-estimate (matches that
    // mode's own marker color elsewhere in this app), blue for goal-set
    // (matches the goalPose marker's own blue above) so the two read as
    // related-but-distinct tools rather than identical.
    if (drag) {
      const color = patrolSetMode ? PATROL : goalSetMode ? '#7fa8e8' : '#3bf09b'
      const [sx, sy] = toC(drag.startWX, drag.startWY)
      const [cx, cy] = toC(drag.curWX, drag.curWY)
      ctx.strokeStyle = color; ctx.lineWidth = 3; ctx.lineCap = 'round'
      ctx.beginPath(); ctx.moveTo(sx, sy); ctx.lineTo(cx, cy); ctx.stroke()
      ctx.beginPath(); ctx.arc(sx, sy, 7, 0, Math.PI * 2)
      ctx.fillStyle = color; ctx.fill()
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
    if (goalSetMode) {
      ctx.fillStyle = 'rgba(127,168,232,0.7)'
      ctx.font = '12px Inter,sans-serif'
      ctx.textAlign = 'left'; ctx.textBaseline = 'bottom'
      ctx.fillText('Click where Argo should go, drag to face a direction on arrival', 12, H - 10)
    }
    if (patrolSetMode) {
      ctx.fillStyle = 'rgba(185,140,245,0.75)'
      ctx.font = '12px Inter,sans-serif'
      ctx.textAlign = 'left'; ctx.textBaseline = 'bottom'
      ctx.fillText('Click the far end of the patrol — Argo shuttles there and back from where it is now', 12, H - 10)
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
    if (dragMode || !clickable || !onMapClick || !offRef.current) return
    onMapClick(eventToWorld(e))
  }, [clickable, onMapClick, dragMode, eventToWorld])

  const handleMouseDown = useCallback(e => {
    if (!dragMode || !offRef.current) return
    const { wx, wy } = eventToWorld(e)
    setDrag({ startWX: wx, startWY: wy, curWX: wx, curWY: wy })
  }, [dragMode, eventToWorld, goalSetMode, patrolSetMode, poseEstimateMode])

  // Get table marker at position (for click and hover detection)
  const getTableAtPosition = useCallback((e) => {
    if (!offRef.current || !tableMarkers || tableMarkers.length === 0) {
      console.log('[getTableAtPosition] No data available')
      return null
    }

    const canvas = canvasRef.current
    const rect = canvas.getBoundingClientRect()
    const { md } = offRef.current
    const W = canvas.width, H = canvas.height
    const scale = Math.min(W / md.width, H / md.height) * zoom
    const offX = (W - md.width * scale) / 2
    const offY = (H - md.height * scale) / 2

    const cssScaleX = canvas.width / rect.width
    const cssScaleY = canvas.height / rect.height
    const cx = (e.clientX - rect.left) * cssScaleX
    const cy = (e.clientY - rect.top) * cssScaleY

    console.log('[getTableAtPosition] Click at canvas:', {cx, cy}, 'scale:', scale.toFixed(2), 'offset:', {offX, offY})
    console.log('[getTableAtPosition] Canvas size:', W, 'x', H, 'rect:', rect.width, 'x', rect.height)

    const distances = []
    for (const table of tableMarkers) {
      const col = (table.x - md.origin.x) / md.resolution
      const row = md.height - 1 - (table.y - md.origin.y) / md.resolution
      const tx = offX + col * scale
      const ty = offY + row * scale

      const dist = Math.sqrt((cx - tx) ** 2 + (cy - ty) ** 2)
      distances.push({ name: table.name, dist: dist.toFixed(1), tx, ty })
      console.log(`[getTableAtPosition] ${table.name}: screen (${tx.toFixed(0)}, ${ty.toFixed(0)}) dist: ${dist.toFixed(1)}px`)
      if (dist < 30) return table  // Increased threshold from 20 to 30
    }
    console.log('[getTableAtPosition] Min distance:', Math.min(...distances.map(d => parseFloat(d.dist))))
    return null
  }, [tableMarkers, zoom])

  const handleMouseMove = useCallback(e => {
    // Handle drag
    if (dragMode && drag && offRef.current) {
      const { wx, wy } = eventToWorld(e)
      setDrag(d => d && { ...d, curWX: wx, curWY: wy })
    }

    // Detect hover over table markers
    const table = getTableAtPosition(e)
    setHoveredTableKey(table ? table.key : null)
  }, [dragMode, drag, eventToWorld, getTableAtPosition])

  // Detect clicks on table markers
  const handleCanvasClick = useCallback(e => {
    console.log('[MapCanvas] Click event, tableMarkers:', tableMarkers?.length)
    // Check if clicked on a table marker first
    const table = getTableAtPosition(e)
    console.log('[MapCanvas] getTableAtPosition returned:', table)
    if (table) {
      console.log('[MapCanvas] Calling onTableMarkerClick')
      onTableMarkerClick?.(table)
      return
    }

    // Otherwise handle regular map click
    if (dragMode || !clickable || !onMapClick) return
    onMapClick(eventToWorld(e))
  }, [clickable, onMapClick, dragMode, eventToWorld, getTableAtPosition, onTableMarkerClick, tableMarkers])

  const handleMouseUp = useCallback(() => {
    if (!dragMode || !drag) return
    const { startWX, startWY, curWX, curWY } = drag
    const dx = curWX - startWX, dy = curWY - startWY
    // Too short a drag to mean anything as a heading — keep whatever
    // heading the robot already has instead of snapping it to a garbage
    // near-zero-length direction.
    const theta = Math.hypot(dx, dy) > 0.05 ? Math.atan2(dy, dx) : (robotPose?.theta ?? 0)
    if (patrolSetMode) onPatrolSet?.({ wx: startWX, wy: startWY, theta })
    else if (goalSetMode) onGoalSet?.({ wx: startWX, wy: startWY, theta })
    else onPoseEstimate?.({ wx: startWX, wy: startWY, theta })
    setDrag(null)
  }, [dragMode, goalSetMode, patrolSetMode, drag, onPoseEstimate, onGoalSet, onPatrolSet, robotPose])

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
      onClick={handleCanvasClick}
      onMouseDown={handleMouseDown}
      onMouseMove={handleMouseMove}
      onMouseUp={handleMouseUp}
      onMouseLeave={() => { dragMode && setDrag(null); setHoveredTableKey(null) }}
      style={{
        width: '100%', height: '100%',
        borderRadius: 16,
        cursor: (clickable || dragMode) ? 'crosshair' : 'default',
        // Match the FREE (empty space) color for visual consistency
        background: `rgb(${FREE.join(',')})`,
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

  const fmtDist = (v) => {
    if (v == null) return '—'
    if (!Number.isFinite(v)) return 'Clear'
    return `${v.toFixed(2)}m`
  }
  const distColor = (v) => {
    if (v == null || !Number.isFinite(v)) return 'rgba(255,255,255,0.9)'
    if (v < 0.4) return '#ff4141'
    if (v < 0.8) return '#e2b35c'
    return 'rgba(59,240,155,0.9)'
  }

  const telemetryBar = (
    <div className="glass-card" style={{
      position: 'absolute', top: 12, left: 12, right: 52,
      padding: '12px 16px', maxWidth: 'calc(100% - 64px)', overflowX: 'auto',
      display: 'flex', gap: 20, alignItems: 'stretch',
    }}>
      <div style={{ display: 'flex', gap: 12 }}>
        <div style={{ minWidth: 85 }}>
          <div style={{ fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 700 }}>Speed</div>
          <div style={{ fontSize: 18, fontWeight: 700, marginTop: 4, color: 'rgba(59,240,155,0.9)' }}>{(driveTelemetry?.speed || 0).toFixed(2)} m/s</div>
        </div>
        <div style={{ minWidth: 85 }}>
          <div style={{ fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 700 }}>L Wheel</div>
          <div style={{ fontSize: 18, fontWeight: 700, marginTop: 4, color: 'rgba(185,140,245,0.9)' }}>{(driveTelemetry?.wheels?.l || 0).toFixed(1)} rad/s</div>
        </div>
        <div style={{ minWidth: 85 }}>
          <div style={{ fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 700 }}>R Wheel</div>
          <div style={{ fontSize: 18, fontWeight: 700, marginTop: 4, color: 'rgba(185,140,245,0.9)' }}>{(driveTelemetry?.wheels?.r || 0).toFixed(1)} rad/s</div>
        </div>
      </div>

      <div style={{ width: 1, background: 'var(--border-glass)' }} />

      <div style={{ display: 'flex', gap: 12 }}>
        <div style={{ minWidth: 85 }}>
          <div style={{ fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 700 }}>Lidar</div>
          <div style={{ fontSize: 18, fontWeight: 700, marginTop: 4, color: distColor(sensorDistances?.lidar) }}>{fmtDist(sensorDistances?.lidar)}</div>
        </div>
        <div style={{ minWidth: 85 }}>
          <div style={{ fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 700 }}>Depth</div>
          <div style={{ fontSize: 18, fontWeight: 700, marginTop: 4, color: distColor(sensorDistances?.depth) }}>{fmtDist(sensorDistances?.depth)}</div>
        </div>
        <div style={{ minWidth: 85 }}>
          <div style={{ fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 700 }}>US FL</div>
          <div style={{ fontSize: 18, fontWeight: 700, marginTop: 4, color: distColor(sensorDistances?.usFrontLeft) }}>{fmtDist(sensorDistances?.usFrontLeft)}</div>
        </div>
        <div style={{ minWidth: 85 }}>
          <div style={{ fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 700 }}>US FR</div>
          <div style={{ fontSize: 18, fontWeight: 700, marginTop: 4, color: distColor(sensorDistances?.usFrontRight) }}>{fmtDist(sensorDistances?.usFrontRight)}</div>
        </div>
      </div>
    </div>
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
            {telemetryBar}
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
      {telemetryBar}
      {zoomButtons}
      {maximizeBtn}
    </div>
  )
}
