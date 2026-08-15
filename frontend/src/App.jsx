import { useState, useEffect, useRef, useCallback } from 'react'
import { ros } from './ros'
import EnvironmentSelector from './components/EnvironmentSelector'
import ExplorationPanel    from './components/ExplorationPanel'
import TablesPanel         from './components/TablesPanel'
import SettingsPanel       from './components/SettingsPanel'
import DashboardHome       from './components/DashboardHome'

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
  const dashboardRef = useRef(null)
  const [step, setStep]           = useState(0)
  const [view, setView]           = useState(null) // null (loading) | 'dashboard' | 'wizard'
  const [connected, setConnected] = useState(false)
  const [battery, setBattery] = useState({ connected: false, charging: false, battery_percent: 0, estimated_remaining_hours: 0, estimated_charge_remaining_hours: 0 })
  const [rosUrl, setRosUrl]       = useState(() => {
    // Auto-use the same host the page was served from.
    // If opened from http://192.168.1.100:3000 → ws://192.168.1.100:9090
    const host = window.location.hostname || 'localhost'
    return `ws://${host}:9090`
  })
  const [editingUrl, setEditingUrl] = useState(false)
  const [selectedEnv, setSelectedEnv] = useState(null)
  const [showSettings, setShowSettings] = useState(false)
  const [selectedMap, setSelectedMap] = useState(() => localStorage.getItem('argo_selected_map') || 'office_map')
  const [navInitializing, setNavInitializing] = useState(false)
  const [navReady, setNavReady] = useState(false)
  const [navPoseSet, setNavPoseSet] = useState(false)
  const [navProgress, setNavProgress] = useState(null)   // live step-by-step message (e.g. "Starting Serial Bridge"), shown on the Start Argo button while initializing
  const retryRef = useRef(null)

  const [mapData, setMapData]     = useState(null)
  const [robotPose, setRobotPose] = useState(null)
  const [frontiers, setFrontiers] = useState([])
  const [plannedPath, setPlannedPath] = useState([])
  // Same map-frame localized pose as robotPose above (slam_toolbox's
  // /pose) but kept in a ref too, not just state — arrival-checking polls
  // this on an interval and doesn't need a re-render on every tick the way
  // the on-screen marker (robotPose state) does.
  const mapPoseRef = useRef(null)
  const arrivalWatch = useRef(null) // { intervalId, timeoutId } while awaiting arrival

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
    if (!subRefs.current.frontiers) {
      const t = ros.topic('/frontier_markers', 'visualization_msgs/MarkerArray')
      t?.subscribe(msg => {
        setFrontiers((msg.markers ?? []).filter(m => m.action === 0).map(m => ({ x: m.pose.position.x, y: m.pose.position.y })))
      })
      subRefs.current.frontiers = t
    }
    if (!subRefs.current.plan) {
      // ntfields_planner_node's ComputePathToPose result, republished as a
      // plain topic (see planner_node.py's _plan_pub) purely so it can be
      // drawn on the map — RViz's usual way of seeing "the path formed by
      // the planner", now available here too since this UI has no RViz.
      const t = ros.topic('/plan', 'nav_msgs/msg/Path', { throttle_rate: 200 })
      t?.subscribe(msg => {
        setPlannedPath((msg.poses ?? []).map(p => ({ x: p.pose.position.x, y: p.pose.position.y })))
      })
      subRefs.current.plan = t
    }
    if (!subRefs.current.pose) {
      // slam_toolbox's map-frame-corrected localized pose — deliberately NOT
      // /odom, which is raw dead-reckoning and drifts from the true
      // map-frame position over time/distance. That drift is exactly why
      // the on-screen robot marker used to end up in the wrong spot on the
      // map while RViz (which visualizes the map->base_link TF, informed by
      // this same correction) showed the right one. Drives both the
      // reactive robotPose state (marker rendering, MapCanvas.jsx) and
      // mapPoseRef (arrival-distance checks below).
      const t = ros.topic('/pose', 'geometry_msgs/msg/PoseWithCovarianceStamped', { throttle_rate: 200 })
      t?.subscribe(msg => {
        const { x, y } = msg.pose.pose.position
        const q = msg.pose.pose.orientation
        const theta = Math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))
        mapPoseRef.current = { x, y }
        setRobotPose({ x, y, theta })
      })
      subRefs.current.pose = t
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
    ros.on('close', () => { setConnected(false); subRefs.current = {} })
    ros.on('error', () => { setConnected(false); subRefs.current = {} })
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

  // Live BMS reading (GET /battery, backend/launcher.py) — drives the top
  // header's battery pill, next to the Argo Sonic brand/connection status.
  // 10s is plenty; the pack's own state doesn't change meaningfully faster.
  useEffect(() => {
    let cancelled = false
    const load = () => {
      fetch(`${launcherUrl(rosUrl)}/battery`)
        .then(r => r.json())
        .then(d => { if (!cancelled) setBattery(d) })
        .catch(() => {})
    }
    load()
    const id = setInterval(load, 10000)
    return () => { cancelled = true; clearInterval(id) }
  }, [rosUrl])

  useEffect(() => { localStorage.setItem('argo_selected_map', selectedMap) }, [selectedMap])

  // Land on the dashboard when maps already exist; first-time setup (no maps
  // yet) has nothing to show a dashboard about, so falls through to the wizard.
  useEffect(() => {
    fetch(`${launcherUrl(rosUrl)}/maps`)
      .then(r => r.json())
      .then(d => setView((d.maps ?? []).length > 0 ? 'dashboard' : 'wizard'))
      .catch(() => setView('wizard'))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // How close (meters) to the goal counts as "arrived". Nav2's own goal
  // tolerance settles somewhere near this range, not exactly on the point.
  const ARRIVAL_RADIUS = 0.5  // Increased from 0.35m for more reliable detection
  // Safety fallback if the robot never quite settles within ARRIVAL_RADIUS
  // (e.g. an obstacle forces a slightly-off final pose) — fires onArrival
  // anyway rather than waiting forever, same reasoning as every other
  // timeout added elsewhere in this system.
  const ARRIVAL_MAX_WAIT_MS = 90000

  const sendNavGoal = useCallback((wx, wy, qz = 0, qw = 1, message = 'Argo is on its way', onArrival) => {
    ros.publish('/goal_pose', 'geometry_msgs/PoseStamped', {
      header: { frame_id: 'map', stamp: { sec: 0, nanosec: 0 } },
      pose: { position: { x: wx, y: wy, z: 0 }, orientation: { x: 0, y: 0, z: qz, w: qw } },
    })
    showToast(message, 'ok')

    // Real arrival detection via the localized map-frame pose, replacing
    // the fixed-timer "assume arrived" the caller used to rely on — that
    // fired the same fake-success message whether or not Nav2 actually got
    // there, exactly the kind of gap the backend side of this session has
    // been fixing (bt_navigator/planner activation no longer silently
    // reported READY on failure either).
    if (onArrival) {
      clearInterval(arrivalWatch.current?.intervalId)
      clearTimeout(arrivalWatch.current?.timeoutId)
      const finish = () => {
        clearInterval(intervalId)
        clearTimeout(timeoutId)
        arrivalWatch.current = null
        onArrival()
      }
      const intervalId = setInterval(() => {
        const p = mapPoseRef.current
        if (p) {
          const dist = Math.hypot(p.x - wx, p.y - wy)
          console.log(`[ARRIVAL CHECK] Robot at (${p.x.toFixed(2)}, ${p.y.toFixed(2)}), Goal at (${wx.toFixed(2)}, ${wy.toFixed(2)}), Distance: ${dist.toFixed(2)}m`)
          if (dist <= ARRIVAL_RADIUS) {
            console.log(`[ARRIVAL] ✓ Robot within ${ARRIVAL_RADIUS}m of goal!`)
            finish()
          }
        }
      }, 300)
      const timeoutId = setTimeout(finish, ARRIVAL_MAX_WAIT_MS)
      arrivalWatch.current = { intervalId, timeoutId }
    }
  }, [showToast])

  // The UI equivalent of RViz's "2D Pose Estimate" tool — amcl/slam_toolbox's
  // localization mode has no idea where the robot actually is on a saved map
  // until told, and the robot is headless (no RViz/DISPLAY, see DEPLOYMENT.md
  // §4) so this is otherwise a manual SSH+RViz step every time "navigate"
  // mode restarts. Same topic, message type, and covariance RViz itself
  // publishes, so amcl/slam_toolbox-localization behave exactly as if it
  // came from RViz.
  const sendInitialPose = useCallback((wx, wy, theta) => {
    const qz = Math.sin(theta / 2)
    const qw = Math.cos(theta / 2)
    ros.publish('/initialpose', 'geometry_msgs/PoseWithCovarianceStamped', {
      header: { frame_id: 'map', stamp: { sec: 0, nanosec: 0 } },
      pose: {
        pose: { position: { x: wx, y: wy, z: 0 }, orientation: { x: 0, y: 0, z: qz, w: qw } },
        covariance: [
          0.25, 0, 0, 0, 0, 0,
          0, 0.25, 0, 0, 0, 0,
          0, 0, 0, 0, 0, 0,
          0, 0, 0, 0, 0, 0,
          0, 0, 0, 0, 0, 0,
          0, 0, 0, 0, 0, 0.06853891945200942,
        ],
      },
    })
    showToast('Robot position set — localizing…', 'info')
  }, [showToast])

  // "5h 30m" / "45m" — mirrors dashboard.py's estimate_hours_and_minutes(),
  // just formatted for display instead of returned as raw seconds. Charging
  // and discharging use different source fields (see GET /battery's doc in
  // backend/launcher.py) — same formatting either way.
  const formatHm = (hours) => {
    const totalMin = Math.round((hours || 0) * 60)
    const h = Math.floor(totalMin / 60)
    const m = totalMin % 60
    return h > 0 ? `${h}h ${m}m` : `${m}m`
  }
  // Still waiting on the first real BMS reading (backend/launcher.py's
  // run_bms_thread is scanning/connecting over Bluetooth, which can take a
  // few seconds) — show a loading spinner instead of a flat "not connected"
  // claim, since from here we can't tell "still connecting" apart from
  // "genuinely unreachable" and shouldn't imply the latter by default.
  const batteryLabel = !battery.connected ? (
    <span style={{
      display: 'inline-block', width: 10, height: 10, borderRadius: '50%',
      border: '2px solid rgba(255,255,255,0.15)', borderTopColor: 'var(--muted)',
      animation: 'spin-slow 0.8s linear infinite', verticalAlign: 'middle',
    }} />
  ) : battery.charging ? `Charging · ${formatHm(battery.estimated_charge_remaining_hours)} left`
    : formatHm(battery.estimated_remaining_hours)
  const batterySub = !battery.connected ? 'Connecting…'
    : `${Math.round(battery.battery_percent)}% battery`
  const batteryRgb = !battery.connected ? '160,160,160'   // muted grey
    : battery.charging ? '127,168,232'                     // blue
    : battery.battery_percent < 20 ? '255,65,65'           // danger red
    : battery.battery_percent < 40 ? '226,179,92'           // gold
    : '59,240,155'                                          // ok green
  const ThunderboltIcon = () => (
    <svg
      width="12" height="12" viewBox="0 0 24 24" fill="var(--blue)"
      style={{ animation: 'pulse-dot 0.9s ease-in-out infinite', flexShrink: 0 }}
    >
      <path d="M13 2 3 14h7l-1 8 10-12h-7l1-8z" />
    </svg>
  )

  const shared = { mapData, robotPose, frontiers, plannedPath, connected, showToast, launcherUrl: launcherUrl(rosUrl), startRetrying, stopRetrying }

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
        background: '#121212',
        backdropFilter: 'blur(30px) saturate(180%)',
        WebkitBackdropFilter: 'blur(30px) saturate(180%)',
        borderBottom: '1px solid var(--border-glass)',
        position: 'sticky', top: 0, zIndex: 30,
      }}>
        {/* Brand */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexShrink: 0 }}>
          <div style={{
            width: 40, height: 40, borderRadius: '50%',
            border: '2px solid var(--gold)',
            background: 'transparent',
            color: 'var(--gold)', fontFamily: 'var(--font-heading)', fontWeight: 800, fontSize: 18,
            display: 'grid', placeItems: 'center',
          }}>A</div>
          <div>
            <div style={{ fontFamily: 'var(--font-heading)', fontWeight: 800, fontSize: 16, letterSpacing: '-0.2px' }}>
              ARGO SONIC{selectedEnv ? ` · ${selectedEnv.name}` : ''}
            </div>
            <div style={{ color: connected ? 'var(--ok)' : 'var(--muted)', fontSize: 11, letterSpacing: '0.05em', fontWeight: 600, marginTop: 3 }}>
              {connected ? 'Connected' : 'Not connected'}
            </div>
          </div>
        </div>

        {/* Battery — moved here from the dashboard's left rail so it's
            visible from every view, not just the dashboard. */}
        <div style={{
          display: 'flex', alignItems: 'center', gap: 8,
          padding: '6px 12px', borderRadius: 99, flexShrink: 0,
          background: `rgba(${batteryRgb},0.14)`,
          border: `1px solid rgba(${batteryRgb},0.35)`,
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 12.5, fontWeight: 700, color: '#fff' }}>
            {battery.connected && battery.charging && <ThunderboltIcon />}
            {batteryLabel}
          </div>
          <div style={{ fontSize: 10.5, color: 'rgba(255,255,255,0.65)' }}>{batterySub}</div>
        </div>

        {/* Step indicator — only relevant while running the map/table wizard */}
        {view === 'wizard' && !showSettings && (
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
        )}

        {/* Connection Edit — hidden but accessible for rosbridge URL changes */}
        {editingUrl && (
        <div style={{ flexShrink: 0 }}>
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
        </div>
        )}

        {/* Right-aligned buttons */}
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', marginLeft: 'auto' }}>
          {/* Activity Bell */}
          <button
            onClick={() => dashboardRef.current?.toggleActivityPanel?.()}
            title="View recent activity and alerts"
            style={{
              display: 'flex', alignItems: 'center', gap: 8,
              padding: '7px 14px', borderRadius: 99, fontSize: 12.5, fontWeight: 600,
              background: 'rgba(226,179,92,0.08)', border: '1px solid rgba(226,179,92,0.28)',
              color: 'var(--gold-bright)', flexShrink: 0,
            }}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/>
              <path d="M13.73 21a2 2 0 0 1-3.46 0"/>
            </svg>
            Activity
          </button>

          {/* Start/Stop Argo Buttons */}
          <button
            onClick={() => { setNavInitializing(true); dashboardRef.current?.startNav?.() }}
            disabled={navInitializing}
            title={navInitializing ? (navProgress || 'Starting navigation stack...') : undefined}
            style={{
              padding: '9px 16px', borderRadius: 14, fontSize: 12.5, fontWeight: 800,
              background: navReady ? 'rgba(59,240,155,0.12)' : 'rgba(226,179,92,0.14)',
              border: navReady ? '1px solid rgba(59,240,155,0.3)' : '1px solid rgba(226,179,92,0.4)',
              color: navReady ? 'var(--ok)' : 'var(--gold-bright)',
              display: 'flex', alignItems: 'center', gap: 7, flexShrink: 0,
              opacity: navInitializing ? 0.6 : 1,
              cursor: navInitializing ? 'not-allowed' : 'pointer',
              maxWidth: 240,
            }}
          >
            {navInitializing ? (
              <>
                <span style={{ display: 'inline-block', animation: 'spin-slow 1s linear infinite', flexShrink: 0 }}>⏳</span>
                <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {navProgress || 'Starting...'}
                </span>
              </>
            ) : navReady ? (
              <>✓ Nav Ready</>
            ) : (
              <>▶ Start Argo</>
            )}
          </button>

          <button
            onClick={() => {
              setNavInitializing(false);
              setNavReady(false);
              setNavPoseSet(false);
              setNavProgress(null);
              dashboardRef.current?.stopNav?.();
              dashboardRef.current?.estop?.()
            }}
            style={{
              padding: '9px 16px', borderRadius: 14, fontSize: 12.5, fontWeight: 800,
              background: 'rgba(255,65,65,0.12)', border: '1px solid rgba(255,65,65,0.45)', color: 'var(--danger)',
              display: 'flex', alignItems: 'center', gap: 7, flexShrink: 0,
            }}
          >
            🛑 Stop Argo
          </button>
        </div>
      </header>

      {/* Content */}
      <main style={{ flex: 1, padding: '28px 32px 56px' }}>
        {view === null && (
          <div style={{ textAlign: 'center', color: 'var(--muted)', padding: '80px 0' }}>Loading…</div>
        )}

        {view !== null && showSettings && (
          <SettingsPanel
            launcherUrl={launcherUrl(rosUrl)}
            selectedMap={selectedMap}
            onSelectMap={setSelectedMap}
            onClose={() => setShowSettings(false)}
            showToast={showToast}
          />
        )}

        {view === 'dashboard' && !showSettings && (
          <DashboardHome
            ref={dashboardRef}
            launcherUrl={launcherUrl(rosUrl)}
            selectedMap={selectedMap}
            connected={connected}
            showToast={showToast}
            onNavigate={sendNavGoal}
            onSetInitialPose={sendInitialPose}
            mapData={mapData}
            robotPose={robotPose}
            plannedPath={plannedPath}
            onOpenSettings={() => setShowSettings(true)}
            onAddMap={() => { setView('wizard'); setStep(0); setSelectedEnv(null) }}
            onNavInitializing={setNavInitializing}
            onNavReady={setNavReady}
            onNavPoseSet={setNavPoseSet}
            onNavProgress={setNavProgress}
          />
        )}

        {view === 'wizard' && !showSettings && (
          <>
            {step === 0 && (
              <EnvironmentSelector
                onSelect={env => { setSelectedEnv(env); setStep(1) }}
                onNewMap={() => { setSelectedEnv(null); setStep(1) }}
              />
            )}
            {step === 1 && <ExplorationPanel {...shared} onDone={() => setStep(2)} />}
            {step === 2 && (
              <>
                <TablesPanel {...shared} selectedMap={selectedMap} onNavigate={sendNavGoal} />
                <div style={{ textAlign: 'center', marginTop: 28, animation: 'slideUp 0.4s ease' }}>
                  <button
                    onClick={() => { setView('dashboard'); setStep(0) }}
                    className="btn-primary"
                    style={{ fontSize: 15, padding: '15px 32px', display: 'inline-flex' }}
                  >
                    Done — back to Dashboard
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
                  </button>
                </div>
              </>
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
