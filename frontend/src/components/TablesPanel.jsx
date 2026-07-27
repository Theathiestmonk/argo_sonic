import { useState, useEffect, useCallback } from 'react'
import MapCanvas from './MapCanvas'
import TeleopPad from './TeleopPad'

const ChevronDown = ({ open }) => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" style={{ transition: 'transform 0.25s', transform: open ? 'rotate(180deg)' : 'none' }}>
    <polyline points="4 9 12 17 20 9" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
  </svg>
)

// Table cards for the selected map — each entry is one waypoint in
// waypoints/<selectedMap>.json (same numeric-key convention waypoint_manager.py
// uses: "0" is Home/dock and is never shown here as a table).
export default function TablesPanel({ mapData, robotPose, connected, showToast, launcherUrl, selectedMap, onNavigate }) {
  const [tables, setTables]       = useState({})
  const [pending, setPending]     = useState(null)
  const [name, setName]           = useState('')
  const [editId, setEditId]       = useState(null)
  const [editName, setEditName]   = useState('')
  const [navTarget, setNavTarget] = useState(null)
  const [showDrive, setShowDrive] = useState(false)
  const [driveName, setDriveName] = useState('')

  useEffect(() => {
    fetch(`${launcherUrl}/waypoints/${selectedMap}`)
      .then(r => r.json())
      .then(d => setTables(d || {}))
      .catch(() => setTables({}))
  }, [launcherUrl, selectedMap])

  const persist = useCallback((next) => {
    setTables(next)
    fetch(`${launcherUrl}/waypoints/${selectedMap}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(next),
    }).catch(() => showToast('Could not save to robot', 'danger'))
  }, [launcherUrl, selectedMap, showToast])

  const nextKey = useCallback(() => {
    const nums = Object.keys(tables).map(Number).filter(n => !isNaN(n))
    const max = nums.length ? Math.max(...nums) : 0
    return String(Math.max(max + 1, 1)) // never reuse "0" — that's Home/dock
  }, [tables])

  const handleMapClick = useCallback(({ wx, wy }) => {
    setPending({ wx, wy })
    setName('')
  }, [])

  const saveLabel = () => {
    if (!name.trim() || !pending) return
    const key = nextKey()
    persist({ ...tables, [key]: { x: pending.wx, y: pending.wy, qz: 0, qw: 1, theta: 0, name: name.trim(), status: 'available' } })
    setPending(null); setName('')
    showToast(`"${name.trim()}" added`, 'ok')
  }

  const saveCurrentPosition = () => {
    if (!driveName.trim()) return
    if (!robotPose) { showToast('Waiting for robot position…', 'warn'); return }
    const key = nextKey()
    const theta = robotPose.theta || 0
    const qz = Math.sin(theta / 2)
    const qw = Math.cos(theta / 2)
    persist({ ...tables, [key]: { x: robotPose.x, y: robotPose.y, qz, qw, theta: theta * 180 / Math.PI, name: driveName.trim(), status: 'available' } })
    setDriveName('')
    showToast(`"${driveName.trim()}" saved at Argo's current spot`, 'ok')
  }

  const renameTable = (key) => {
    if (!editName.trim()) return
    persist({ ...tables, [key]: { ...tables[key], name: editName.trim() } })
    setEditId(null); setEditName('')
  }

  const removeTable = (key) => {
    const next = { ...tables }
    delete next[key]
    persist(next)
  }

  const toggleStatus = (key) => {
    const cur = tables[key]
    persist({ ...tables, [key]: { ...cur, status: cur.status === 'reserved' ? 'available' : 'reserved' } })
  }

  const sendAction = (key, table, verb, toastMsg) => {
    setNavTarget(`${key}:${verb}`)
    onNavigate(table.x, table.y, table.qz ?? 0, table.qw ?? 1, toastMsg)
    setTimeout(() => setNavTarget(null), 4000)
  }

  const entries = Object.entries(tables)
    .filter(([key]) => key !== '0')
    .sort((a, b) => Number(a[0]) - Number(b[0]))

  const labelsForMap = entries.map(([key, t]) => ({ name: t.name || `Table ${key}`, wx: t.x, wy: t.y }))

  return (
    <div style={{ display: 'grid', gridTemplateColumns: '300px 1fr', gap: 20, height: 'calc(100vh - 140px)', maxWidth: 1200, margin: '0 auto', animation: 'slideUp 0.35s ease' }}>

      {/* ── Left panel ── */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12, overflowY: 'auto', paddingRight: 2 }}>

        {/* Instruction */}
        <div className="glass-dense" style={{ padding: 22 }}>
          <div style={{ fontFamily: 'var(--font-heading)', fontWeight: 700, fontSize: 15, marginBottom: 8 }}>
            Tables — {selectedMap}
          </div>
          <p style={{ fontSize: 13, color: 'var(--muted)', lineHeight: 1.7 }}>
            Tap the map to add a table — or drive Argo there and save its exact spot.
          </p>
        </div>

        {/* Pending table from map click */}
        {pending && (
          <div className="glass-dense" style={{ padding: 22, borderColor: 'rgba(226,179,92,0.3)', animation: 'slideUp 0.2s ease' }}>
            <div className="label-xs" style={{ color: 'var(--gold-bright)', marginBottom: 12 }}>New table</div>
            <div style={{ fontSize: 11.5, color: 'var(--muted)', marginBottom: 10 }}>
              Position: {pending.wx.toFixed(1)} m, {pending.wy.toFixed(1)} m
            </div>
            <input
              autoFocus value={name}
              onChange={e => setName(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') saveLabel(); if (e.key === 'Escape') setPending(null) }}
              placeholder="e.g. Table 3, Window Booth"
              style={{
                width: '100%', padding: '11px 14px', borderRadius: 12,
                background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(226,179,92,0.3)',
                color: '#fff', outline: 'none', marginBottom: 12, boxSizing: 'border-box',
              }}
            />
            <div style={{ display: 'flex', gap: 8 }}>
              <button onClick={saveLabel} className="btn-ok" style={{ flex: 1, justifyContent: 'center' }}>Save table</button>
              <button onClick={() => setPending(null)} className="btn-ghost" style={{ padding: '11px 14px' }}>Cancel</button>
            </div>
          </div>
        )}

        {/* ── Drive to spot section ── */}
        <div className="glass-card" style={{ padding: 0, overflow: 'hidden' }}>
          <button
            onClick={() => setShowDrive(v => !v)}
            style={{
              width: '100%', padding: '18px 20px',
              display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12,
              background: showDrive ? 'rgba(127,168,232,0.06)' : 'transparent',
              borderBottom: showDrive ? '1px solid rgba(255,255,255,0.07)' : 'none',
              transition: 'background 0.2s',
            }}
          >
            <div style={{ textAlign: 'left' }}>
              <div style={{ fontWeight: 700, fontSize: 14, color: showDrive ? 'var(--blue)' : '#fff' }}>
                Drive Argo to a spot
              </div>
              <div style={{ fontSize: 11.5, color: 'var(--muted)', marginTop: 3 }}>
                Move Argo manually, then save its position
              </div>
            </div>
            <div style={{ color: showDrive ? 'var(--blue)' : 'var(--muted)', flexShrink: 0 }}>
              <ChevronDown open={showDrive} />
            </div>
          </button>

          {showDrive && (
            <div style={{ padding: '20px 20px 22px', animation: 'slideUp 0.2s ease' }}>
              <TeleopPad connected={connected} compact />

              <div style={{ marginTop: 20, paddingTop: 18, borderTop: '1px solid rgba(255,255,255,0.07)' }}>
                <div className="label-xs" style={{ marginBottom: 10 }}>
                  Save Argo's current spot
                  {robotPose && (
                    <span style={{ marginLeft: 8, color: 'rgba(255,255,255,0.3)', fontWeight: 500, textTransform: 'none', letterSpacing: 0, fontSize: 10.5 }}>
                      ({robotPose.x.toFixed(1)} m, {robotPose.y.toFixed(1)} m)
                    </span>
                  )}
                </div>
                <input
                  value={driveName}
                  onChange={e => setDriveName(e.target.value)}
                  onKeyDown={e => { if (e.key === 'Enter') saveCurrentPosition() }}
                  placeholder="e.g. Table 3, Charging dock"
                  style={{
                    width: '100%', padding: '11px 14px', borderRadius: 12, fontSize: 13,
                    background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(127,168,232,0.25)',
                    color: '#fff', outline: 'none', marginBottom: 10, boxSizing: 'border-box',
                  }}
                />
                <button
                  onClick={saveCurrentPosition}
                  disabled={!driveName.trim()}
                  style={{
                    width: '100%', padding: '11px 0', borderRadius: 12,
                    fontSize: 13, fontWeight: 700, justifyContent: 'center',
                    background: driveName.trim() ? 'rgba(127,168,232,0.14)' : 'rgba(255,255,255,0.03)',
                    border: `1px solid ${driveName.trim() ? 'rgba(127,168,232,0.38)' : 'rgba(255,255,255,0.06)'}`,
                    color: driveName.trim() ? 'var(--blue)' : 'var(--muted)',
                    cursor: driveName.trim() ? 'pointer' : 'not-allowed',
                    transition: 'all 0.2s',
                  }}
                >
                  Mark this spot
                </button>
                {!robotPose && (
                  <p style={{ fontSize: 11, color: 'var(--muted)', marginTop: 8, textAlign: 'center' }}>
                    Waiting for robot position…
                  </p>
                )}
              </div>
            </div>
          )}
        </div>

        {/* Table cards */}
        <div className="glass-card" style={{ padding: 22, flex: 1 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
            <div className="label-xs">Tables</div>
            <span style={{ fontSize: 11, color: 'var(--muted)', background: 'rgba(255,255,255,0.05)', padding: '3px 9px', borderRadius: 99, border: '1px solid var(--border-glass)' }}>
              {entries.length}
            </span>
          </div>

          {entries.length === 0 && (
            <div style={{ textAlign: 'center', color: 'var(--muted)', fontSize: 13.5, padding: '20px 12px', lineHeight: 1.7 }}>
              No tables saved yet for {selectedMap}.<br />
              <span style={{ color: 'rgba(255,255,255,0.4)' }}>Tap the map or drive Argo to add one.</span>
            </div>
          )}

          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {entries.map(([key, t]) => {
              const label = t.name || `Table ${key}`
              const reserved = t.status === 'reserved'
              const isTarget = navTarget?.startsWith(`${key}:`)
              return (
                <div
                  key={key}
                  className="glass-card"
                  style={{
                    padding: '14px 16px',
                    borderColor: isTarget ? 'rgba(59,240,155,0.35)' : 'var(--border-glass)',
                    background: isTarget ? 'rgba(59,240,155,0.06)' : 'rgba(255,255,255,0.02)',
                    transition: 'all 0.25s',
                  }}
                >
                  {editId === key ? (
                    <div style={{ display: 'flex', gap: 6 }}>
                      <input
                        autoFocus value={editName}
                        onChange={e => setEditName(e.target.value)}
                        onKeyDown={e => { if (e.key === 'Enter') renameTable(key); if (e.key === 'Escape') setEditId(null) }}
                        style={{
                          flex: 1, padding: '8px 11px', borderRadius: 9, fontSize: 13,
                          background: 'rgba(255,255,255,0.05)', border: '1px solid var(--border-glass)',
                          color: '#fff', outline: 'none',
                        }}
                      />
                      <button onClick={() => renameTable(key)} style={{ color: 'var(--ok)', fontWeight: 700, padding: '0 8px', fontSize: 14 }}>✓</button>
                      <button onClick={() => setEditId(null)} style={{ color: 'var(--muted)', padding: '0 8px', fontSize: 14 }}>✕</button>
                    </div>
                  ) : (
                    <>
                      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 9 }}>
                          <div style={{ width: 8, height: 8, borderRadius: '50%', background: 'var(--ok)', boxShadow: '0 0 7px var(--ok)', flexShrink: 0 }} />
                          <span style={{ fontWeight: 600, fontSize: 14 }}>{label}</span>
                        </div>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                          <button
                            onClick={() => toggleStatus(key)}
                            style={{
                              fontSize: 10.5, fontWeight: 700, padding: '3px 9px', borderRadius: 99,
                              background: reserved ? 'rgba(255,159,67,0.12)' : 'rgba(59,240,155,0.1)',
                              border: `1px solid ${reserved ? 'rgba(255,159,67,0.3)' : 'rgba(59,240,155,0.25)'}`,
                              color: reserved ? '#ff9f43' : 'var(--ok)',
                              marginRight: 4,
                            }}
                          >
                            {reserved ? 'Reserved' : 'Available'}
                          </button>
                          <button onClick={() => { setEditId(key); setEditName(label) }} style={{ padding: '4px 7px', color: 'var(--muted)', borderRadius: 6 }} title="Rename">
                            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                          </button>
                          <button onClick={() => removeTable(key)} style={{ padding: '4px 7px', color: 'var(--danger)', borderRadius: 6, opacity: 0.7 }} title="Remove">
                            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>
                          </button>
                        </div>
                      </div>

                      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                        {[
                          ['order',   'Take Order',    `Argo heading to ${label} to take the order`],
                          ['deliver', 'Deliver Order', `Argo heading to ${label} to deliver the order`],
                          ['bill',    'Give Bill',      `Argo heading to ${label} to give the bill`],
                        ].map(([verb, btnLabel, toastMsg]) => {
                          const active = navTarget === `${key}:${verb}`
                          return (
                            <button
                              key={verb}
                              onClick={() => sendAction(key, t, verb, toastMsg)}
                              style={{
                                width: '100%', padding: '9px 12px', borderRadius: 10,
                                fontSize: 12.5, fontWeight: 600, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8,
                                background: active ? 'rgba(59,240,155,0.15)' : 'rgba(226,179,92,0.08)',
                                border: `1px solid ${active ? 'rgba(59,240,155,0.35)' : 'rgba(226,179,92,0.25)'}`,
                                color: active ? 'var(--ok)' : 'var(--gold-bright)',
                                transition: 'all 0.2s',
                              }}
                            >
                              {active ? (
                                <><span style={{ width: 7, height: 7, borderRadius: '50%', background: 'var(--ok)', animation: 'pulse-dot 1s infinite', flexShrink: 0 }} />On the way…</>
                              ) : btnLabel}
                            </button>
                          )
                        })}
                      </div>
                    </>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      </div>

      {/* ── Right: map ── */}
      <div className="glass-dense" style={{ padding: 16, display: 'flex', flexDirection: 'column' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12, padding: '0 4px' }}>
          <div style={{ fontFamily: 'var(--font-heading)', fontWeight: 700, fontSize: 16 }}>Tap to add a table</div>
          <div style={{ display: 'flex', gap: 18, fontSize: 12, color: 'var(--muted)' }}>
            <span style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
              <span style={{ width: 8, height: 8, borderRadius: '50%', background: 'var(--ok)', display: 'inline-block' }} />
              Tables
            </span>
            <span style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
              <span style={{ width: 8, height: 8, borderRadius: '50%', background: 'var(--gold-bright)', display: 'inline-block' }} />
              Argo
            </span>
          </div>
        </div>
        <div style={{ flex: 1, minHeight: 0 }}>
          <MapCanvas mapData={mapData} robotPose={robotPose} frontiers={[]} labels={labelsForMap} clickable onMapClick={handleMapClick} />
        </div>
      </div>
    </div>
  )
}
