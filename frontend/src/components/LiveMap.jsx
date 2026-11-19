import { useRef, useEffect, useState, useCallback } from 'react'

/* LiveMap — renders the *live* /map OccupancyGrid (the SLAM map the robot is navigating on) as a styled
   floor plan: tiled floor + soft shadow, charcoal walls, furniture drawn at the saved table waypoints,
   waypoint badges, the planned route and the robot. Nothing here is invented: every shape comes from the
   occupancy grid, the saved waypoints (/waypoints/<map>), the localized pose and the Nav2 plan.

   Two layers:  bake()  – the static floor/walls bitmap, rebuilt only when the grid content changes;
                draw()  – cheap per-frame dynamic layers (furniture, route, robot, goal pulse). */

const INK = '#2b2622'
const GOLD = '#c18a1c'
const BLUE = '#4f86e0'
const VIOLET = '#8b63d6'
const GREEN = '#2f9e5b'

// ───────────── morphology helpers (grids are small: ~300x100) ─────────────
function dilate(m, w, h, r) {
  const tmp = new Uint8Array(w * h), out = new Uint8Array(w * h)
  for (let y = 0; y < h; y++) {                       // horizontal pass via running window count
    let cnt = 0
    for (let x = 0; x < Math.min(r, w); x++) cnt += m[y * w + x]
    for (let x = 0; x < w; x++) {
      if (x + r < w) cnt += m[y * w + x + r]
      if (x - r - 1 >= 0) cnt -= m[y * w + x - r - 1]
      tmp[y * w + x] = cnt > 0 ? 1 : 0
    }
  }
  for (let x = 0; x < w; x++) {                       // vertical pass
    let cnt = 0
    for (let y = 0; y < Math.min(r, h); y++) cnt += tmp[y * w + x]
    for (let y = 0; y < h; y++) {
      if (y + r < h) cnt += tmp[(y + r) * w + x]
      if (y - r - 1 >= 0) cnt -= tmp[(y - r - 1) * w + x]
      out[y * w + x] = cnt > 0 ? 1 : 0
    }
  }
  return out
}
const invert = (m) => m.map(v => 1 - v)
const erode = (m, w, h, r) => invert(dilate(invert(m), w, h, r))

function maskCanvas(mask, w, h, [r, g, b]) {
  const c = document.createElement('canvas'); c.width = w; c.height = h
  const ctx = c.getContext('2d'); const img = ctx.createImageData(w, h)
  for (let i = 0; i < w * h; i++) {
    const p = i * 4
    img.data[p] = r; img.data[p + 1] = g; img.data[p + 2] = b; img.data[p + 3] = mask[i] ? 255 : 0
  }
  ctx.putImageData(img, 0, 0)
  return c
}

function tilePattern(ctx, px) {
  const t = document.createElement('canvas'); t.width = t.height = Math.max(24, Math.round(px))
  const c = t.getContext('2d'), n = t.width
  const g = c.createLinearGradient(0, 0, n, n)
  g.addColorStop(0, '#ece5d6'); g.addColorStop(1, '#e4dccb')
  c.fillStyle = g; c.fillRect(0, 0, n, n)
  c.strokeStyle = 'rgba(160,140,100,0.28)'; c.lineWidth = 2
  c.strokeRect(1, 1, n - 1, n - 1)
  c.strokeStyle = 'rgba(255,255,255,0.55)'; c.lineWidth = 1
  c.strokeRect(3, 3, n - 5, n - 5)
  return ctx.createPattern(t, 'repeat')
}

function gridKey(md) {
  let h = md.width * 73856093 ^ md.height * 19349663
  const d = md.data
  for (let i = 0; i < d.length; i++) h = (Math.imul(h, 31) + d[i] + 1) | 0
  return `${md.width}x${md.height}@${md.resolution}:${md.origin.x},${md.origin.y}:${h}`
}

export function bake(md) {
  const { width: w, height: h, data, resolution } = md
  const s = Math.max(2, Math.min(6, Math.floor(4096 / w)))     // baked px per cell
  const occ = new Uint8Array(w * h), free = new Uint8Array(w * h)
  for (let r = 0; r < h; r++) {
    const cr = h - 1 - r                                        // ROS row 0 is the bottom
    for (let c = 0; c < w; c++) {
      const v = data[r * w + c], i = cr * w + c
      if (v >= 65) occ[i] = 1
      else if (v >= 0 && v <= 25) free[i] = 1
    }
  }
  // drop sensor-noise speckle: wall fragments of fewer than 3 connected cells
  const walls = new Uint8Array(occ), seen = new Uint8Array(w * h), stack = []
  for (let i0 = 0; i0 < w * h; i0++) {
    if (!occ[i0] || seen[i0]) continue
    const comp = []; stack.push(i0); seen[i0] = 1
    while (stack.length) {
      const i = stack.pop(); comp.push(i)
      const x = i % w, y = (i / w) | 0
      for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
        const nx = x + dx, ny = y + dy
        if (nx < 0 || ny < 0 || nx >= w || ny >= h) continue
        const j = ny * w + nx
        if (occ[j] && !seen[j]) { seen[j] = 1; stack.push(j) }
      }
    }
    if (comp.length < 3) for (const i of comp) walls[i] = 0
  }
  // Floor = free space, with small unknown gaps closed so the room reads as one surface.
  let floor = erode(dilate(free, w, h, 4), w, h, 4)
  floor = dilate(erode(floor, w, h, 1), w, h, 1)                // opening: removes hairline lidar beams
  for (let i = 0; i < floor.length; i++) if (walls[i]) floor[i] = 1

  const W = w * s, H = h * s
  const B = document.createElement('canvas'); B.width = W; B.height = H
  const ctx = B.getContext('2d')
  ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = 'high'
  const supportsFilter = 'filter' in ctx

  const floorShadow = maskCanvas(floor, w, h, [70, 50, 20])
  const floorMask = maskCanvas(floor, w, h, [0, 0, 0])
  const wallC = maskCanvas(walls, w, h, [43, 38, 34])
  const wallShadow = maskCanvas(walls, w, h, [30, 20, 5])

  // soft drop shadow under the whole floor plate
  ctx.save(); if (supportsFilter) ctx.filter = `blur(${Math.round(s * 2.2)}px)`; ctx.globalAlpha = 0.30
  ctx.drawImage(floorShadow, 0, Math.round(s * 1.2), W, H); ctx.restore()

  // tiled floor (0.6 m tiles), clipped to the floor mask
  const T = document.createElement('canvas'); T.width = W; T.height = H
  const tc = T.getContext('2d'); tc.imageSmoothingEnabled = true
  if ('filter' in tc) tc.filter = `blur(${Math.max(1, s * 0.25)}px)`
  tc.drawImage(floorMask, 0, 0, W, H)
  tc.filter = 'none'
  tc.globalCompositeOperation = 'source-in'
  tc.fillStyle = tilePattern(tc, (0.6 / resolution) * s)
  tc.fillRect(0, 0, W, H)
  ctx.drawImage(T, 0, 0)

  // wall shadow cast onto the floor, then the walls themselves
  ctx.save(); if (supportsFilter) ctx.filter = `blur(${Math.round(s * 1.4)}px)`; ctx.globalAlpha = 0.28
  ctx.drawImage(wallShadow, Math.round(s * 0.5), Math.round(s * 1.1), W, H); ctx.restore()
  ctx.save(); if (supportsFilter) ctx.filter = `blur(${(s * 0.22).toFixed(2)}px)`
  ctx.drawImage(wallC, 0, 0, W, H); ctx.restore()

  return { bmp: B, md: { width: w, height: h, resolution, origin: md.origin }, key: gridKey(md) }
}

// ───────────── drawing primitives ─────────────
const rr = (ctx, x, y, w, h, r) => { ctx.beginPath(); ctx.roundRect ? ctx.roundRect(x, y, w, h, r) : ctx.rect(x, y, w, h) }

function drawTable(ctx, x, y, size) {
  const chair = size * 0.34, gap = size * 0.07
  ctx.save(); ctx.translate(x, y)
  ctx.fillStyle = 'rgba(40,25,5,0.20)'                         // floor shadow
  rr(ctx, -size / 2 + 2, -size / 2 + 4, size, size, size * 0.12); ctx.fill()
  const chairs = [[0, -1], [0, 1], [-1, 0], [1, 0]]
  chairs.forEach(([dx, dy]) => {
    const cx = dx * (size / 2 + gap + chair * 0.35), cy = dy * (size / 2 + gap + chair * 0.35)
    const horiz = dy !== 0
    const cw = horiz ? chair * 1.5 : chair, ch = horiz ? chair : chair * 1.5
    const g = ctx.createLinearGradient(cx - cw / 2, cy - ch / 2, cx + cw / 2, cy + ch / 2)
    g.addColorStop(0, '#c8934a'); g.addColorStop(1, '#a6732f')
    ctx.fillStyle = g; rr(ctx, cx - cw / 2, cy - ch / 2, cw, ch, chair * 0.35); ctx.fill()
    ctx.strokeStyle = 'rgba(80,45,5,0.35)'; ctx.lineWidth = 1; ctx.stroke()
  })
  const g = ctx.createLinearGradient(-size / 2, -size / 2, size / 2, size / 2)
  g.addColorStop(0, '#e6b878'); g.addColorStop(1, '#c58d44')
  ctx.fillStyle = g; rr(ctx, -size / 2, -size / 2, size, size, size * 0.1); ctx.fill()
  ctx.strokeStyle = 'rgba(90,55,10,0.45)'; ctx.lineWidth = 1.2; ctx.stroke()
  ctx.strokeStyle = 'rgba(255,255,255,0.35)'; rr(ctx, -size / 2 + 3, -size / 2 + 3, size - 6, size - 6, size * 0.08); ctx.stroke()
  ctx.restore()
}

function drawCounter(ctx, x, y, w, h) {
  ctx.save(); ctx.translate(x, y)
  ctx.fillStyle = 'rgba(40,25,5,0.22)'; rr(ctx, -w / 2 + 2, -h / 2 + 4, w, h, Math.min(w, h) * 0.18); ctx.fill()
  const g = ctx.createLinearGradient(0, -h / 2, 0, h / 2); g.addColorStop(0, '#c99759'); g.addColorStop(1, '#9d6c35')
  ctx.fillStyle = g; rr(ctx, -w / 2, -h / 2, w, h, Math.min(w, h) * 0.18); ctx.fill()
  ctx.strokeStyle = 'rgba(80,45,5,0.5)'; ctx.lineWidth = 1.2; ctx.stroke()
  ctx.strokeStyle = 'rgba(255,255,255,0.3)'; rr(ctx, -w / 2 + 3, -h / 2 + 3, w - 6, h - 6, Math.min(w, h) * 0.14); ctx.stroke()
  ctx.restore()
}

function drawIsland(ctx, x, y, w, h) {
  ctx.save(); ctx.translate(x, y)
  ctx.fillStyle = 'rgba(40,25,5,0.24)'; rr(ctx, -w / 2 + 3, -h / 2 + 6, w, h, Math.min(w, h) * 0.07); ctx.fill()
  const g = ctx.createLinearGradient(-w / 2, -h / 2, w / 2, h / 2); g.addColorStop(0, '#e2b87c'); g.addColorStop(1, '#bf8a49')
  ctx.fillStyle = g; rr(ctx, -w / 2, -h / 2, w, h, Math.min(w, h) * 0.07); ctx.fill()
  ctx.strokeStyle = 'rgba(80,45,5,0.5)'; ctx.lineWidth = 1.4; ctx.stroke()
  ctx.strokeStyle = 'rgba(120,75,20,0.22)'; ctx.lineWidth = 1                       // plank seams
  for (let i = 1; i < 6; i++) { ctx.beginPath(); ctx.moveTo(-w / 2 + (w * i) / 6, -h / 2 + 4); ctx.lineTo(-w / 2 + (w * i) / 6, h / 2 - 4); ctx.stroke() }
  ctx.strokeStyle = 'rgba(255,255,255,0.35)'; rr(ctx, -w / 2 + 5, -h / 2 + 5, w - 10, h - 10, Math.min(w, h) * 0.05); ctx.stroke()
  ctx.restore()
}

function drawPrinter(ctx, x, y, w, h) {
  ctx.save(); ctx.translate(x, y)
  ctx.fillStyle = 'rgba(40,25,5,0.25)'; rr(ctx, -w / 2 + 2, -h / 2 + 3, w, h, 4); ctx.fill()
  ctx.fillStyle = '#fbfaf7'; rr(ctx, -w * 0.3, -h * 0.62, w * 0.6, h * 0.5, 2); ctx.fill()          // paper out
  ctx.strokeStyle = 'rgba(0,0,0,0.25)'; ctx.lineWidth = 1; ctx.stroke()
  const g = ctx.createLinearGradient(0, -h / 2, 0, h / 2); g.addColorStop(0, '#4a443e'); g.addColorStop(1, '#2b2724')
  ctx.fillStyle = g; rr(ctx, -w / 2, -h / 2, w, h, 5); ctx.fill()
  ctx.fillStyle = 'rgba(255,255,255,0.14)'; rr(ctx, -w / 2 + 3, -h / 2 + 3, w - 6, h * 0.3, 3); ctx.fill()
  ctx.fillStyle = '#4fd37f'; ctx.beginPath(); ctx.arc(w * 0.32, h * 0.22, Math.max(1.5, h * 0.06), 0, 6.3); ctx.fill()
  ctx.restore()
}

function tag(ctx, x, y, text) {
  ctx.save(); ctx.font = '600 11.5px Inter, system-ui, sans-serif'
  const w = ctx.measureText(text).width + 14, h = 20
  ctx.fillStyle = 'rgba(52,44,36,0.86)'; rr(ctx, x - w / 2, y - h / 2, w, h, 7); ctx.fill()
  ctx.fillStyle = '#fff'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.fillText(text, x, y + 0.5); ctx.restore()
}

function badge(ctx, x, y, text, { active = false, ring = false, r = 14, pulse = 0 } = {}) {
  ctx.save()
  if (active) {
    const g = r * (0.55 + 0.35 * pulse)
    ctx.fillStyle = `rgba(193,138,28,${0.24 - 0.1 * pulse})`
    ctx.beginPath(); ctx.arc(x, y, r + g, 0, 6.3); ctx.fill()
  }
  ctx.shadowColor = 'rgba(40,25,5,0.35)'; ctx.shadowBlur = 6; ctx.shadowOffsetY = 2
  ctx.fillStyle = active ? GOLD : '#8d8a85'
  ctx.beginPath(); ctx.arc(x, y, r, 0, 6.3); ctx.fill()
  ctx.shadowColor = 'transparent'
  ctx.lineWidth = ring || active ? 2.5 : 2; ctx.strokeStyle = active ? '#fff3cf' : ring ? '#e0aa3a' : '#f2f0ec'
  ctx.stroke()
  ctx.fillStyle = '#fff'; ctx.font = `700 ${Math.round(r * 0.84)}px Inter, system-ui, sans-serif`
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle'
  ctx.fillText(text, x, y + 0.5)
  ctx.restore()
}

function pill(ctx, x, y, text, { fill = GOLD, color = '#fff', pointer = true } = {}) {
  ctx.save()
  ctx.font = '700 13px Inter, system-ui, sans-serif'
  const w = ctx.measureText(text).width + 26, h = 28
  ctx.shadowColor = 'rgba(40,25,5,0.35)'; ctx.shadowBlur = 8; ctx.shadowOffsetY = 3
  ctx.fillStyle = fill; rr(ctx, x - w / 2, y - h / 2, w, h, 9); ctx.fill()
  if (pointer) { ctx.beginPath(); ctx.moveTo(x - 6, y + h / 2 - 1); ctx.lineTo(x + 6, y + h / 2 - 1); ctx.lineTo(x, y + h / 2 + 7); ctx.closePath(); ctx.fill() }
  ctx.shadowColor = 'transparent'; ctx.strokeStyle = 'rgba(255,255,255,0.6)'; ctx.lineWidth = 1.5; rr(ctx, x - w / 2, y - h / 2, w, h, 9); ctx.stroke()
  ctx.fillStyle = color; ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.fillText(text, x, y + 0.5)
  ctx.restore()
}

function drawRobot(ctx, x, y, theta, px, pulse) {
  const r = Math.max(13, px)
  ctx.save(); ctx.translate(x, y)
  ctx.fillStyle = `rgba(193,138,28,${0.20 - 0.10 * pulse})`
  ctx.beginPath(); ctx.arc(0, 0, r * (2.0 + 0.5 * pulse), 0, 6.3); ctx.fill()
  ctx.rotate(-theta)                                           // canvas y is down; ROS yaw is CCW
  ctx.shadowColor = 'rgba(30,20,5,0.45)'; ctx.shadowBlur = 8; ctx.shadowOffsetY = 3
  const g = ctx.createLinearGradient(-r, -r, r, r); g.addColorStop(0, '#ffffff'); g.addColorStop(1, '#dcd8d0')
  ctx.fillStyle = g; rr(ctx, -r * 1.05, -r * 0.8, r * 2.1, r * 1.6, r * 0.8); ctx.fill()
  ctx.shadowColor = 'transparent'; ctx.strokeStyle = 'rgba(60,45,20,0.4)'; ctx.lineWidth = 1.2; ctx.stroke()
  ctx.fillStyle = '#1d1a17'; rr(ctx, r * 0.25, -r * 0.5, r * 0.62, r * 1.0, r * 0.3); ctx.fill()   // sensor bar = front
  ctx.fillStyle = '#2b2622'; ctx.beginPath(); ctx.arc(-r * 0.22, 0, r * 0.32, 0, 6.3); ctx.fill()
  ctx.restore()
}

export default function LiveMap({
  mapData, robotPose, robotStale = false, goalPose, plannedPath = [], tableMarkers = [], orderTables = [], layout = null,
  poseEstimateMode = false, goalSetMode = false, patrolSetMode = false,
  onPoseEstimate, onGoalSet, onPatrolSet, onTableMarkerClick, connected = true,
}) {
  const wrapRef = useRef(null), canvasRef = useRef(null)
  const bakedRef = useRef(null)
  const viewRef = useRef({ zoom: 1, px: 0, py: 0 })
  const [zoomState, setZoomState] = useState(1)
  const dragRef = useRef(null)
  const live = useRef({})
  const dragMode = poseEstimateMode || goalSetMode || patrolSetMode
  live.current = { layout, robotPose, robotStale, goalPose, plannedPath, tableMarkers, orderTables, poseEstimateMode, goalSetMode, patrolSetMode, mapData }

  // (re)bake only when the grid content actually changed
  useEffect(() => {
    if (!mapData || !mapData.data?.length) { bakedRef.current = null; return }
    const key = gridKey(mapData)
    if (bakedRef.current?.key === key) { bakedRef.current.md.origin = mapData.origin; return }
    try { bakedRef.current = bake(mapData) } catch (e) { console.error('map bake failed', e) }
  }, [mapData])

  const geom = useCallback(() => {
    const c = canvasRef.current, b = bakedRef.current
    if (!c || !b) return null
    const cw = c.clientWidth, ch = c.clientHeight, { md } = b
    const fit = Math.min((cw - 24) / md.width, (ch - 24) / md.height)
    const v = viewRef.current, cell = fit * v.zoom
    return { cw, ch, md, cell, offX: (cw - md.width * cell) / 2 + v.px, offY: (ch - md.height * cell) / 2 + v.py }
  }, [])
  const toS = (g, wx, wy) => [g.offX + ((wx - g.md.origin.x) / g.md.resolution) * g.cell, g.offY + (g.md.height - (wy - g.md.origin.y) / g.md.resolution) * g.cell]
  const toW = (g, sx, sy) => ({ wx: g.md.origin.x + ((sx - g.offX) / g.cell) * g.md.resolution, wy: g.md.origin.y + (g.md.height - (sy - g.offY) / g.cell) * g.md.resolution })

  // ───────────── render loop ─────────────
  useEffect(() => {
    let raf, last = 0, dead = false
    const frame = (t) => {
      if (dead) return
      raf = requestAnimationFrame(frame)
      if (t - last < 40 || document.hidden) return              // ≈24 fps cap
      last = t
      const c = canvasRef.current; if (!c) return
      const dpr = window.devicePixelRatio || 1
      const cw = c.clientWidth, ch = c.clientHeight
      if (c.width !== Math.round(cw * dpr) || c.height !== Math.round(ch * dpr)) { c.width = Math.round(cw * dpr); c.height = Math.round(ch * dpr) }
      const ctx = c.getContext('2d')
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      ctx.clearRect(0, 0, cw, ch)
      const g = geom()
      const L = live.current
      if (!g) {
        ctx.fillStyle = '#9a917f'; ctx.font = '500 14px Inter, sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle'
        ctx.fillText(connected ? 'Waiting for the map from Argo…' : 'Not connected to Argo', cw / 2, ch / 2)
        return
      }
      const pulse = (Math.sin(t / 450) + 1) / 2
      ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = 'high'
      ctx.drawImage(bakedRef.current.bmp, g.offX, g.offY, g.md.width * g.cell, g.md.height * g.cell)
      const mpp = g.cell / g.md.resolution                       // screen px per metre
      const goalTable = L.goalPose && L.tableMarkers.find(m => Math.hypot(m.x - L.goalPose.x, m.y - L.goalPose.y) < 0.7) || null

      // furniture first (under route/robot). With a saved layout, objects sit where they really are in the room
      // (tables are beside the robot's stop-point waypoints, not on them); without one, tables fall back to the waypoints.
      const objs = L.layout?.objects
      const tsz = (o) => Math.max(18, Math.min((o.size || 0.75) * mpp, 64))
      if (objs) {
        objs.forEach(o => {
          const [x, y] = toS(g, o.x, o.y), w = Math.max(14, (o.w || 0.8) * mpp), h = Math.max(12, (o.h || o.w || 0.8) * mpp)
          if (o.type === 'table') drawTable(ctx, x, y, tsz(o))
          else if (o.type === 'island') drawIsland(ctx, x, y, w, h)
          else if (o.type === 'printer') drawPrinter(ctx, x, y, w, h)
          else drawCounter(ctx, x, y, w, h)
        })
      } else {
        L.tableMarkers.filter(m => /table/i.test(m.name)).forEach(m => { const [x, y] = toS(g, m.x, m.y); drawTable(ctx, x, y, Math.max(18, Math.min(0.6 * mpp, 56))) })
      }

      // planned route (dashed gold) + arrow head
      if (L.plannedPath.length > 1) {
        ctx.save(); ctx.setLineDash([9, 8]); ctx.strokeStyle = GOLD; ctx.lineWidth = Math.max(2.5, Math.min(4, mpp * 0.08)); ctx.lineCap = 'round'; ctx.lineJoin = 'round'
        ctx.shadowColor = 'rgba(255,255,255,0.8)'; ctx.shadowBlur = 3
        ctx.beginPath(); L.plannedPath.forEach((p, i) => { const [x, y] = toS(g, p.x, p.y); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y) }); ctx.stroke(); ctx.restore()
        const a = L.plannedPath[Math.max(0, L.plannedPath.length - 4)], b = L.plannedPath[L.plannedPath.length - 1]
        const [ax, ay] = toS(g, a.x, a.y), [bx, by] = toS(g, b.x, b.y)
        ctx.save(); ctx.translate(bx, by); ctx.rotate(Math.atan2(by - ay, bx - ax)); ctx.fillStyle = GOLD
        ctx.beginPath(); ctx.moveTo(9, 0); ctx.lineTo(-5, 6); ctx.lineTo(-5, -6); ctx.closePath(); ctx.fill(); ctx.restore()
      }

      // labels: table badges (T1…), tags for other furniture, waypoint pills/dots
      const orderHas = (num) => L.orderTables.includes(String(parseInt(num, 10)))
      if (objs) {
        objs.forEach(o => {
          const [x, y] = toS(g, o.x, o.y)
          if (o.type === 'table') {
            const sz = tsz(o), num = (String(o.waypoint || o.label).match(/\d+/) || [''])[0]
            badge(ctx, x, y, o.label || `T${num}`, { active: goalTable?.name === o.waypoint, ring: orderHas(num), pulse, r: Math.max(11, sz * 0.3) })
          } else tag(ctx, x, y + ((o.h || 0.8) * mpp) / 2 + 12, o.label)
        })
        L.tableMarkers.forEach(m => {
          const [x, y] = toS(g, m.x, m.y)
          if (!/table/i.test(m.name)) { pill(ctx, x, y - 22, m.name, { fill: /kitchen/i.test(m.name) ? '#a8741a' : '#5a5148' }); ctx.fillStyle = GOLD; ctx.strokeStyle = '#fff'; ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(x, y, 5, 0, 6.3); ctx.fill(); ctx.stroke() }
        })
      } else {
        L.tableMarkers.forEach(m => {
          const [x, y] = toS(g, m.x, m.y)
          const isTable = /table/i.test(m.name), num = (m.name.match(/\d+/) || [m.name])[0]
          if (isTable) { const sz = Math.max(18, Math.min(0.6 * mpp, 56)); badge(ctx, x, y, `T${num}`, { active: goalTable === m, ring: orderHas(num), pulse, r: Math.max(11, sz * 0.3) }) }
          else { pill(ctx, x, y - 22, m.name, { fill: /kitchen/i.test(m.name) ? '#a8741a' : '#5a5148' }); ctx.fillStyle = GOLD; ctx.strokeStyle = '#fff'; ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(x, y, 5, 0, 6.3); ctx.fill(); ctx.stroke() }
        })
      }

      // goal marker (when it is not on a table)
      if (L.goalPose && !(goalTable && /table/i.test(goalTable.name))) {
        const [x, y] = toS(g, L.goalPose.x, L.goalPose.y)
        ctx.save(); ctx.strokeStyle = BLUE; ctx.lineWidth = 2.5; ctx.fillStyle = `rgba(79,134,224,${0.18 + 0.1 * pulse})`
        ctx.beginPath(); ctx.arc(x, y, 12 + 4 * pulse, 0, 6.3); ctx.fill(); ctx.stroke()
        ctx.fillStyle = BLUE; ctx.beginPath(); ctx.arc(x, y, 3.5, 0, 6.3); ctx.fill(); ctx.restore()
      }

      // robot
      if (L.robotPose) {
        const [x, y] = toS(g, L.robotPose.x, L.robotPose.y)
        ctx.save(); if (L.robotStale) ctx.globalAlpha = 0.5          // last known position, not live
        drawRobot(ctx, x, y, L.robotPose.theta, Math.max(11, 0.26 * mpp), L.robotStale ? 0 : pulse); ctx.restore()
      }

      // tool drag preview
      const d = dragRef.current
      if (d?.cur) {
        const col = L.patrolSetMode ? VIOLET : L.goalSetMode ? BLUE : GREEN
        const [sx, sy] = toS(g, d.start.wx, d.start.wy), [cx, cy] = toS(g, d.cur.wx, d.cur.wy)
        ctx.strokeStyle = col; ctx.lineWidth = 3; ctx.lineCap = 'round'; ctx.beginPath(); ctx.moveTo(sx, sy); ctx.lineTo(cx, cy); ctx.stroke()
        ctx.fillStyle = col; ctx.beginPath(); ctx.arc(sx, sy, 7, 0, 6.3); ctx.fill()
      }
    }
    raf = requestAnimationFrame(frame)
    return () => { dead = true; cancelAnimationFrame(raf) }
  }, [geom, connected])

  // ───────────── interaction ─────────────
  const rectXY = (e) => { const r = canvasRef.current.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top] }
  const tableAt = (g, sx, sy) => {
    const L = live.current
    const objs = L.layout?.objects
    if (objs) {
      for (const o of objs) {
        if (o.type !== 'table') continue
        const m = L.tableMarkers.find(t => t.name === o.waypoint); if (!m) continue
        const [x, y] = toS(g, o.x, o.y)
        if (Math.hypot(sx - x, sy - y) < Math.max(28, ((o.size || 0.75) * g.cell / g.md.resolution) * 0.9)) return m
      }
    }
    for (const m of L.tableMarkers) { if (objs && /table/i.test(m.name)) continue; const [x, y] = toS(g, m.x, m.y); if (Math.hypot(sx - x, sy - y) < 30) return m }
    return null
  }
  // Small screens: map is read-only by default so the page can be scrolled with a finger; tool modes or the Interact button unlock it
  const [small, setSmall] = useState(() => typeof window !== 'undefined' && window.matchMedia('(max-width: 900px)').matches)
  const [interact, setInteract] = useState(false)
  useEffect(() => {
    const mq = window.matchMedia('(max-width: 900px)'); const f = () => setSmall(mq.matches)
    mq.addEventListener?.('change', f); return () => mq.removeEventListener?.('change', f)
  }, [])
  const locked = small && !interact && !dragMode
  const onDown = (e) => {
    if (locked) return
    const g = geom(); if (!g) return
    const [sx, sy] = rectXY(e)
    canvasRef.current.setPointerCapture?.(e.pointerId)
    if (dragMode) { const p = toW(g, sx, sy); dragRef.current = { start: p, cur: p, tool: true } }
    else dragRef.current = { pan: true, sx, sy, px: viewRef.current.px, py: viewRef.current.py, moved: false }
  }
  const onMove = (e) => {
    const d = dragRef.current, g = geom(); if (!g) return
    const [sx, sy] = rectXY(e)
    if (d?.tool) d.cur = toW(g, sx, sy)
    else if (d?.pan) {
      if (Math.hypot(sx - d.sx, sy - d.sy) > 4) d.moved = true
      if (d.moved) { viewRef.current.px = d.px + (sx - d.sx); viewRef.current.py = d.py + (sy - d.sy) }
    }
    canvasRef.current.style.cursor = dragMode ? 'crosshair' : tableAt(g, sx, sy) ? 'pointer' : d?.moved ? 'grabbing' : 'grab'
  }
  const onUp = (e) => {
    const d = dragRef.current, g = geom(); dragRef.current = null
    if (!d || !g) return
    const [sx, sy] = rectXY(e)
    if (d.tool) {
      const dx = d.cur.wx - d.start.wx, dy = d.cur.wy - d.start.wy
      const theta = Math.hypot(dx, dy) > 0.05 ? Math.atan2(dy, dx) : (live.current.robotPose?.theta ?? 0)
      const out = { wx: d.start.wx, wy: d.start.wy, theta }
      if (patrolSetMode) onPatrolSet?.(out); else if (goalSetMode) onGoalSet?.(out); else onPoseEstimate?.(out)
    } else if (!d.moved) {
      const t = tableAt(g, sx, sy); if (t) onTableMarkerClick?.(t, { x: e.clientX, y: e.clientY })
    }
  }
  useEffect(() => {                                              // non-passive wheel (zoom toward the cursor)
    const c = canvasRef.current; if (!c) return
    const wheel = (e) => {
      e.preventDefault()
      const g = geom(); if (!g) return
      const v = viewRef.current, nz = Math.max(0.6, Math.min(6, v.zoom * (e.deltaY < 0 ? 1.15 : 1 / 1.15)))
      const [sx, sy] = rectXY(e), k = nz / v.zoom
      const cx = g.cw / 2, cy = g.ch / 2
      v.px = (sx - cx) - ((sx - cx) - v.px) * k; v.py = (sy - cy) - ((sy - cy) - v.py) * k
      v.zoom = nz; setZoomState(nz)
    }
    c.addEventListener('wheel', wheel, { passive: false })
    return () => c.removeEventListener('wheel', wheel)
  }, [geom])
  const zoomBy = (k) => { const v = viewRef.current; v.zoom = Math.max(0.6, Math.min(6, v.zoom * k)); setZoomState(v.zoom) }
  const reset = () => { viewRef.current = { zoom: 1, px: 0, py: 0 }; setZoomState(1) }

  const hint = patrolSetMode ? 'Click the far end of the patrol — Argo shuttles there and back from where it is now'
    : goalSetMode ? 'Click where Argo should go, drag to choose its heading on arrival'
    : poseEstimateMode ? 'Click where Argo is standing, then drag toward where it is facing' : null

  return (
    <div ref={wrapRef} style={{ position: 'absolute', inset: 0 }}>
      <canvas ref={canvasRef}
        onPointerDown={onDown} onPointerMove={onMove} onPointerUp={onUp} onPointerLeave={() => { if (dragRef.current?.tool) dragRef.current = null }}
        style={{ width: '100%', height: '100%', display: 'block', touchAction: locked ? 'auto' : 'none', background: 'radial-gradient(ellipse at 50% 40%, #fffefb, #f7f3ea)' }} />
      {small && !dragMode && (
        <button className="a-btn a-mapinteract" onClick={() => setInteract(v => !v)}>{interact ? 'Lock map' : 'Move map'}</button>
      )}
      <div className="a-mapzoom">
        <button onClick={() => zoomBy(1.25)} title="Zoom in">+</button>
        <button onClick={() => zoomBy(0.8)} title="Zoom out">−</button>
        {zoomState !== 1 && <button onClick={reset} title="Reset view" style={{ fontSize: 13 }}>⟲</button>}
      </div>
      {hint && <div className="a-mapnote">{hint}</div>}
    </div>
  )
}
