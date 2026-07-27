import { useState, useEffect, useRef, useCallback } from 'react'

// Same minimal PGM parser as MapPreviewThumb.jsx — kept local rather than
// shared since each component here is self-contained (matches the rest of
// this codebase's convention of no shared utils module).
function parsePGM(buffer) {
  const bytes = new Uint8Array(buffer)
  let pos = 0
  const readToken = () => {
    while (pos < bytes.length) {
      const c = bytes[pos]
      if (c === 0x23) { while (pos < bytes.length && bytes[pos] !== 0x0a) pos++ }
      else if (c === 0x20 || c === 0x09 || c === 0x0a || c === 0x0d) pos++
      else break
    }
    const start = pos
    while (pos < bytes.length && ![0x20, 0x09, 0x0a, 0x0d].includes(bytes[pos])) pos++
    return String.fromCharCode(...bytes.subarray(start, pos))
  }
  if (readToken() !== 'P5') return null
  const width = parseInt(readToken(), 10)
  const height = parseInt(readToken(), 10)
  parseInt(readToken(), 10)
  pos += 1
  if (!width || !height) return null
  return { width, height, pixels: bytes.subarray(pos, pos + width * height) }
}

export default function MapWaypointViewer({ launcherUrl, mapName, showToast }) {
  const canvasRef = useRef(null)
  const bitmapRef = useRef(null) // { bmp, width, height }
  const [meta, setMeta]         = useState(null) // { resolution, origin }
  const [waypoints, setWaypoints] = useState({})
  const [selected, setSelected] = useState(null) // waypoint key
  const [editId, setEditId]     = useState(null)
  const [editName, setEditName] = useState('')
  const [status, setStatus]     = useState('loading')

  // Load PGM → offscreen bitmap
  useEffect(() => {
    let cancelled = false
    setStatus('loading')
    bitmapRef.current = null
    fetch(`${launcherUrl}/maps/${mapName}/preview`)
      .then(r => { if (!r.ok) throw new Error('not found'); return r.arrayBuffer() })
      .then(buf => {
        if (cancelled) return
        const pgm = parsePGM(buf)
        if (!pgm) { setStatus('error'); return }
        const imgData = new ImageData(pgm.width, pgm.height)
        for (let i = 0; i < pgm.width * pgm.height; i++) {
          const v = pgm.pixels[i]
          imgData.data[i * 4] = v; imgData.data[i * 4 + 1] = v; imgData.data[i * 4 + 2] = v; imgData.data[i * 4 + 3] = 255
        }
        const off = new OffscreenCanvas(pgm.width, pgm.height)
        off.getContext('2d').putImageData(imgData, 0, 0)
        bitmapRef.current = { bmp: off, width: pgm.width, height: pgm.height }
        setStatus('ok')
      })
      .catch(() => { if (!cancelled) setStatus('error') })
    return () => { cancelled = true }
  }, [launcherUrl, mapName])

  // Load meta (resolution/origin) + waypoints
  useEffect(() => {
    fetch(`${launcherUrl}/maps/${mapName}/meta`).then(r => r.json()).then(setMeta).catch(() => setMeta(null))
    fetch(`${launcherUrl}/waypoints/${mapName}`).then(r => r.json()).then(d => setWaypoints(d || {})).catch(() => setWaypoints({}))
    setSelected(null)
  }, [launcherUrl, mapName])

  const entries = Object.entries(waypoints)
    .filter(([key]) => key !== '0')
    .sort((a, b) => Number(a[0]) - Number(b[0]))

  // world (x,y meters, map frame) → canvas pixel, same convention MapCanvas.jsx uses
  const worldToCanvas = useCallback((wx, wy, W, H) => {
    const bm = bitmapRef.current
    if (!bm || !meta) return null
    const scale = Math.min(W / bm.width, H / bm.height)
    const offX = (W - bm.width * scale) / 2
    const offY = (H - bm.height * scale) / 2
    const col = (wx - meta.origin[0]) / meta.resolution
    const rowFromBottom = (wy - meta.origin[1]) / meta.resolution
    const canvasRow = bm.height - 1 - rowFromBottom
    return [offX + col * scale, offY + canvasRow * scale]
  }, [meta])

  // Draw
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    const W = canvas.width, H = canvas.height
    ctx.clearRect(0, 0, W, H)

    if (status === 'loading') {
      ctx.fillStyle = 'rgba(255,255,255,0.4)'; ctx.font = '14px Inter,sans-serif'
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle'
      ctx.fillText('Loading map…', W / 2, H / 2)
      return
    }
    if (status === 'error' || !bitmapRef.current) {
      ctx.fillStyle = 'rgba(255,255,255,0.3)'; ctx.font = '14px Inter,sans-serif'
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle'
      ctx.fillText('No map preview available', W / 2, H / 2)
      return
    }
    const bm = bitmapRef.current
    const scale = Math.min(W / bm.width, H / bm.height)
    const drawW = bm.width * scale, drawH = bm.height * scale
    const offX = (W - drawW) / 2, offY = (H - drawH) / 2
    ctx.drawImage(bm.bmp, offX, offY, drawW, drawH)

    if (!meta) return
    entries.forEach(([key, wp]) => {
      const pt = worldToCanvas(wp.x, wp.y, W, H)
      if (!pt) return
      const [px, py] = pt
      const isSel = selected === key
      ctx.beginPath(); ctx.arc(px, py, isSel ? 9 : 7, 0, Math.PI * 2)
      ctx.fillStyle = isSel ? 'rgba(226,179,92,0.3)' : 'rgba(59,240,155,0.2)'
      ctx.fill()
      ctx.strokeStyle = isSel ? '#ffd485' : '#3bf09b'
      ctx.lineWidth = 2
      ctx.stroke()
      ctx.fillStyle = isSel ? '#ffd485' : '#e8e4ee'
      ctx.font = `${isSel ? 'bold ' : ''}11px Inter,sans-serif`
      ctx.textAlign = 'center'; ctx.textBaseline = 'bottom'
      ctx.fillText(wp.name || `Table ${key}`, px, py - 10)
    })
  }, [status, meta, entries, selected, worldToCanvas])

  const handleClick = useCallback((e) => {
    const canvas = canvasRef.current
    if (!canvas || !meta) return
    const rect = canvas.getBoundingClientRect()
    const cssScaleX = canvas.width / rect.width, cssScaleY = canvas.height / rect.height
    const cx = (e.clientX - rect.left) * cssScaleX
    const cy = (e.clientY - rect.top) * cssScaleY
    let closest = null, closestDist = 16 // px hit-radius
    entries.forEach(([key, wp]) => {
      const pt = worldToCanvas(wp.x, wp.y, canvas.width, canvas.height)
      if (!pt) return
      const d = Math.hypot(pt[0] - cx, pt[1] - cy)
      if (d < closestDist) { closest = key; closestDist = d }
    })
    if (closest) setSelected(closest)
  }, [entries, meta, worldToCanvas])

  const persist = (next) => {
    setWaypoints(next)
    fetch(`${launcherUrl}/waypoints/${mapName}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(next),
    }).catch(() => showToast?.('Could not save to robot', 'danger'))
  }

  const startRename = (key, currentName) => { setEditId(key); setEditName(currentName) }
  const saveRename = (key) => {
    if (!editName.trim()) return
    persist({ ...waypoints, [key]: { ...waypoints[key], name: editName.trim() } })
    setEditId(null); setEditName('')
    showToast?.('Label updated', 'ok')
  }

  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 280px', gap: 16 }}>
      <div className="glass-dense" style={{ padding: 14 }}>
        <canvas
          ref={canvasRef}
          width={900} height={620}
          onClick={handleClick}
          style={{ width: '100%', height: 480, borderRadius: 14, background: '#08060e', display: 'block', cursor: entries.length ? 'pointer' : 'default' }}
        />
      </div>

      <div className="glass-dense" style={{ padding: 16, maxHeight: 480, overflowY: 'auto' }}>
        <div className="label-xs" style={{ marginBottom: 12 }}>Waypoints ({entries.length})</div>
        {entries.length === 0 && (
          <div style={{ fontSize: 12.5, color: 'var(--muted)', lineHeight: 1.6 }}>
            No waypoints saved for {mapName} yet.
          </div>
        )}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {entries.map(([key, wp]) => {
            const label = wp.name || `Table ${key}`
            const isSel = selected === key
            return (
              <div
                key={key}
                onClick={() => setSelected(key)}
                style={{
                  padding: '9px 11px', borderRadius: 10, cursor: 'pointer',
                  background: isSel ? 'rgba(226,179,92,0.1)' : 'rgba(255,255,255,0.02)',
                  border: `1px solid ${isSel ? 'rgba(226,179,92,0.35)' : 'var(--border-glass)'}`,
                }}
              >
                {editId === key ? (
                  <div style={{ display: 'flex', gap: 6 }} onClick={e => e.stopPropagation()}>
                    <input
                      autoFocus value={editName}
                      onChange={e => setEditName(e.target.value)}
                      onKeyDown={e => { if (e.key === 'Enter') saveRename(key); if (e.key === 'Escape') setEditId(null) }}
                      style={{ flex: 1, padding: '6px 9px', borderRadius: 8, fontSize: 12.5, background: 'rgba(255,255,255,0.05)', border: '1px solid var(--border-glass)', color: '#fff', outline: 'none' }}
                    />
                    <button onClick={() => saveRename(key)} style={{ color: 'var(--ok)', fontWeight: 700, padding: '0 6px' }}>✓</button>
                    <button onClick={() => setEditId(null)} style={{ color: 'var(--muted)', padding: '0 6px' }}>✕</button>
                  </div>
                ) : (
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <span style={{ fontSize: 13, fontWeight: 600 }}>{label}</span>
                    <button
                      onClick={e => { e.stopPropagation(); startRename(key, label) }}
                      style={{ padding: '3px 6px', color: 'var(--muted)', borderRadius: 6, flexShrink: 0 }}
                      title="Rename"
                    >
                      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                    </button>
                  </div>
                )}
                {!editId && (
                  <div style={{ fontSize: 10.5, color: 'var(--muted)', marginTop: 3 }}>{Number(wp.x).toFixed(1)} m, {Number(wp.y).toFixed(1)} m</div>
                )}
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
