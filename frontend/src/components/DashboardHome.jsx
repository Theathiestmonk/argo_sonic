import { useState, useEffect, useCallback, useRef, forwardRef, useImperativeHandle } from 'react'
import { ros } from '../ros'
import RadialNav from './RadialNav'
import MapCanvas from './MapCanvas'
import TeleopPad from './TeleopPad'

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
  ['Deliver',      'Deliver'],
  ['Billing',      'Send Bill'],
]

const DashboardHomeComponent = forwardRef(({ launcherUrl, selectedMap, connected, showToast, onNavigate, onSetInitialPose, mapData, robotPose, onAddMap, onOpenSettings, onActivityToggle, onNavInitializing, onNavReady, onNavPoseSet }, ref) => {
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
  const [showTeleopPad, setShowTeleopPad] = useState(false)
  const [showActivityPanel, setShowActivityPanel] = useState(false)

  useImperativeHandle(ref, () => ({
    toggleActivityPanel: () => setShowActivityPanel(prev => !prev),
    startNav: startNav,
    estop: estop,
    setShowSetPose: setShowSetPose,
  }))

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

  // Update parent about nav readiness state
  useEffect(() => {
    onNavReady?.(navReady)
    if (navState === 'running') {
      onNavInitializing?.(false)
    }
  }, [navReady, onNavReady, navState, onNavInitializing])

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

  // Mirrors voiceStatus's polling pattern — independent state, own endpoint,
  // so the E-Stop button's label always reflects the actual backend state
  // rather than an optimistic local guess (e.g. after a page refresh, or if
  // another operator on a different device already hit it).
  const [estopped, setEstopped] = useState(false)
  useEffect(() => {
    let cancelled = false
    const load = () => {
      fetch(`${launcherUrl}/estop/status`)
        .then(r => r.json())
        .then(d => { if (!cancelled) setEstopped(!!d.estopped) })
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

    const action = TASK_TO_ACTION[task]
    const startVoiceSession = () => {
      if (!action || !dest.key || dest.key === '0') return   // '0' = Home — no table context, never voice-trigger
      if (voiceStatus.running && voiceStatus.table !== dest.key) {
        showToast(`Sonic is busy with another table — wait for that session to finish`, 'warn')
        return
      }
      fetch(`${launcherUrl}/voice/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, map: selectedMap, table: dest.key }),
      })
        .then(r => r.json())
        .then(d => { if (!d.ok) showToast(d.error === 'voice_session_busy' ? 'Sonic is busy with another table' : 'Could not start Sonic', 'danger') })
        .catch(() => showToast('Could not reach launcher for the voice session', 'danger'))
    }
    const updateArrivedUI = () => {
      setCurPos(destName)
      setCurStatus('Arrived')
      showToast(`Argo arrived at ${destName}`, 'ok')
    }

    const live = connected && navReady
    setCurStatus('Moving')
    setTaskLabel(`${task} → ${destName}`)
    if (live) {
      // Sonic used to fire the instant this button was clicked, regardless
      // of whether the robot had actually reached the table yet — customers
      // could get greeted by a robot that was still ten meters away. Now
      // gated on onNavigate's real arrival detection (see App.jsx's
      // sendNavGoal) instead of a flat click-time trigger.
      onNavigate(dest.x, dest.y, dest.qz, dest.qw, `Argo is heading to ${destName}`,
        () => { updateArrivedUI(); startVoiceSession() })
    } else {
      // No real rosbridge/Nav2 to send a goal to — never block the click for
      // this (testing without a robot connected is a normal, expected state,
      // not an error) — just say plainly that this trip is simulated. No
      // real arrival to wait for either, so keep the original flat-timer
      // simulated trip (10s) with Sonic firing immediately, same as this
      // has always worked for testing without a robot present.
      showToast(`Argo is heading to ${destName} (simulated — no live robot connection)`, 'info')
      startVoiceSession()
      setTimeout(updateArrivedUI, 10000)
    }
    addActivity(destName, task)
  }, [selectedDest, selectedTask, destinations, onNavigate, showToast, addActivity, connected, navReady, selectedMap, launcherUrl, voiceStatus])

  const stopVoice = () => {
    fetch(`${launcherUrl}/voice/stop`, { method: 'POST' }).catch(() => {})
  }

  // No confirm() dialog here on purpose, unlike stopNav() — this is a real
  // emergency stop (cuts motor commands immediately if the robot is
  // physically misbehaving); a confirmation dialog would defeat the point
  // of "immediately". Kills only serial_bridge, not the rest of the nav
  // stack (SLAM/planner/etc. keep running), so resuming doesn't require a
  // full restart.
  const estop = () => {
    fetch(`${launcherUrl}/estop`, { method: 'POST' })
      .then(() => { setEstopped(true); showToast('E-STOP: motors cut', 'danger') })
      .catch(() => showToast('Could not reach launcher for E-STOP', 'danger'))
  }

  const estopResume = () => {
    fetch(`${launcherUrl}/estop/resume`, { method: 'POST' })
      .then(() => { setEstopped(false); showToast('Motors resumed', 'ok') })
      .catch(() => showToast('Could not reach launcher to resume motors', 'danger'))
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
          <h2 style={{ fontFamily: 'var(--font-heading)', fontSize: 20, fontWeight: 800, letterSpacing: '-0.5px', marginBottom: 4 }}>{greeting}</h2>
          <p style={{ color: 'var(--muted)', fontSize: 12, marginBottom: 16 }}>Here's what's happening right now.</p>
        </div>

        <div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
            {[
              ['Location', curPos, '', '#fff'],
              ['Status', curStatus, '', curStatus === 'Moving' ? 'var(--gold)' : 'var(--ok)'],
              ['Task', taskLabel, '', 'var(--gold-bright)'],
              ['Working Hours', '~1h 20m', 'Remaining', 'var(--ok)'],
            ].map(([k, v, s, color]) => (
              <div key={k} className="glass-card" style={{ padding: 12 }}>
                <div style={{ fontSize: 9.5, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 700 }}>{k}</div>
                <div style={{ fontSize: 13, fontWeight: 600, marginTop: 5, color, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{v}</div>
                <div style={{ fontSize: 10.5, color: 'var(--muted)', marginTop: 2 }}>{s}</div>
              </div>
            ))}
          </div>
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr', gap: 10 }}>
          {[
            { k: "Today's Revenue", v: '₹18,240', d: '+12.5% vs yesterday', pos: true },
            { k: 'Total Orders', v: '56', d: '+8 new today' },
          ].map(({ k, v, d, pos }) => (
            <div key={k} className="glass-card" style={{ padding: 12 }}>
              <div style={{ fontSize: 9.5, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 700 }}>{k}</div>
              <div style={{ fontSize: 13, fontWeight: 600, marginTop: 5, color: 'var(--gold-bright)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{v}</div>
              <div style={{ fontSize: 10.5, color: pos ? 'var(--ok)' : 'var(--muted)', marginTop: 2 }}>{d}</div>
            </div>
          ))}
        </div>

        {navState === 'running' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginTop: 8 }}>
            {!navActionReady ? (
              <>
                <div className="glass-card" style={{ padding: 12 }}>
                  <div style={{ fontSize: 9.5, color: 'var(--ok)', textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 700 }}>⏳ Initializing</div>
                  <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 6, lineHeight: 1.5 }}>
                    {navProgress.message || 'Starting navigation stack...'}
                  </div>
                </div>
                <button
                  onClick={() => setShowSetPose(true)}
                  style={{
                    width: '100%', padding: '12px 16px', borderRadius: 10, fontSize: 12.5, fontWeight: 700,
                    background: 'rgba(59,240,155,0.12)', border: '1px solid rgba(59,240,155,0.3)', color: 'var(--ok)',
                    cursor: 'pointer',
                  }}
                >
                  📍 Set 2D Pose Estimate
                </button>
              </>
            ) : (
              <div className="glass-card" style={{ padding: 12, background: 'rgba(59,240,155,0.15)' }}>
                <div style={{ fontSize: 9.5, color: 'var(--ok)', textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 700 }}>✓ Ready</div>
                <div style={{ fontSize: 11, color: 'var(--ok)', marginTop: 6 }}>Navigation stack is active and ready for goals</div>
              </div>
            )}
          </div>
        )}

      </aside>

      {/* ── Main ── */}
      <main style={{ paddingLeft: 20, borderLeft: '1px solid var(--border-glass)' }}>
        {/* Saved places grid */}
        <section id="dash-places">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
            <div style={{ fontFamily: 'var(--font-heading)', fontSize: 19, fontWeight: 700 }}>Locations</div>
            <div style={{ display: 'flex', gap: 16, alignItems: 'center' }}>
              <button onClick={() => setShowAddPlace(true)} style={{ color: 'var(--gold-bright)', fontSize: 13, fontWeight: 600 }}>+ Add place</button>
              <button onClick={onAddMap} style={{ color: 'var(--muted)', fontSize: 13, fontWeight: 600 }} title="Full setup wizard — remap the space, build a new map, etc.">Setup wizard →</button>
            </div>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 16, marginBottom: 36 }}>
            {/* Home Card */}
            <div
              className="glass-card"
              style={{
                padding: '18px 20px', cursor: 'pointer',
                border: selectedDest === 'Home' ? '2px solid var(--gold)' : 'none',
                display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 12,
                minHeight: '280px',
              }}
              onClick={() => setSelectedDest('Home')}
            >
              <div style={{
                width: '56px', height: '56px', borderRadius: '50%',
                border: '2px solid var(--gold-bright)', display: 'flex', alignItems: 'center', justifyContent: 'center',
              }}>
                <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ color: 'var(--gold-bright)' }}>
                  <path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>
                  <polyline points="9 22 9 12 15 12 15 22"/>
                </svg>
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr', gap: 6, width: '100%' }}>
                <button
                  onClick={(e) => { e.stopPropagation(); setSelectedDest('Kitchen'); sendArgo('Kitchen') }}
                  style={{
                    padding: '9px 8px', borderRadius: 10, fontSize: 11.5, fontWeight: 700,
                    background: 'rgba(226,179,92,0.08)', border: '1px solid rgba(226,179,92,0.22)',
                    color: 'var(--gold-bright)', cursor: 'pointer',
                  }}
                >
                  Go to kitchen
                </button>
                <button
                  onClick={(e) => { e.stopPropagation(); setSelectedDest('Docker Station'); sendArgo('Docker Station') }}
                  style={{
                    padding: '9px 8px', borderRadius: 10, fontSize: 11.5, fontWeight: 700,
                    background: 'rgba(226,179,92,0.08)', border: '1px solid rgba(226,179,92,0.22)',
                    color: 'var(--gold-bright)', cursor: 'pointer',
                  }}
                >
                  Go to Docker
                </button>
              </div>
            </div>

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
                    padding: '18px 20px', cursor: editId === key ? 'default' : 'pointer',
                    border: selectedDest === label ? '2px solid var(--gold)' : 'none',
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
                      </div>
                    </div>
                  )}
                  <div style={{ fontSize: 11.5, color: 'var(--muted)', marginTop: 8 }}>{Number(t.x).toFixed(1)} m, {Number(t.y).toFixed(1)} m</div>

                  {order && (
                    <div style={{ marginTop: 10, paddingTop: 8, borderTop: '1px dashed rgba(226,179,92,0.3)' }} onClick={e => e.stopPropagation()}>
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

                  {/* Direct action buttons — fire immediately with this table
                      as destination, no detour through the sidebar's
                      select-task / select-destination / confirm steps. */}
                  <div style={{ marginTop: 14, display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 6 }}>
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

      </main>

      <RadialNav pages={radialPages} activePage="overview" />

      {/* Floating Activity/Alerts Panel — top right corner */}
      {showActivityPanel && (
        <div style={{ position: 'fixed', top: 80, right: 24, zIndex: 100 }}>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14, maxWidth: 640 }}>
            <div className="glass-card" style={{ padding: 20 }}>
              <div style={{ fontFamily: 'var(--font-heading)', fontSize: 15, fontWeight: 700, marginBottom: 12 }}>Recent Activity</div>
              <div style={{ color: 'var(--muted)', fontSize: 11, margin: '0 0 12px' }}>What Argo has done today.</div>
              <ul style={{ listStyle: 'none' }}>
                {activity.length === 0 ? (
                  <li style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '10px 0' }}>
                    <div style={{ fontSize: 12 }}>No activity yet<small style={{ display: 'block', color: 'var(--muted)', fontSize: 10, marginTop: 2 }}>Argo is ready to start</small></div>
                  </li>
                ) : activity.map((a, i) => (
                  <li key={i} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '10px 0', borderBottom: i < activity.length - 1 ? '1px solid rgba(255,255,255,0.05)' : 'none' }}>
                    <div style={{ width: 28, height: 28, borderRadius: 8, background: 'rgba(255,255,255,0.03)', color: 'var(--gold-bright)', display: 'grid', placeItems: 'center', fontWeight: 700, fontSize: 10, flexShrink: 0 }}>{a.dest.slice(0, 3)}</div>
                    <div style={{ fontSize: 12 }}>{a.task} → {a.dest}<small style={{ display: 'block', color: 'var(--muted)', fontSize: 10, marginTop: 1 }}>{a.time}</small></div>
                  </li>
                ))}
              </ul>
            </div>
            <div className="glass-card" style={{ padding: 20 }}>
              <div style={{ fontFamily: 'var(--font-heading)', fontSize: 15, fontWeight: 700, marginBottom: 12 }}>Alerts</div>
              <div style={{ color: 'var(--muted)', fontSize: 11, margin: '0 0 12px' }}>Things that may need attention.</div>
              <ul style={{ listStyle: 'none' }}>
                <li style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '10px 0', borderBottom: '1px solid rgba(255,255,255,0.05)' }}>
                  <div style={{ width: 8, height: 8, borderRadius: '50%', background: 'var(--ok)', flexShrink: 0 }} />
                  <div style={{ fontSize: 12 }}>All systems normal<small style={{ display: 'block', color: 'var(--muted)', fontSize: 10, marginTop: 1 }}>Argo is online and ready</small></div>
                </li>
                <li style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '10px 0' }}>
                  <div style={{ width: 8, height: 8, borderRadius: '50%', background: 'var(--gold)', flexShrink: 0 }} />
                  <div style={{ fontSize: 12 }}>No pending tasks<small style={{ display: 'block', color: 'var(--muted)', fontSize: 10, marginTop: 1 }}>Choose a destination and confirm</small></div>
                </li>
              </ul>
            </div>
          </div>
        </div>
      )}

      {/* Floating Teleop Pad — bottom right corner */}
      <div style={{ position: 'fixed', bottom: 24, right: 24, zIndex: 100 }}>
        {!showTeleopPad && (
        <button
          onClick={() => setShowTeleopPad(!showTeleopPad)}
          title="Toggle drive controls"
          style={{
            padding: '12px 14px', borderRadius: 12, fontSize: 16,
            background: 'rgba(226,179,92,0.14)', border: '1px solid rgba(226,179,92,0.4)',
            color: 'var(--gold-bright)', cursor: 'pointer', fontWeight: 700,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            transition: 'all 0.2s',
          }}
        >
          {/* Joystick icon */}
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="6" cy="6" r="4"/>
            <path d="M6 10v8"/>
            <path d="M2 6h8"/>
            <circle cx="18" cy="18" r="3"/>
            <path d="M18 21v2"/>
            <path d="M15 18h6"/>
          </svg>
        </button>
        )}
        {showTeleopPad && (
          <div className="glass-card" style={{ padding: 20, marginTop: 12, minWidth: 220 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
              <div className="label-xs">Drive Controls</div>
              <button
                onClick={() => setShowTeleopPad(false)}
                title="Minimize drive controls"
                style={{
                  background: 'none', border: 'none', color: 'var(--muted)', cursor: 'pointer',
                  padding: '4px', display: 'flex', alignItems: 'center', justifyContent: 'center',
                  fontSize: 16,
                }}
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <polyline points="6 9 12 15 18 9"/>
                </svg>
              </button>
            </div>
            <TeleopPad connected={connected} compact />
          </div>
        )}
      </div>

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
                  onNavPoseSet?.(true)
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
})

DashboardHomeComponent.displayName = 'DashboardHome'
export default DashboardHomeComponent
