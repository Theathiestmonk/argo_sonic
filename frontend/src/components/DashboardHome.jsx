import { useState, useEffect, useCallback, useRef } from 'react'
import { ros } from '../ros'
import RadialNav from './RadialNav'
import MapCanvas from './MapCanvas'

// Faithful React port of frontend/public/dashboard.html's layout and copy —
// same rail (Argo Status → What do you need → Where should Argo go → Send
// Argo), same stats row, same "Saved Places" grid, same Recent Activity /
// Alerts panels. The only real change from the original: places come from
// GET /waypoints/<selectedMap> (live, per-map) instead of localStorage.
// Same plain '$'-prefixed formatting TablesPanel.jsx uses for the same
// reason — no shared access to menu-data.js's currency/tax settings here.
const money = (n) => '$' + Number(n || 0).toFixed(2)

const TASKS = ['Deliver', 'Call Argo', 'Take order', 'Billing', 'Room service']

// Maps a task chip to the voice action backend/launcher.py's POST /voice/start
// expects. 'Call Argo' has no entry on purpose — it's a plain summon, not an
// order/bill/delivery/room-service interaction, so it never triggers Sonic.
const TASK_TO_ACTION = {
  'Take order':   'order',
  'Deliver':      'deliver',
  'Billing':      'bill',
  'Room service': 'room_service',
}

// Direct per-card buttons on a Saved Place — [task, button label]. task is a
// TASK_TO_ACTION key, so clicking one fires the exact same voice action the
// 3-step "What do you need? → Where should Argo go? → Confirm" rail does,
// just without needing that separate select-then-confirm detour for the
// common case of "do X at table Y" (see sendArgo's taskOverride param).
const TABLE_ACTIONS = [
  ['Take order',   'Take Order'],
  ['Deliver',      'Deliver Order'],
  ['Billing',      'Give Bill'],
  ['Room service', 'Room Service'],
]

export default function DashboardHome({ launcherUrl, selectedMap, connected, showToast, onNavigate, onSetInitialPose, mapData, robotPose, onAddMap, onOpenSettings }) {
  const [tables, setTables]         = useState({})
  const [showSetPose, setShowSetPose] = useState(false)
  const [selectedTask, setSelectedTask] = useState('Deliver')
  const [selectedDest, setSelectedDest] = useState(null)
  const [curPos, setCurPos]         = useState('Home')
  const [curStatus, setCurStatus]   = useState('Idle')
  const [taskLabel, setTaskLabel]   = useState('—')
  const [activity, setActivity]     = useState([])
  const [greeting, setGreeting]     = useState('Good day')
  const [time, setTime]             = useState('—')
  const [voiceStatus, setVoiceStatus] = useState({ running: false, action: null, map: null, table: null })
  const [orders, setOrders]         = useState({})
  const [expandedBills, setExpandedBills] = useState(() => new Set())
  const [editId, setEditId]         = useState(null)
  const [editName, setEditName]     = useState('')
  const [showAddPlace, setShowAddPlace] = useState(false)
  const [pendingPlace, setPendingPlace] = useState(null)   // { wx, wy } once the map's been clicked
  const [newPlaceName, setNewPlaceName] = useState('')

  // Nav2 + SLAM-localization stack — this is what actually lets a goal reach
  // the robot; picking a map here only decides which waypoints.json to read.
  const [navState, setNavState] = useState('unknown') // 'unknown'|'starting'|'running'|'stopped'
  const [navMap, setNavMap]     = useState(null)
  const navPollRef = useRef(null)
  const navFailRef = useRef(0)

  // navState === 'running' only means the launcher's wrapper *process* is
  // alive — start_argo_nav_ui.sh itself takes 90+ real seconds (camera wait,
  // costmap wait, several lifecycle configure/activate steps) before Nav2 can
  // actually accept a goal. Confirm that separately over rosbridge.
  //
  // Was checking /rosapi/topics for "/navigate_to_pose/_action/status" —
  // every ROS2 action implicitly exposes that as a topic, so this should
  // have worked, but live testing found it unreliable: `ros2 topic list`
  // itself inconsistently omitted that specific auto-generated topic even
  // though `ros2 action info /navigate_to_pose` reliably confirmed the
  // action server (bt_navigator) was genuinely up — a real discovery gap
  // for that topic specifically, not just this component's polling being
  // wrong. /rosapi/action_servers asks the same question ros2 action info
  // does (does this action server actually exist), which is what actually
  // matched reality in testing.
  const [navActionReady, setNavActionReady] = useState(false)
  useEffect(() => {
    // Also gated on `connected` — without a rosbridge WebSocket connection,
    // /rosapi/action_servers can never resolve, so polling here would just
    // silently do nothing forever instead of ever reflecting reality. The
    // button render below (navState==='running' && !connected) tells the
    // user the real blocker directly instead of looking like an indefinite
    // Nav2 wait.
    if (navState !== 'running' || navMap !== selectedMap || !connected) {
      setNavActionReady(false)
      return
    }
    let cancelled = false
    const check = () => {
      const svc = ros.service('/rosapi/action_servers', 'rosapi/ActionServers')
      svc?.callService({}, res => {
        if (!cancelled) setNavActionReady((res.action_servers || []).includes('/navigate_to_pose'))
      }, () => {})
    }
    check()
    const id = setInterval(check, 2000)
    return () => { cancelled = true; clearInterval(id) }
  }, [navState, navMap, selectedMap, connected])

  const navReady = navState === 'running' && navMap === selectedMap && navActionReady

  // Real step-by-step progress from sh/start_argo_nav_ui.sh itself (see
  // backend/launcher.py's GET /nav_progress) — without this, a failure
  // partway through the ~90s+ startup looked identical to it just still
  // being slow: an indefinite "Waiting for Nav2" spinner with no way to
  // tell which node broke or that it broke at all, short of SSHing in.
  const [navProgress, setNavProgress] = useState({ status: null, message: null })
  useEffect(() => {
    if (navState !== 'running' || navMap !== selectedMap) {
      setNavProgress({ status: null, message: null })
      return
    }
    let cancelled = false
    const check = () => {
      fetch(`${launcherUrl}/nav_progress`)
        .then(r => r.json())
        .then(d => { if (!cancelled) setNavProgress(d) })
        .catch(() => {})
    }
    check()
    const id = setInterval(check, 2000)
    return () => { cancelled = true; clearInterval(id) }
  }, [navState, navMap, selectedMap, launcherUrl])

  const checkNav = useCallback(() => {
    fetch(`${launcherUrl}/status`)
      .then(r => r.json())
      .then(d => {
        navFailRef.current = 0
        setNavState(d.running ? 'running' : 'stopped')
        setNavMap(d.running ? d.map : null)
      })
      .catch(() => {
        // Same debounce as ExplorationPanel's polling — one dropped request
        // shouldn't flip a genuinely-running stack to "stopped".
        navFailRef.current += 1
        if (navFailRef.current >= 3) setNavState('stopped')
      })
  }, [launcherUrl])

  useEffect(() => {
    checkNav()
    const id = setInterval(checkNav, 5000)
    return () => clearInterval(id)
  }, [checkNav])

  useEffect(() => {
    if (navState !== 'starting' && navPollRef.current) {
      clearInterval(navPollRef.current)
      navPollRef.current = null
    }
  }, [navState])

  const startNav = async () => {
    setNavState('starting')
    showToast(`Starting navigation on ${selectedMap}…`, 'info')
    try {
      await fetch(`${launcherUrl}/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: 'navigate', map: selectedMap }),
      })
      navPollRef.current = setInterval(checkNav, 3000)
    } catch {
      setNavState('stopped')
      showToast('Could not reach launcher — is launcher.py running on the robot?', 'danger')
    }
  }

  const stopNav = async () => {
    // All four status-pill states in the header (navigating / rosbridge
    // unreachable / error / still starting up) wire onClick straight to
    // this function, and read as passive status text rather than a
    // button — so a single misclick (e.g. tapping the "waiting for
    // Nav2" spinner out of impatience) used to kill the stack instantly,
    // including mid-startup before it ever had a chance to finish.
    if (!window.confirm('Stop the navigation stack now?')) return
    try {
      await fetch(`${launcherUrl}/stop`, { method: 'POST' })
      setNavState('stopped'); setNavMap(null)
      showToast('Navigation stopped', 'info')
    } catch {
      showToast('Could not reach launcher', 'danger')
    }
  }

  useEffect(() => {
    fetch(`${launcherUrl}/waypoints/${selectedMap}`)
      .then(r => r.json())
      .then(d => setTables(d || {}))
      .catch(() => setTables({}))
    setSelectedDest(null)
  }, [launcherUrl, selectedMap])

  // Full-overwrite save, same semantics as TablesPanel.jsx's own persist()
  // and waypoint_manager.py's save_waypoints() — POST /waypoints/<map>
  // always replaces the whole file, never a partial patch.
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

  const renameTable = (key) => {
    if (!editName.trim()) return
    persist({ ...tables, [key]: { ...tables[key], name: editName.trim() } })
    setEditId(null); setEditName('')
  }

  const removeTable = (key) => {
    const next = { ...tables }
    delete next[key]
    persist(next)
    if (selectedDest === (tables[key]?.name || `Table ${key}`)) setSelectedDest(null)
  }

  const saveNewPlace = () => {
    if (!newPlaceName.trim() || !pendingPlace) return
    const key = nextKey()
    persist({ ...tables, [key]: { x: pendingPlace.wx, y: pendingPlace.wy, qz: 0, qw: 1, theta: 0, name: newPlaceName.trim(), status: 'available' } })
    showToast(`"${newPlaceName.trim()}" added`, 'ok')
    setPendingPlace(null); setNewPlaceName(''); setShowAddPlace(false)
  }

  // Is Sonic currently mid-conversation at some table? Same poll pattern as
  // TablesPanel.jsx's identical hook — used to show a busy banner and avoid
  // firing a second voice session the backend would just 409 anyway.
  useEffect(() => {
    let cancelled = false
    const load = () => {
      fetch(`${launcherUrl}/voice/status`)
        .then(r => r.json())
        .then(d => { if (!cancelled) setVoiceStatus(d) })
        .catch(() => {})
    }
    load()
    const id = setInterval(load, 3000)
    return () => { cancelled = true; clearInterval(id) }
  }, [launcherUrl])

  // Orders Sonic has taken — same poll TablesPanel.jsx uses, so a finished
  // order shows up here on the Dashboard too, not only on the Tables page.
  useEffect(() => {
    let cancelled = false
    const load = () => {
      fetch(`${launcherUrl}/orders/${selectedMap}`)
        .then(r => r.json())
        .then(d => { if (!cancelled) setOrders(d || {}) })
        .catch(() => {})
    }
    load()
    const id = setInterval(load, 3000)
    return () => { cancelled = true; clearInterval(id) }
  }, [launcherUrl, selectedMap])

  const toggleBill = (key) => {
    setExpandedBills(prev => {
      const next = new Set(prev)
      next.has(key) ? next.delete(key) : next.add(key)
      return next
    })
  }

  const clearOrder = (key) => {
    // Optimistic — drop it locally right away rather than waiting on the
    // next poll tick, then confirm against the backend's per-table delete.
    setOrders(prev => {
      const next = { ...prev }
      delete next[key]
      return next
    })
    fetch(`${launcherUrl}/orders/${selectedMap}/${key}`, { method: 'DELETE' })
      .then(r => r.json())
      .then(d => { if (!d.ok) showToast('Could not clear order', 'danger') })
      .catch(() => showToast('Could not reach launcher to clear order', 'danger'))
  }

  useEffect(() => {
    const tick = () => {
      const now = new Date()
      const h = now.getHours()
      setGreeting(h < 12 ? 'Good Morning' : h < 17 ? 'Good Afternoon' : 'Good Evening')
      setTime(now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }))
    }
    tick()
    const id = setInterval(tick, 30000)
    return () => clearInterval(id)
  }, [])

  const entries = Object.entries(tables)
    .filter(([key]) => key !== '0')
    .sort((a, b) => Number(a[0]) - Number(b[0]))

  // Key "0" is always the dock/home position (waypoint_manager.py's own
  // convention) — use its real saved pose instead of hardcoding (0,0), and
  // don't duplicate it if a table happens to also be named "Home".
  const home = tables['0'] ?? { x: 0, y: 0, qz: 0, qw: 1 }
  const destinations = [
    { name: 'Home', key: '0', x: home.x, y: home.y, qz: home.qz ?? 0, qw: home.qw ?? 1 },
    ...entries.map(([key, t]) => ({ name: t.name || `Table ${key}`, key, x: t.x, y: t.y, qz: t.qz ?? 0, qw: t.qw ?? 1 })),
  ].filter((d, i, arr) => arr.findIndex(x => x.name === d.name) === i) // drop duplicate names, keep the first (real Home wins)

  const addActivity = useCallback((dest, task) => {
    const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    setActivity(prev => [{ dest, task, time }, ...prev].slice(0, 6))
  }, [])

  const sendArgo = useCallback((destNameOverride, taskOverride) => {
    const destName = destNameOverride ?? selectedDest
    // taskOverride lets a direct per-card action button (see TABLE_ACTIONS)
    // fire with an explicit task in the same click — reading selectedTask
    // here instead would race the setSelectedTask() call right before it,
    // since state updates aren't visible until the next render.
    const task = taskOverride ?? selectedTask
    if (!destName) { showToast('Select a destination first', 'danger'); return }
    const dest = destinations.find(d => d.name === destName)
    if (!dest) return

    const live = connected && navReady
    setCurStatus('Moving')
    setTaskLabel(`${task} → ${destName}`)
    if (live) {
      onNavigate(dest.x, dest.y, dest.qz, dest.qw, `Argo is heading to ${destName}`)
    } else {
      // No real rosbridge/Nav2 to send a goal to — never block the click for
      // this (testing without a robot connected is a normal, expected state,
      // not an error) — just say plainly that this trip is simulated.
      showToast(`Argo is heading to ${destName} (simulated — no live robot connection)`, 'info')
    }
    addActivity(destName, task)

    const action = TASK_TO_ACTION[task]
    if (action && dest.key && dest.key !== '0') {   // '0' = Home — no table context, never voice-trigger
      if (voiceStatus.running && voiceStatus.table !== dest.key) {
        showToast(`Sonic is busy with another table — wait for that session to finish`, 'warn')
      } else {
        fetch(`${launcherUrl}/voice/start`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action, map: selectedMap, table: dest.key }),
        })
          .then(r => r.json())
          .then(d => { if (!d.ok) showToast(d.error === 'voice_session_busy' ? 'Sonic is busy with another table' : 'Could not start Sonic', 'danger') })
          .catch(() => showToast('Could not reach launcher for the voice session', 'danger'))
      }
    }

    // Live nav has real (if still optimistic) feedback to wait on; with no
    // connection at all there's nothing to wait on, so treat "arrived" as a
    // simple simulated travel time instead — 10s, not the live case's 1.8s.
    setTimeout(() => {
      setCurPos(destName)
      setCurStatus('Arrived')
      showToast(`Argo arrived at ${destName}`, 'ok')
    }, live ? 1800 : 10000)
  }, [selectedDest, selectedTask, destinations, onNavigate, showToast, addActivity, connected, navReady, selectedMap, launcherUrl, voiceStatus])

  const stopVoice = () => {
    fetch(`${launcherUrl}/voice/stop`, { method: 'POST' }).catch(() => {})
  }

  const recall = () => { setSelectedDest('Home'); sendArgo('Home') }

  const radialPages = [
    { id: 'overview', label: 'Overview', action: () => document.getElementById('dash-overview')?.scrollIntoView({ behavior: 'smooth', block: 'start' }),
      icon: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 11l9-7 9 7"/><path d="M5 10v9h14v-9"/></svg> },
    { id: 'places', label: 'Places', action: () => document.getElementById('dash-places')?.scrollIntoView({ behavior: 'smooth', block: 'start' }),
      icon: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 21s-7-6.1-7-11a7 7 0 0 1 14 0c0 4.9-7 11-7 11z"/><circle cx="12" cy="10" r="2.5"/></svg> },
    { id: 'activity', label: 'Activity', action: () => document.getElementById('dash-activity')?.scrollIntoView({ behavior: 'smooth', block: 'start' }),
      icon: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 12h4l2 8 4-16 2 8h6"/></svg> },
    { id: 'order', label: 'Order', href: '/order.html',
      icon: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M6 2 3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4Z"/><path d="M3 6h18M16 10a4 4 0 0 1-8 0"/></svg> },
    { id: 'menu', label: 'Menu', href: '/menu.html',
      icon: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 6h16M4 12h16M4 18h16"/></svg> },
    { id: 'settings', label: 'Settings', action: onOpenSettings,
      icon: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="3"/><path d="M12 1v4M12 19v4M4.22 4.22l2.83 2.83M16.95 16.95l2.83 2.83M1 12h4M19 12h4M4.22 19.78l2.83-2.83M16.95 7.05l2.83-2.83"/></svg> },
  ]

  const chip = (active) => ({
    padding: '8px 14px', borderRadius: 99, fontSize: 12.5, fontWeight: active ? 700 : 600,
    background: active ? 'rgba(226,179,92,0.15)' : 'rgba(255,255,255,0.03)',
    border: `1px solid ${active ? 'rgba(226,179,92,0.38)' : 'var(--border-glass)'}`,
    color: active ? 'var(--gold-bright)' : 'var(--muted)',
    transition: 'all 0.2s',
  })

  return (
    <div style={{ display: 'grid', gridTemplateColumns: '320px 1fr', gap: 0, animation: 'slideUp 0.35s ease' }}>

      {/* ── Left rail: Argo control ── */}
      <aside style={{ padding: '4px 20px 24px 0', display: 'flex', flexDirection: 'column', gap: 16 }}>
        <div>
          <div style={{ fontSize: 10.5, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.14em', fontWeight: 700, marginBottom: 14 }}>Argo Status</div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
            {[
              ['Current spot', curPos, 'Right now', '#fff'],
              ['Status', curStatus, curStatus === 'Moving' ? 'Moving' : 'Ready', curStatus === 'Moving' ? 'var(--gold)' : 'var(--ok)'],
              ['Task', taskLabel, 'Next job', 'var(--gold-bright)'],
              ['Battery', '78%', '~1h 20m', 'var(--ok)'],
            ].map(([k, v, s, color]) => (
              <div key={k} style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid var(--border-glass)', borderRadius: 14, padding: 12 }}>
                <div style={{ fontSize: 9.5, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 700 }}>{k}</div>
                <div style={{ fontSize: 13, fontWeight: 600, marginTop: 5, color, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{v}</div>
                <div style={{ fontSize: 10.5, color: 'var(--muted)', marginTop: 2 }}>{s}</div>
              </div>
            ))}
          </div>
        </div>

        <div style={{ paddingTop: 16, borderTop: '1px dashed rgba(255,255,255,0.08)' }}>
          <div style={{ fontSize: 10.5, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.14em', fontWeight: 700, marginBottom: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{ width: 20, height: 20, borderRadius: 7, background: 'rgba(226,179,92,0.12)', color: 'var(--gold-bright)', display: 'grid', placeItems: 'center', fontSize: 10, fontWeight: 800, flexShrink: 0 }}>1</span>
            What do you need?
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
            {TASKS.map(t => (
              <button key={t} style={chip(selectedTask === t)} onClick={() => { setSelectedTask(t); setTaskLabel(t) }}>{t}</button>
            ))}
          </div>
        </div>

        <div style={{ paddingTop: 16, borderTop: '1px dashed rgba(255,255,255,0.08)' }}>
          <div style={{ fontSize: 10.5, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.14em', fontWeight: 700, marginBottom: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{ width: 20, height: 20, borderRadius: 7, background: 'rgba(226,179,92,0.12)', color: 'var(--gold-bright)', display: 'grid', placeItems: 'center', fontSize: 10, fontWeight: 800, flexShrink: 0 }}>2</span>
            Where should Argo go?
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
            {destinations.map(d => (
              <button key={d.name} style={chip(selectedDest === d.name)} onClick={() => setSelectedDest(d.name)}>{d.name}</button>
            ))}
          </div>
        </div>

        <div style={{ paddingTop: 16, borderTop: '1px dashed rgba(255,255,255,0.08)' }}>
          <div style={{ fontSize: 10.5, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.14em', fontWeight: 700, marginBottom: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{ width: 20, height: 20, borderRadius: 7, background: 'rgba(226,179,92,0.12)', color: 'var(--gold-bright)', display: 'grid', placeItems: 'center', fontSize: 10, fontWeight: 800, flexShrink: 0 }}>3</span>
            Send Argo
          </div>
          <div style={{ display: 'flex', gap: 10 }}>
            <button className="btn-ghost" style={{ flex: 1 }} onClick={recall}>Send Home</button>
            <button className="btn-primary" style={{ flex: 1 }} onClick={() => sendArgo()}>Confirm</button>
          </div>
          {!(connected && navReady) && (
            <p style={{ fontSize: 11, color: 'var(--muted)', marginTop: 8 }}>
              {navState === 'running' && navMap === selectedMap
                ? 'Nav2 is still starting up — this takes a minute or two, or Send/Confirm now to simulate the trip.'
                : `No live navigation for ${selectedMap} yet — Send/Confirm still works, it'll simulate the trip.`}
            </p>
          )}
          {voiceStatus.running && (
            <div style={{
              marginTop: 12, padding: '10px 14px', borderRadius: 10,
              background: 'rgba(226,179,92,0.08)', border: '1px solid rgba(226,179,92,0.28)',
              display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10,
            }}>
              <span style={{ fontSize: 12, color: 'var(--gold-bright)', fontWeight: 600 }}>
                🎙️ Sonic is talking with {destinations.find(d => d.key === voiceStatus.table)?.name || `Table ${voiceStatus.table}`} ({voiceStatus.action})
              </span>
              <button onClick={stopVoice} className="btn-ghost" style={{ padding: '4px 10px', fontSize: 11.5 }}>Stop</button>
            </div>
          )}
        </div>
      </aside>

      {/* ── Main ── */}
      <main style={{ paddingLeft: 20, borderLeft: '1px solid var(--border-glass)' }}>
        <div id="dash-overview" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-end', marginBottom: 28, flexWrap: 'wrap', gap: 16 }}>
          <div>
            <h2 style={{ fontFamily: 'var(--font-heading)', fontSize: 27, fontWeight: 800, letterSpacing: '-0.5px' }}>{greeting}</h2>
            <p style={{ color: 'var(--muted)', fontSize: 14, marginTop: 4 }}>Here's what's happening right now.</p>
          </div>
          <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
            <button
              onClick={onAddMap}
              style={{
                padding: '9px 16px', borderRadius: 14, fontSize: 12.5, fontWeight: 700,
                background: 'rgba(255,255,255,0.04)', border: '1px solid var(--border-glass)', color: 'var(--muted)',
                display: 'flex', alignItems: 'center', gap: 7,
              }}
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="3"/><path d="M12 1v4M12 19v4M4.22 4.22l2.83 2.83M16.95 16.95l2.83 2.83M1 12h4M19 12h4M4.22 19.78l2.83-2.83M16.95 7.05l2.83-2.83"/></svg>
              Setup / Remap
            </button>

            {navReady ? (
              <button
                onClick={stopNav}
                title={`Navigating on ${selectedMap} — click to stop`}
                style={{
                  padding: '9px 16px', borderRadius: 99, fontSize: 12.5, fontWeight: 600,
                  background: 'rgba(59,240,155,0.08)', border: '1px solid rgba(59,240,155,0.28)', color: 'var(--ok)',
                  display: 'flex', alignItems: 'center', gap: 8,
                }}
              >
                <span style={{ width: 7, height: 7, borderRadius: '50%', background: 'var(--ok)', boxShadow: '0 0 7px var(--ok)', animation: 'pulse-dot 2s infinite' }} />
                Navigating on {selectedMap}
              </button>
            ) : navState === 'running' && navMap === selectedMap && !connected ? (
              // The launcher's wrapper process is up, but we have no rosbridge
              // WebSocket connection at all — /rosapi/topics can never resolve
              // in this state, so navActionReady would silently stay false
              // forever. Saying "Waiting for Nav2" here would hide the real
              // blocker (rosbridge itself isn't reachable) behind a message
              // that implies Nav2 is just slow — say what's actually wrong.
              <button
                onClick={stopNav}
                title="rosbridge_websocket isn't reachable — check it's running and the URL/port are correct"
                style={{
                  padding: '9px 16px', borderRadius: 99, fontSize: 12.5, fontWeight: 600,
                  background: 'rgba(255,94,94,0.08)', border: '1px solid rgba(255,94,94,0.3)', color: 'var(--danger)',
                  display: 'flex', alignItems: 'center', gap: 8,
                }}
              >
                <span style={{ width: 7, height: 7, borderRadius: '50%', background: 'var(--danger)', flexShrink: 0 }} />
                Not connected to rosbridge
              </button>
            ) : navState === 'running' && navMap === selectedMap && navProgress.status === 'ERROR' ? (
              // sh/start_argo_nav_ui.sh itself reported a specific failed
              // step (see GET /nav_progress) — show exactly what broke
              // instead of leaving this looking identical to "still
              // starting", which used to mean the only way to find out
              // something had failed, or what, was to SSH in and dig
              // through process lists and hidden log files by hand.
              <button
                onClick={stopNav}
                title={`${navProgress.message} — click to stop`}
                style={{
                  padding: '9px 16px', borderRadius: 99, fontSize: 12.5, fontWeight: 600,
                  background: 'rgba(255,94,94,0.08)', border: '1px solid rgba(255,94,94,0.3)', color: 'var(--danger)',
                  display: 'flex', alignItems: 'center', gap: 8, maxWidth: 340,
                }}
              >
                <span style={{ width: 7, height: 7, borderRadius: '50%', background: 'var(--danger)', flexShrink: 0 }} />
                <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>Failed: {navProgress.message}</span>
              </button>
            ) : navState === 'running' && navMap === selectedMap ? (
              // Wrapper process is up AND rosbridge is connected, but Nav2's
              // own ~90s+ lifecycle chain (camera wait, costmap wait, several
              // lifecycle activations) hasn't actually finished yet — don't
              // claim "ready" early. navActionReady's own polling (above)
              // has no fixed timeout — it keeps checking every 2s for as long
              // as this state holds, however long Nav2 actually takes.
              <button
                onClick={stopNav}
                title="Nav2 is still starting up — click to cancel"
                style={{
                  padding: '9px 16px', borderRadius: 99, fontSize: 12.5, fontWeight: 600,
                  background: 'rgba(127,168,232,0.1)', border: '1px solid rgba(127,168,232,0.3)', color: 'var(--blue)',
                  display: 'flex', alignItems: 'center', gap: 8, maxWidth: 340,
                }}
              >
                <span style={{ width: 12, height: 12, borderRadius: '50%', border: '2px solid currentColor', borderTopColor: 'transparent', animation: 'spin-slow 0.8s linear infinite', flexShrink: 0 }} />
                <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {navProgress.message || 'Waiting for Nav2 (~1–2 min)…'}
                </span>
              </button>
            ) : (
              <button
                onClick={startNav}
                disabled={navState === 'starting'}
                style={{
                  padding: '9px 16px', borderRadius: 99, fontSize: 12.5, fontWeight: 700,
                  background: 'rgba(226,179,92,0.14)', border: '1px solid rgba(226,179,92,0.4)', color: 'var(--gold-bright)',
                  display: 'flex', alignItems: 'center', gap: 8, opacity: navState === 'starting' ? 0.6 : 1,
                }}
              >
                {navState === 'starting' && (
                  <span style={{ width: 12, height: 12, borderRadius: '50%', border: '2px solid currentColor', borderTopColor: 'transparent', animation: 'spin-slow 0.8s linear infinite', flexShrink: 0 }} />
                )}
                {navState === 'starting' ? 'Starting…' : `Start Navigating on ${selectedMap}`}
              </button>
            )}

            {/* Nav2's localization has no idea where the robot actually is
                until told — normally an RViz "2D Pose Estimate" step, but
                the robot is headless (no RViz/DISPLAY). Only offered once
                navReady is genuinely true (the same real-readiness check
                gating the buttons above), since setting a pose before the
                action server exists would just be silently ignored. */}
            {navReady && (
              <button
                onClick={() => setShowSetPose(true)}
                title="Tell Nav2 where Argo is actually standing right now"
                style={{
                  padding: '9px 16px', borderRadius: 99, fontSize: 12.5, fontWeight: 600,
                  background: 'rgba(226,179,92,0.08)', border: '1px solid rgba(226,179,92,0.28)', color: 'var(--gold-bright)',
                  display: 'flex', alignItems: 'center', gap: 8,
                }}
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M12 2 12 6M12 18 12 22M2 12 6 12M18 12 22 12"/><circle cx="12" cy="12" r="5"/></svg>
                Set Robot Position
              </button>
            )}
            <div style={{ padding: '9px 16px', borderRadius: 99, fontSize: 13, fontWeight: 600, background: 'rgba(255,255,255,0.03)', border: '1px solid var(--border-glass)', color: 'var(--muted)' }}>{time}</div>
          </div>
        </div>

        {/* Stats */}
        <section style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 14, marginBottom: 32 }}>
          {[
            { k: "Today's Revenue", v: '₹18,240', d: '+12.5% vs yesterday', pos: true },
            { k: 'Total Orders', v: '56', d: '+8 new today' },
            { k: 'Locations Mapped', v: String(entries.length), d: 'Ready to navigate' },
            { k: 'Argo Status', v: curStatus === 'Moving' ? 'Moving' : 'Ready', d: 'Battery 78%', color: curStatus === 'Moving' ? 'var(--gold)' : 'var(--ok)' },
          ].map(({ k, v, d, pos, color }) => (
            <div key={k} className="glass-card" style={{ padding: '18px 20px', display: 'flex', flexDirection: 'column', gap: 5 }}>
              <div style={{ fontSize: 10.5, color: 'var(--muted)', letterSpacing: '0.08em', textTransform: 'uppercase', fontWeight: 700 }}>{k}</div>
              <div style={{ fontFamily: 'var(--font-heading)', fontSize: 24, fontWeight: 800, margin: '2px 0', color }}>{v}</div>
              <div style={{ fontSize: 11.5, color: pos ? 'var(--ok)' : 'var(--muted)' }}>{d}</div>
            </div>
          ))}
        </section>

        {/* Saved places grid */}
        <section id="dash-places">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
            <div>
              <div style={{ fontFamily: 'var(--font-heading)', fontSize: 19, fontWeight: 700 }}>Saved Places</div>
              <div style={{ color: 'var(--muted)', fontSize: 12.5, marginTop: 3 }}>Click any place to send Argo there instantly.</div>
            </div>
            <div style={{ display: 'flex', gap: 16, alignItems: 'center' }}>
              <button onClick={() => setShowAddPlace(true)} style={{ color: 'var(--gold-bright)', fontSize: 13, fontWeight: 600 }}>+ Add place</button>
              <button onClick={onAddMap} style={{ color: 'var(--muted)', fontSize: 13, fontWeight: 600 }} title="Full setup wizard — remap the space, build a new map, etc.">Setup wizard →</button>
            </div>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))', gap: 16, marginBottom: 36 }}>
            {entries.length === 0 ? (
              <div style={{ gridColumn: '1/-1', textAlign: 'center', padding: '56px 20px', color: 'var(--muted)', fontSize: 14, lineHeight: 1.8 }}>
                No places saved yet.<br/>
                <button onClick={onAddMap} style={{ color: 'var(--gold-bright)', fontWeight: 700 }}>Run the setup flow</button> to map your space and label locations — then they'll appear here.
              </div>
            ) : entries.map(([key, t]) => {
              const label = t.name || `Table ${key}`
              const orderRaw = orders[key]
              const order = orderRaw && orderRaw.items && orderRaw.items.length ? orderRaw : null
              const billOpen = expandedBills.has(key)
              return (
                <div
                  key={key}
                  className="glass-card"
                  onClick={() => editId !== key && setSelectedDest(label)}
                  style={{
                    borderLeft: `3px solid ${selectedDest === label ? 'var(--gold)' : 'rgba(255,255,255,0.15)'}`,
                    padding: '18px 20px', cursor: editId === key ? 'default' : 'pointer',
                    boxShadow: selectedDest === label ? '0 0 0 2px rgba(226,179,92,0.2), var(--shadow-fluid)' : undefined,
                  }}
                >
                  {editId === key ? (
                    <div style={{ display: 'flex', gap: 6 }} onClick={e => e.stopPropagation()}>
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
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
                      <div style={{ fontFamily: 'var(--font-heading)', fontSize: 16, fontWeight: 800 }}>{label}</div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 4, flexShrink: 0 }} onClick={e => e.stopPropagation()}>
                        <button onClick={() => { setEditId(key); setEditName(label) }} style={{ padding: '4px 6px', color: 'var(--muted)', borderRadius: 6 }} title="Rename">
                          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                        </button>
                        <button onClick={() => removeTable(key)} style={{ padding: '4px 6px', color: 'var(--danger)', borderRadius: 6, opacity: 0.7 }} title="Remove">
                          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>
                        </button>
                        <div style={{ fontSize: 10, padding: '4px 10px', borderRadius: 99, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em', background: 'rgba(255,255,255,0.05)', border: '1px solid var(--border-glass)', color: 'var(--muted)', whiteSpace: 'nowrap' }}>Saved</div>
                      </div>
                    </div>
                  )}
                  <div style={{ fontSize: 11.5, color: 'var(--muted)', marginTop: 8 }}>{Number(t.x).toFixed(1)} m, {Number(t.y).toFixed(1)} m</div>

                  {order && (
                    <div style={{ marginTop: 10 }}>
                      <button
                        onClick={(e) => { e.stopPropagation(); toggleBill(key) }}
                        style={{
                          fontSize: 10.5, fontWeight: 700, padding: '3px 9px', borderRadius: 99,
                          background: 'rgba(226,179,92,0.1)', border: '1px solid rgba(226,179,92,0.28)',
                          color: 'var(--gold-bright)', whiteSpace: 'nowrap',
                        }}
                      >
                        🧾 {order.items.length} item{order.items.length === 1 ? '' : 's'} · {money(order.total)} {billOpen ? '▲' : '▼'}
                      </button>
                      {billOpen && (
                        <div style={{ marginTop: 8, paddingTop: 8, borderTop: '1px dashed rgba(226,179,92,0.3)' }} onClick={e => e.stopPropagation()}>
                          {order.items.map((it, i) => (
                            <div key={it.id || i} style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, marginBottom: 3 }}>
                              <span>{it.qty} × {it.name}</span>
                              <span style={{ fontFamily: 'monospace' }}>{money(it.qty * it.price)}</span>
                            </div>
                          ))}
                          <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 6, paddingTop: 6, borderTop: '1px solid rgba(255,255,255,0.08)', fontWeight: 700, fontSize: 12.5 }}>
                            <span>Total</span>
                            <span style={{ fontFamily: 'monospace' }}>{money(order.total)}</span>
                          </div>
                          <button
                            onClick={(e) => { e.stopPropagation(); clearOrder(key) }}
                            title="Clear this table's order history"
                            style={{ marginTop: 8, fontSize: 10.5, fontWeight: 600, color: 'var(--danger)', opacity: 0.75, padding: '2px 6px' }}
                          >
                            Clear
                          </button>
                        </div>
                      )}
                    </div>
                  )}

                  {/* Direct action buttons — fire immediately with this table
                      as destination, no detour through the sidebar's
                      select-task / select-destination / confirm steps. */}
                  <div style={{ marginTop: 14, display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6 }}>
                    {TABLE_ACTIONS.map(([task, actLabel]) => {
                      const blocked = voiceStatus.running && voiceStatus.table !== key
                      const active = voiceStatus.running && voiceStatus.table === key && voiceStatus.action === TASK_TO_ACTION[task]
                      return (
                        <button
                          key={task}
                          disabled={blocked}
                          onClick={(e) => {
                            e.stopPropagation()
                            setSelectedTask(task)
                            setSelectedDest(label)
                            sendArgo(label, task)
                          }}
                          style={{
                            padding: '9px 8px', borderRadius: 10, fontSize: 11.5, fontWeight: 700,
                            background: active ? 'rgba(59,240,155,0.15)' : 'rgba(226,179,92,0.08)',
                            border: `1px solid ${active ? 'rgba(59,240,155,0.35)' : 'rgba(226,179,92,0.22)'}`,
                            color: active ? 'var(--ok)' : 'var(--gold-bright)',
                            opacity: blocked ? 0.4 : 1,
                            cursor: blocked ? 'not-allowed' : 'pointer',
                          }}
                        >
                          {active ? 'On the way…' : actLabel}
                        </button>
                      )
                    })}
                  </div>
                </div>
              )
            })}
          </div>
        </section>

        {/* Activity + Alerts */}
        <section id="dash-activity" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20 }}>
          <div className="glass-card" style={{ padding: 24 }}>
            <div style={{ fontFamily: 'var(--font-heading)', fontSize: 17, fontWeight: 700 }}>Recent Activity</div>
            <div style={{ color: 'var(--muted)', fontSize: 12, margin: '4px 0 16px' }}>What Argo has done today.</div>
            <ul style={{ listStyle: 'none' }}>
              {activity.length === 0 ? (
                <li style={{ display: 'flex', alignItems: 'center', gap: 14, padding: '13px 0' }}>
                  <div style={{ width: 34, height: 34, borderRadius: 10, background: 'rgba(255,255,255,0.03)', border: '1px solid var(--border-glass)', color: 'var(--gold-bright)', display: 'grid', placeItems: 'center', fontWeight: 700, fontSize: 11, flexShrink: 0 }}>—</div>
                  <div style={{ fontSize: 13 }}>No activity yet<small style={{ display: 'block', color: 'var(--muted)', fontSize: 11, marginTop: 2 }}>Argo is ready to start</small></div>
                </li>
              ) : activity.map((a, i) => (
                <li key={i} style={{ display: 'flex', alignItems: 'center', gap: 14, padding: '13px 0', borderBottom: i < activity.length - 1 ? '1px solid rgba(255,255,255,0.05)' : 'none' }}>
                  <div style={{ width: 34, height: 34, borderRadius: 10, background: 'rgba(255,255,255,0.03)', border: '1px solid var(--border-glass)', color: 'var(--gold-bright)', display: 'grid', placeItems: 'center', fontWeight: 700, fontSize: 11, flexShrink: 0 }}>{a.dest.slice(0, 4)}</div>
                  <div style={{ fontSize: 13 }}>{a.task} → {a.dest}<small style={{ display: 'block', color: 'var(--muted)', fontSize: 11, marginTop: 2 }}>{a.time}</small></div>
                </li>
              ))}
            </ul>
          </div>
          <div className="glass-card" style={{ padding: 24 }}>
            <div style={{ fontFamily: 'var(--font-heading)', fontSize: 17, fontWeight: 700 }}>Alerts</div>
            <div style={{ color: 'var(--muted)', fontSize: 12, margin: '4px 0 16px' }}>Things that may need attention.</div>
            <ul style={{ listStyle: 'none' }}>
              <li style={{ display: 'flex', alignItems: 'center', gap: 14, padding: '13px 0', borderBottom: '1px solid rgba(255,255,255,0.05)' }}>
                <div style={{ width: 8, height: 8, borderRadius: '50%', background: 'var(--ok)', flexShrink: 0 }} />
                <div style={{ fontSize: 13 }}>All systems normal<small style={{ display: 'block', color: 'var(--muted)', fontSize: 11, marginTop: 2 }}>Argo is online and ready</small></div>
              </li>
              <li style={{ display: 'flex', alignItems: 'center', gap: 14, padding: '13px 0' }}>
                <div style={{ width: 8, height: 8, borderRadius: '50%', background: 'var(--gold)', flexShrink: 0 }} />
                <div style={{ fontSize: 13 }}>No pending tasks<small style={{ display: 'block', color: 'var(--muted)', fontSize: 11, marginTop: 2 }}>Choose a destination and confirm</small></div>
              </li>
            </ul>
          </div>
        </section>
      </main>

      <RadialNav pages={radialPages} activePage="overview" />

      {showSetPose && (
        <div
          onClick={() => setShowSetPose(false)}
          style={{
            position: 'fixed', inset: 0, zIndex: 200, background: 'rgba(0,0,0,0.6)',
            display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 24,
          }}
        >
          <div
            onClick={e => e.stopPropagation()}
            className="glass-dense"
            style={{ width: '100%', maxWidth: 820, padding: 22, animation: 'slideUp 0.2s ease' }}
          >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 6 }}>
              <div>
                <div style={{ fontFamily: 'var(--font-heading)', fontWeight: 700, fontSize: 16 }}>Set Robot Position</div>
                <p style={{ fontSize: 12.5, color: 'var(--muted)', marginTop: 4, lineHeight: 1.6, maxWidth: 560 }}>
                  Click the map where Argo is actually standing, then drag in the direction it's facing before releasing — same as RViz's "2D Pose Estimate" tool.
                </p>
              </div>
              <button onClick={() => setShowSetPose(false)} style={{ color: 'var(--muted)', padding: 4, flexShrink: 0 }}>
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M18 6 6 18M6 6l12 12"/></svg>
              </button>
            </div>
            <div style={{ height: 440, marginTop: 12 }}>
              <MapCanvas
                mapData={mapData}
                robotPose={robotPose}
                poseEstimateMode
                onPoseEstimate={({ wx, wy, theta }) => {
                  onSetInitialPose?.(wx, wy, theta)
                  setShowSetPose(false)
                }}
              />
            </div>
          </div>
        </div>
      )}

      {showAddPlace && (
        <div
          onClick={() => { setShowAddPlace(false); setPendingPlace(null); setNewPlaceName('') }}
          style={{
            position: 'fixed', inset: 0, zIndex: 200, background: 'rgba(0,0,0,0.6)',
            display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 24,
          }}
        >
          <div
            onClick={e => e.stopPropagation()}
            className="glass-dense"
            style={{ width: '100%', maxWidth: 820, padding: 22, animation: 'slideUp 0.2s ease' }}
          >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 6 }}>
              <div>
                <div style={{ fontFamily: 'var(--font-heading)', fontWeight: 700, fontSize: 16 }}>Add Place</div>
                <p style={{ fontSize: 12.5, color: 'var(--muted)', marginTop: 4, lineHeight: 1.6, maxWidth: 560 }}>
                  Click the map where the new place should be, then name it.
                </p>
              </div>
              <button onClick={() => { setShowAddPlace(false); setPendingPlace(null); setNewPlaceName('') }} style={{ color: 'var(--muted)', padding: 4, flexShrink: 0 }}>
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M18 6 6 18M6 6l12 12"/></svg>
              </button>
            </div>
            <div style={{ height: 400, marginTop: 12 }}>
              <MapCanvas
                mapData={mapData}
                robotPose={robotPose}
                clickable
                onMapClick={({ wx, wy }) => setPendingPlace({ wx, wy })}
              />
            </div>
            {pendingPlace && (
              <div style={{ marginTop: 14, display: 'flex', gap: 8 }}>
                <input
                  autoFocus value={newPlaceName}
                  onChange={e => setNewPlaceName(e.target.value)}
                  onKeyDown={e => { if (e.key === 'Enter') saveNewPlace() }}
                  placeholder="e.g. Table 6, Window Booth"
                  style={{
                    flex: 1, padding: '11px 14px', borderRadius: 12,
                    background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(226,179,92,0.3)',
                    color: '#fff', outline: 'none', boxSizing: 'border-box',
                  }}
                />
                <button onClick={saveNewPlace} className="btn-ok" disabled={!newPlaceName.trim()}>Save</button>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
