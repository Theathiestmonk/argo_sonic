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
  const [movingId, setMovingId] = useState(null) // waypoint key currently being repositioned via map click
  const [status, setStatus]     = useState('loading')
  const [addingPlace, setAddingPlace]   = useState(false) // armed: next map click drops a new waypoint
  const [pendingPlace, setPendingPlace] = useState(null)  // { x, y } world coords, awaiting a name before it's saved
  const [newPlaceName, setNewPlaceName] = useState('')

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
    setEditId(null)
    setMovingId(null)
    setAddingPlace(false)
    setPendingPlace(null)
    setNewPlaceName('')
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

  // canvas pixel → world (x,y meters, map frame) — inverse of worldToCanvas, used to
  // place a waypoint at the point the user clicks while a move is armed
  const canvasToWorld = useCallback((px, py, W, H) => {
    const bm = bitmapRef.current
    if (!bm || !meta) return null
    const scale = Math.min(W / bm.width, H / bm.height)
    const offX = (W - bm.width * scale) / 2
    const offY = (H - bm.height * scale) / 2
    const col = (px - offX) / scale
    const canvasRow = (py - offY) / scale
    const rowFromBottom = bm.height - 1 - canvasRow
    return [col * meta.resolution + meta.origin[0], rowFromBottom * meta.resolution + meta.origin[1]]
  }, [meta])

  // Cancel an armed move with Escape, same as the rename input already allows
  useEffect(() => {
    if (!movingId) return
    const onKey = (e) => { if (e.key === 'Escape') setMovingId(null) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [movingId])

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
      const isMoving = movingId === key
      ctx.beginPath(); ctx.arc(px, py, isSel ? 9 : 7, 0, Math.PI * 2)
      ctx.fillStyle = isSel ? 'rgba(59,240,155,0.35)' : 'rgba(0,0,0,0.4)'
      ctx.fill()
      ctx.strokeStyle = isSel ? '#3bf09b' : '#000000'
      ctx.lineWidth = 2
      if (isMoving) ctx.setLineDash([4, 3])
      ctx.stroke()
      ctx.setLineDash([])
      ctx.fillStyle = isSel ? '#3bf09b' : '#000000'
      ctx.font = `${isSel ? 'bold ' : ''}11px Inter,sans-serif`
      ctx.textAlign = 'center'; ctx.textBaseline = 'bottom'
      ctx.fillText(wp.name || `Table ${key}`, px, py - 10)
    })

    if (pendingPlace) {
      const pt = worldToCanvas(pendingPlace.x, pendingPlace.y, W, H)
      if (pt) {
        const [px, py] = pt
        ctx.beginPath(); ctx.arc(px, py, 9, 0, Math.PI * 2)
        ctx.fillStyle = 'rgba(226,179,92,0.3)'
        ctx.fill()
        ctx.strokeStyle = '#ffd485'
        ctx.lineWidth = 2
        ctx.setLineDash([4, 3])
        ctx.stroke()
        ctx.setLineDash([])
      }
    }
  }, [status, meta, entries, selected, movingId, pendingPlace, worldToCanvas])

  const persist = (next) => {
    setWaypoints(next)
    fetch(`${launcherUrl}/waypoints/${mapName}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(next),
    }).catch(() => showToast?.('Could not save to robot', 'danger'))
  }

  const handleClick = useCallback((e) => {
    const canvas = canvasRef.current
    if (!canvas || !meta) return
    const rect = canvas.getBoundingClientRect()
    const cssScaleX = canvas.width / rect.width, cssScaleY = canvas.height / rect.height
    const cx = (e.clientX - rect.left) * cssScaleX
    const cy = (e.clientY - rect.top) * cssScaleY

    // Awaiting a name for a just-placed point — ignore further map clicks
    // until that's confirmed or cancelled, rather than silently re-arming.
    if (pendingPlace) return

    if (addingPlace) {
      const world = canvasToWorld(cx, cy, canvas.width, canvas.height)
      if (world) setPendingPlace({ x: world[0], y: world[1] })
      setAddingPlace(false)
      return
    }

    if (movingId) {
      const world = canvasToWorld(cx, cy, canvas.width, canvas.height)
      if (world) {
        persist({ ...waypoints, [movingId]: { ...waypoints[movingId], x: world[0], y: world[1] } })
        showToast?.('Location updated', 'ok')
      }
      setMovingId(null)
      return
    }

    let closest = null, closestDist = 16 // px hit-radius
    entries.forEach(([key, wp]) => {
      const pt = worldToCanvas(wp.x, wp.y, canvas.width, canvas.height)
      if (!pt) return
      const d = Math.hypot(pt[0] - cx, pt[1] - cy)
      if (d < closestDist) { closest = key; closestDist = d }
    })
    if (closest) setSelected(closest)
  }, [entries, meta, worldToCanvas, canvasToWorld, movingId, addingPlace, pendingPlace, waypoints, showToast])

  // Cancel an armed add/move with Escape, and dismiss the pending-name form too
  useEffect(() => {
    if (!addingPlace && !pendingPlace) return
    const onKey = (e) => {
      if (e.key !== 'Escape') return
      setAddingPlace(false); setPendingPlace(null); setNewPlaceName('')
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [addingPlace, pendingPlace])

  const nextKey = useCallback(() => {
    const nums = Object.keys(waypoints).map(Number).filter(n => !isNaN(n))
    const max = nums.length ? Math.max(...nums) : 0
    return String(Math.max(max + 1, 1)) // never reuse "0" — that's Home/dock
  }, [waypoints])

  const startAddPlace = () => {
    if (!meta || status !== 'ok') { showToast?.('Wait for the map to finish loading', 'warn'); return }
    setEditId(null); setMovingId(null); setSelected(null)
    setPendingPlace(null); setNewPlaceName('')
    setAddingPlace(true)
  }
  const cancelAddPlace = () => { setAddingPlace(false); setPendingPlace(null); setNewPlaceName('') }
  const saveNewPlace = () => {
    if (!newPlaceName.trim() || !pendingPlace) return
    const key = nextKey()
    persist({ ...waypoints, [key]: { x: pendingPlace.x, y: pendingPlace.y, qz: 0, qw: 1, theta: 0, name: newPlaceName.trim(), status: 'available' } })
    showToast?.(`"${newPlaceName.trim()}" added`, 'ok')
    setPendingPlace(null); setNewPlaceName('')
  }

  const removeWaypoint = (key, label) => {
    const next = { ...waypoints }
    delete next[key]
    persist(next)
    if (selected === key) setSelected(null)
    if (movingId === key) setMovingId(null)
    showToast?.(`"${label}" removed`, 'ok')
  }

  const startRename = (key, currentName) => { setMovingId(null); setAddingPlace(false); setPendingPlace(null); setEditId(key); setEditName(currentName) }
  const saveRename = (key) => {
    if (!editName.trim()) return
    persist({ ...waypoints, [key]: { ...waypoints[key], name: editName.trim() } })
    setEditId(null); setEditName('')
    showToast?.('Label updated', 'ok')
  }

  const startMove = (key) => { setEditId(null); setAddingPlace(false); setPendingPlace(null); setSelected(key); setMovingId(key) }

  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 280px', gap: 16 }}>
      <div className="glass-dense" style={{ padding: 14 }}>
        <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 10 }}>
          <button onClick={startAddPlace} style={{ color: 'var(--gold-bright)', fontSize: 13, fontWeight: 600 }}>+ Add place</button>
        </div>
        {movingId && (
          <div style={{
            marginBottom: 10, padding: '8px 12px', borderRadius: 10,
            background: 'rgba(226,179,92,0.1)', border: '1px solid rgba(226,179,92,0.3)',
            fontSize: 12.5, color: 'var(--gold-bright)',
            display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12,
          }}>
            <span>Click anywhere on the map to move "{waypoints[movingId]?.name || `Table ${movingId}`}"</span>
            <button onClick={() => setMovingId(null)} style={{ color: 'var(--muted)', fontWeight: 700, padding: '2px 8px', flexShrink: 0 }}>Cancel</button>
          </div>
        )}
        {addingPlace && (
          <div style={{
            marginBottom: 10, padding: '8px 12px', borderRadius: 10,
            background: 'rgba(226,179,92,0.1)', border: '1px solid rgba(226,179,92,0.3)',
            fontSize: 12.5, color: 'var(--gold-bright)',
            display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12,
          }}>
            <span>Click anywhere on the map to place the new location</span>
            <button onClick={cancelAddPlace} style={{ color: 'var(--muted)', fontWeight: 700, padding: '2px 8px', flexShrink: 0 }}>Cancel</button>
          </div>
        )}
        {pendingPlace && (
          <div style={{
            marginBottom: 10, padding: '8px 12px', borderRadius: 10,
            background: 'rgba(226,179,92,0.1)', border: '1px solid rgba(226,179,92,0.3)',
            display: 'flex', gap: 8, alignItems: 'center',
          }}>
            <input
              autoFocus value={newPlaceName}
              onChange={e => setNewPlaceName(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') saveNewPlace(); if (e.key === 'Escape') cancelAddPlace() }}
              placeholder="e.g. Table 6, Window Booth"
              style={{ flex: 1, padding: '6px 9px', borderRadius: 8, fontSize: 12.5, background: 'rgba(255,255,255,0.05)', border: '1px solid var(--border-glass)', color: '#fff', outline: 'none' }}
            />
            <button onClick={saveNewPlace} disabled={!newPlaceName.trim()} style={{ color: 'var(--ok)', fontWeight: 700, padding: '2px 8px', flexShrink: 0, opacity: newPlaceName.trim() ? 1 : 0.4 }}>Save</button>
            <button onClick={cancelAddPlace} style={{ color: 'var(--muted)', fontWeight: 700, padding: '2px 8px', flexShrink: 0 }}>Cancel</button>
          </div>
        )}
        <canvas
          ref={canvasRef}
          width={900} height={620}
          onClick={handleClick}
          style={{ width: '100%', height: 480, borderRadius: 14, background: '#08060e', display: 'block', cursor: (movingId || addingPlace) ? 'crosshair' : (entries.length ? 'pointer' : 'default') }}
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
                onClick={() => { setSelected(key); if (movingId && movingId !== key) setMovingId(null); if (addingPlace) setAddingPlace(false) }}
                style={{
                  padding: '9px 11px', borderRadius: 10, cursor: 'pointer',
                  background: isSel ? 'rgba(59,240,155,0.1)' : 'rgba(255,255,255,0.02)',
                  border: `1px solid ${isSel ? 'rgba(59,240,155,0.35)' : 'var(--border-glass)'}`,
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
                    <div style={{ display: 'flex', gap: 2, flexShrink: 0 }}>
                      <button
                        onClick={e => { e.stopPropagation(); startMove(key) }}
                        style={{
                          padding: '3px 6px', borderRadius: 6,
                          color: movingId === key ? 'var(--gold-bright)' : 'var(--muted)',
                          background: movingId === key ? 'rgba(226,179,92,0.15)' : 'transparent',
                        }}
                        title="Move on map — click a new spot to reposition"
                      >
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M12 2a7 7 0 0 1 7 7c0 5-7 13-7 13S5 14 5 9a7 7 0 0 1 7-7z"/><circle cx="12" cy="9" r="2.5"/></svg>
                      </button>
                      <button
                        onClick={e => { e.stopPropagation(); startRename(key, label) }}
                        style={{ padding: '3px 6px', color: 'var(--muted)', borderRadius: 6 }}
                        title="Rename"
                      >
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                      </button>
                      <button
                        onClick={e => { e.stopPropagation(); removeWaypoint(key, label) }}
                        style={{ padding: '3px 6px', color: 'var(--danger)', borderRadius: 6, opacity: 0.7 }}
                        title="Remove"
                      >
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>
                      </button>
                    </div>
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
