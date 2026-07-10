import { useState, useEffect, useRef, useCallback } from 'react'
import { ros } from './ros'
import EnvironmentSelector from './components/EnvironmentSelector'
import ExplorationPanel    from './components/ExplorationPanel'
import LabelNavPanel       from './components/LabelNavPanel'

const STEPS = [
  { label: 'Choose Space',  short: 'Space' },
  { label: 'Map the Area',  short: 'Map'   },
  { label: 'Set Locations', short: 'Places' },
]

function launcherUrl(rosUrl) {
  // derive launcher HTTP URL from rosbridge ws URL
  // ws://192.168.1.5:9090  →  http://192.168.1.5:8888
  return rosUrl.replace(/^ws/, 'http').replace(/:\d+$/, ':8888')
}

export default function App() {
  const [step, setStep]           = useState(0)
  const [connected, setConnected] = useState(false)
  const [rosUrl, setRosUrl]       = useState(() => {
    // Auto-use the same host the page was served from.
    // If opened from http://192.168.1.100:3000 → ws://192.168.1.100:9090
    const host = window.location.hostname || 'localhost'
    return `ws://${host}:9090`
  })
  const [editingUrl, setEditingUrl] = useState(false)
  const [selectedEnv, setSelectedEnv] = useState(null)
  const retryRef = useRef(null)

  const [mapData, setMapData]     = useState(null)
  const [robotPose, setRobotPose] = useState(null)
  const [frontiers, setFrontiers] = useState([])
  const [labels, setLabels]       = useState(() => {
    try { return JSON.parse(localStorage.getItem('argo_labels') || '[]') } catch { return [] }
  })

  const [toast, setToast] = useState(null)
  const toastTimer = useRef(null)
  const subRefs = useRef({})

  const showToast = useCallback((msg, type = 'info') => {
    clearTimeout(toastTimer.current)
    setToast({ msg, type })
    toastTimer.current = setTimeout(() => setToast(null), 3500)
  }, [])

  const subscribe = useCallback(() => {
    if (!subRefs.current.map) {
      const t = ros.topic('/map', 'nav_msgs/OccupancyGrid', { throttle_rate: 500 })
      t?.subscribe(msg => setMapData({
        width: msg.info.width, height: msg.info.height,
        resolution: msg.info.resolution,
        origin: { x: msg.info.origin.position.x, y: msg.info.origin.position.y },
        data: msg.data,
      }))
      subRefs.current.map = t
    }
    if (!subRefs.current.odom) {
      const t = ros.topic('/odom', 'nav_msgs/Odometry', { throttle_rate: 100 })
      t?.subscribe(msg => {
        const { x, y } = msg.pose.pose.position
        const q = msg.pose.pose.orientation
        const theta = Math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))
        setRobotPose({ x, y, theta })
      })
      subRefs.current.odom = t
    }
    if (!subRefs.current.frontiers) {
      const t = ros.topic('/frontier_markers', 'visualization_msgs/MarkerArray')
      t?.subscribe(msg => {
        setFrontiers((msg.markers ?? []).filter(m => m.action === 0).map(m => ({ x: m.pose.position.x, y: m.pose.position.y })))
      })
      subRefs.current.frontiers = t
    }
  }, [])

  const connect = useCallback(() => {
    ros.connect(rosUrl)
    ros.on('connect', () => {
      clearInterval(retryRef.current)
      setConnected(true)
      showToast('Argo is connected', 'ok')
      subscribe()
    })
    ros.on('close', () => { setConnected(false) })
    ros.on('error', () => { setConnected(false) })
  }, [rosUrl, subscribe, showToast])

  // auto-retry connect every 3 s (started by ExplorationPanel after launching stack)
  const startRetrying = useCallback(() => {
    clearInterval(retryRef.current)
    retryRef.current = setInterval(() => { ros.connect(rosUrl) }, 3000)
  }, [rosUrl])

  const stopRetrying = useCallback(() => {
    clearInterval(retryRef.current)
  }, [])

  useEffect(() => { connect(); return () => clearInterval(retryRef.current) }, [])

  useEffect(() => { localStorage.setItem('argo_labels', JSON.stringify(labels)) }, [labels])

  const addLabel = useCallback(label => {
    setLabels(prev => {
      const i = prev.findIndex(l => l.name === label.name)
      if (i >= 0) { const n = [...prev]; n[i] = label; return n }
      return [...prev, label]
    })
  }, [])

  const removeLabel = useCallback(name => setLabels(prev => prev.filter(l => l.name !== name)), [])

  const sendNavGoal = useCallback((wx, wy) => {
    ros.publish('/goal_pose', 'geometry_msgs/PoseStamped', {
      header: { frame_id: 'map', stamp: { sec: 0, nanosec: 0 } },
      pose: { position: { x: wx, y: wy, z: 0 }, orientation: { x: 0, y: 0, z: 0, w: 1 } },
    })
    showToast('Argo is on its way', 'ok')
  }, [showToast])

  const shared = { mapData, robotPose, frontiers, labels, connected, showToast, launcherUrl: launcherUrl(rosUrl), startRetrying, stopRetrying }

  return (
    <div style={{ minHeight: '100vh', background: 'var(--bg)', display: 'flex', flexDirection: 'column' }}>

      {/* Ambient blobs */}
      <div style={{ position: 'fixed', inset: 0, zIndex: -1, overflow: 'hidden', pointerEvents: 'none' }}>
        {[
          { top: '-8%',  left: '22%',  w: 600, h: 600, c: '#7b3fd4', d: '0s'  },
          { bottom: '8%', right: '8%', w: 520, h: 520, c: '#e2962a', d: '-4s' },
          { top: '42%',  left: '-4%', w: 400, h: 400, c: '#1d9962', d: '-8s'  },
        ].map((b, i) => (
          <div key={i} style={{
            position: 'absolute', borderRadius: '50%', filter: 'blur(80px)', opacity: 0.38,
            width: b.w, height: b.h, background: b.c,
            top: b.top, left: b.left, bottom: b.bottom, right: b.right,
            animation: 'float 12s infinite alternate ease-in-out',
            animationDelay: b.d,
          }} />
        ))}
      </div>

      {/* Top navigation */}
      <header style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap',
        padding: '0 28px', gap: 16, height: 68,
        background: 'rgba(10,8,14,0.55)',
        backdropFilter: 'blur(30px) saturate(180%)',
        WebkitBackdropFilter: 'blur(30px) saturate(180%)',
        borderBottom: '1px solid var(--border-glass)',
        position: 'sticky', top: 0, zIndex: 30,
      }}>
        {/* Brand */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexShrink: 0 }}>
          <div style={{
            width: 36, height: 36, borderRadius: 11,
            background: 'linear-gradient(180deg,rgba(255,255,255,0.9) 0%,rgba(255,255,255,0) 100%)',
            backgroundColor: 'var(--gold)',
            backgroundBlendMode: 'overlay',
            color: '#1a1103', fontFamily: 'var(--font-heading)', fontWeight: 800, fontSize: 17,
            display: 'grid', placeItems: 'center',
            boxShadow: '0 6px 18px rgba(226,179,92,0.28)',
          }}>A</div>
          <div>
            <div style={{ fontFamily: 'var(--font-heading)', fontWeight: 800, fontSize: 16, letterSpacing: '-0.2px' }}>
              Argo{selectedEnv ? ` · ${selectedEnv.name}` : ''}
            </div>
            <div style={{ color: 'var(--muted)', fontSize: 10.5, letterSpacing: '0.1em', textTransform: 'uppercase', fontWeight: 700 }}>
              Smart Robot Assistant
            </div>
          </div>
        </div>

        {/* Step indicator */}
        <nav style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          {STEPS.map((s, i) => (
            <div key={s.label} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
              <button
                onClick={() => i < step && setStep(i)}
                style={{
                  display: 'flex', alignItems: 'center', gap: 7,
                  padding: '7px 12px', borderRadius: 99, fontSize: 12.5, fontWeight: 600,
                  background: i === step
                    ? 'rgba(226,179,92,0.12)'
                    : i < step ? 'rgba(59,240,155,0.08)' : 'rgba(255,255,255,0.03)',
                  border: `1px solid ${i === step ? 'rgba(226,179,92,0.38)' : i < step ? 'rgba(59,240,155,0.25)' : 'var(--border-glass)'}`,
                  color: i === step ? 'var(--gold-bright)' : i < step ? 'var(--ok)' : 'var(--muted)',
                  cursor: i < step ? 'pointer' : 'default',
                  transition: 'all 0.25s',
                }}
              >
                <span style={{
                  width: 18, height: 18, borderRadius: 6, fontSize: 10, fontWeight: 800,
                  background: i === step ? 'rgba(226,179,92,0.18)' : i < step ? 'rgba(59,240,155,0.18)' : 'rgba(255,255,255,0.05)',
                  color: 'inherit', display: 'grid', placeItems: 'center', flexShrink: 0,
                }}>
                  {i < step ? '✓' : i + 1}
                </span>
                {s.label}
              </button>
              {i < STEPS.length - 1 && (
                <div style={{ width: 18, height: 1, background: 'var(--border-glass)', flexShrink: 0 }} />
              )}
            </div>
          ))}
        </nav>

        {/* Connection */}
        <div style={{ flexShrink: 0 }}>
          {editingUrl ? (
            <form
              onSubmit={e => { e.preventDefault(); connect(); setEditingUrl(false) }}
              style={{ display: 'flex', gap: 6 }}
            >
              <input
                autoFocus value={rosUrl} onChange={e => setRosUrl(e.target.value)}
                style={{
                  padding: '7px 12px', borderRadius: 9, fontSize: 12,
                  background: 'rgba(255,255,255,0.05)', border: '1px solid var(--border-glass)',
                  color: '#fff', outline: 'none', width: 180,
                }}
              />
              <button type="submit" className="btn-ok" style={{ padding: '7px 14px', fontSize: 12 }}>Connect</button>
              <button type="button" onClick={() => setEditingUrl(false)} className="btn-ghost" style={{ padding: '7px 10px', fontSize: 12 }}>✕</button>
            </form>
          ) : (
            <button
              onClick={() => setEditingUrl(true)}
              style={{
                display: 'flex', alignItems: 'center', gap: 8,
                padding: '7px 14px', borderRadius: 99, fontSize: 12.5, fontWeight: 600,
                background: connected ? 'rgba(59,240,155,0.08)' : 'rgba(255,255,255,0.03)',
                border: `1px solid ${connected ? 'rgba(59,240,155,0.28)' : 'var(--border-glass)'}`,
                color: connected ? 'var(--ok)' : 'var(--muted)',
              }}
            >
              <span style={{
                width: 7, height: 7, borderRadius: '50%',
                background: connected ? 'var(--ok)' : 'rgba(255,255,255,0.25)',
                boxShadow: connected ? '0 0 7px var(--ok)' : 'none',
                animation: connected ? 'pulse-dot 2s infinite' : 'none',
                flexShrink: 0,
              }} />
              {connected ? 'Connected' : 'Not connected'}
            </button>
          )}
        </div>
      </header>

      {/* Content */}
      <main style={{ flex: 1, padding: '28px 32px 56px' }}>
        {step === 0 && (
          <EnvironmentSelector
            onSelect={env => { setSelectedEnv(env); setStep(1) }}
            onNewMap={() => { setSelectedEnv(null); setStep(1) }}
          />
        )}
        {step === 1 && <ExplorationPanel {...shared} onDone={() => setStep(2)} />}
        {step === 2 && (
          <>
            <LabelNavPanel {...shared} onAddLabel={addLabel} onRemoveLabel={removeLabel} onNavigate={sendNavGoal} />
            {labels.length > 0 && (
              <div style={{ textAlign: 'center', marginTop: 28, animation: 'slideUp 0.4s ease' }}>
                <div style={{ color: 'var(--muted)', fontSize: 13, marginBottom: 14 }}>
                  Places saved. Ready to use the full dashboard?
                </div>
                <a
                  href="/dashboard.html"
                  style={{
                    display: 'inline-flex', alignItems: 'center', gap: 10,
                    padding: '15px 32px', borderRadius: 16, fontSize: 15, fontWeight: 800,
                    background: 'linear-gradient(180deg,#fff 0%,#f3ede2 100%)',
                    color: '#1a1103', textDecoration: 'none',
                    boxShadow: '0 12px 36px rgba(226,179,92,0.32)',
                    fontFamily: 'var(--font-heading)',
                  }}
                >
                  Open Restaurant Dashboard
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
                </a>
              </div>
            )}
          </>
        )}
      </main>

      {/* Toast */}
      {toast && (
        <div style={{
          position: 'fixed', bottom: 28, right: 28, zIndex: 100,
          background: 'rgba(12,9,18,0.92)',
          border: `1px solid ${toast.type === 'ok' ? 'rgba(59,240,155,0.3)' : toast.type === 'danger' ? 'rgba(255,94,94,0.3)' : 'rgba(226,179,92,0.2)'}`,
          borderRadius: 14, padding: '13px 20px', fontSize: 13.5, fontWeight: 600,
          backdropFilter: 'blur(20px)',
          boxShadow: '0 16px 48px rgba(0,0,0,0.6), inset 0 1px 0 rgba(255,255,255,0.08)',
          color: toast.type === 'ok' ? 'var(--ok)' : toast.type === 'danger' ? 'var(--danger)' : 'var(--gold-bright)',
          animation: 'slideUp 0.3s ease',
          maxWidth: 320,
        }}>
          {toast.msg}
        </div>
      )}
    </div>
  )
}
