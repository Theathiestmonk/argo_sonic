import { useState, useEffect, useCallback, useRef, forwardRef, useImperativeHandle } from 'react'
import { ros } from '../ros'
import LiveMap from './LiveMap'
import TeleopPad from './TeleopPad'
import { friendlyNavMessage, TELEOP_BADGE } from '../friendlyNames'
import { Hero, TableCards, TelemetryStrip, RobotStatusCard, ActiveOrdersCard, PosMenuCard, normalizeOrders, usePosMenu } from '../ui/cards'
import * as Ic from '../ui/icons'
import { money } from '../ui/format'
import FreeRoamPanel from './FreeRoamPanel'

// React port of frontend/public/dashboard.html's layout and copy — same
// stats row, same "Saved Places" grid, same Recent Activity / Alerts
// panels. Places come from GET /waypoints/<selectedMap> (live, per-map)
// instead of localStorage. Navigation itself is never triggered from here
// directly — trips are started via POST /nav/goto or /goal_pose, and this
// component only displays status polled back from those. Same plain
// '$'-prefixed formatting TablesPanel.jsx uses for the same reason — no
// shared access to menu-data.js's currency/tax settings here.


// A waypoint's JSON key (e.g. "3") is just its arbitrary position in the
// waypoints/<map>.json file — it has NO guaranteed relationship to the
// table's actual number. nav_bridge's lookup ("Table {N}")
// and Postgres's service_points.label both expect the real number embedded
// in the waypoint's own name ("Table 3" -> "3"). Sending the raw key
// instead (as this used to) silently routed to whichever OTHER table
// happened to share that number as its name on a fully-populated map, and
// failed outright on a map that didn't have that many waypoints yet — used
// everywhere a table needs identifying (dispatch, order lookup, busy/active
// status) instead of the raw key.
function tableNumberFromName(name, fallbackKey) {
  const m = String(name || '').match(/\d+/)
  return m ? m[0] : fallbackKey
}

const DashboardHomeComponent = forwardRef(({ launcherUrl, selectedMap, connected, showToast, onSetInitialPose, mapData, robotPose, poseStale, plannedPath, driveTelemetry, sensorDistances, shieldStatus, battery, onAddMap, onFreeRoam, onOpenSettings, onActivityToggle, onNavInitializing, onNavReady, onNavPoseSet, onNavProgress, onNavRunning, onViewOrders, onSelectMap, menuApi }, ref) => {
  const [tables, setTables]         = useState({})
  const [poseMode, setPoseMode]     = useState(false)   // pose-estimate drag mode on the always-visible map card
  const [goalSetMode, setGoalSetMode] = useState(false)
  const [patrolSetMode, setPatrolSetMode] = useState(false)
  const [curPos, setCurPos]         = useState('Home')
  const [curStatus, setCurStatus]   = useState('Idle')
  // Blue goal marker on the map — set whenever a table action, "Navigate",
  // or "Go to kitchen" is clicked (see goToDestination
  // below), cleared once that trip is no longer running (below).
  const [goalMarker, setGoalMarker] = useState(null)
  const [taskLabel, setTaskLabel]   = useState('—')
  const [activity, setActivity]     = useState([])
  const [greeting, setGreeting]     = useState('Good day')
  const [time, setTime]             = useState('—')
  const [navGotoStatus, setNavGotoStatus] = useState({ running: false, destination: null, phase: null, phase_text: null })
  const [orders, setOrders]         = useState({})
  const [showTeleopPad, setShowTeleopPad] = useState(false)
  const [showActivityPanel, setShowActivityPanel] = useState(false)
  const [selectedTable, setSelectedTable] = useState(null)  // {key, name, x, y} when a table is clicked on map
  const [modelLoading, setModelLoading] = useState(false)
  const [freeRoamActive, setFreeRoamActive] = useState(false)
  const [freeRoamStats, setFreeRoamStats] = useState({ goalsReached: 0, goalsFailed: 0 })
  const [availableMaps, setAvailableMaps] = useState([])

  // Fetch available maps from launcher
  useEffect(() => {
    let cancelled = false
    const loadMaps = async () => {
      try {
        const response = await fetch(`${launcherUrl}/maps`)
        if (!cancelled) {
          const data = await response.json()
          setAvailableMaps(data.maps || [])
        }
      } catch (err) {
        console.error('Failed to load maps:', err)
      }
    }
    loadMaps()
    return () => { cancelled = true }
  }, [launcherUrl])

  useImperativeHandle(ref, () => ({
    toggleActivityPanel: () => setShowActivityPanel(prev => !prev),
    startNav: startNav,
    stopNav: stopNav,
    estop: estop,
    setPoseMode: setPoseMode,
    scrollToOrders: () => document.getElementById('a-orders')?.scrollIntoView({ behavior: 'smooth', block: 'center' }),
  }))

  // Nav2 + SLAM-localization stack — this is what actually lets a goal reach
  // the robot; picking a map here only decides which waypoints.json to read.
  //
  // navState/navMap seed from the last value cached in localStorage rather
  // than a hardcoded 'unknown' — purely so a hard refresh shows the
  // last-known state immediately instead of a blank/spinner flash while
  // the first /status poll is in flight. This is NEVER the source of
  // truth: checkNav() below fires on mount regardless and overwrites
  // whatever was seeded here with the real backend answer within one
  // round trip, and every subsequent poll keeps the cache in sync. If the
  // robot's real state changed while this tab was closed (crashed,
  // stopped from another device), the cache is stale for at most that one
  // round trip, then self-corrects.
  const NAV_STATE_CACHE_KEY = 'argo_nav_state_cache'
  const readNavStateCache = () => {
    try {
      const raw = localStorage.getItem(NAV_STATE_CACHE_KEY)
      if (!raw) return { navState: 'unknown', navMap: null }
      const parsed = JSON.parse(raw)
      return {
        navState: ['unknown', 'starting', 'running', 'stopped'].includes(parsed.navState) ? parsed.navState : 'unknown',
        navMap: typeof parsed.navMap === 'string' ? parsed.navMap : null,
      }
    } catch {
      return { navState: 'unknown', navMap: null }
    }
  }
  const [navState, setNavState] = useState(() => readNavStateCache().navState) // 'unknown'|'starting'|'running'|'stopped'
  const [navMap, setNavMap]     = useState(() => readNavStateCache().navMap)
  const navPollRef = useRef(null)
  const navFailRef = useRef(0)
  // Start/Stop race guard. Every Start/Stop bumps the epoch; a /status reply
  // that was requested before the latest transition (or arrives while a stop is
  // in flight) is discarded, so a late "running" can't undo a Stop or a late
  // "stopped" can't undo a Start.
  const navEpochRef = useRef(0)
  const navStoppingRef = useRef(false)

  // Keep the cache in sync with every state change, live-poll-driven or
  // user-triggered alike — write-through, not a separate save step.
  useEffect(() => {
    if (navState === 'unknown') return   // don't clobber a real cached value with the pre-first-fetch default
    try {
      localStorage.setItem(NAV_STATE_CACHE_KEY, JSON.stringify({ navState, navMap }))
    } catch {
      // localStorage unavailable (private browsing, quota) — cache is
      // purely a UX nicety, never load-bearing, so just skip it silently.
    }
  }, [navState, navMap])

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

  // The map navigation is actually running is the source of truth: waypoints, orders and the table layout
  // must all come from it (a stale selection in localStorage would show another map's tables on this map).
  useEffect(() => {
    if (navState === 'running' && navMap && navMap !== selectedMap) onSelectMap?.(navMap)
  }, [navState, navMap, selectedMap, onSelectMap])

  // READY only once the launch script itself reports every node up (/nav_progress
  // status READY). The /navigate_to_pose action server alone appears long before
  // the camera, safety shield and patrol manager are launched.
  const [navProgress, setNavProgress] = useState({ status: null, message: null, percent: 0 })
  // App passes an inline arrow (new identity every render); keep it in a ref so the
  // polling effect below isn't torn down and restarted on every progress update.
  const onNavProgressRef = useRef(onNavProgress)
  useEffect(() => { onNavProgressRef.current = onNavProgress })
  const navReady = navState === 'running' && navMap === selectedMap && navActionReady && navProgress.status === 'READY'

  // Show "Getting ready" only while a start is genuinely in flight: the launcher
  // reports status OK (in progress) with a recent update. A leftover OK from a
  // run that was stopped/killed outside the UI goes stale and must not show.
  // Age comes from the robot's own clock (the browser clock may differ and made the
  // panel vanish). Between steps the progress file can briefly be empty (status null),
  // so treat null like "in progress" while the stack process is up — otherwise the
  // panel hides after a step and reappears on the next one.
  const progAgeS = navProgress.age ?? 0
  const startupBusy = navProgress.status === 'OK' || navProgress.status === null
  const showStartup = !navReady && (
    navState === 'starting' ||
    (navState === 'running' && navMap === selectedMap && startupBusy && progAgeS < 180))

  // Tell the parent whether a stack exists at all, so Stop Argo is only clickable when there is something to stop.
  useEffect(() => { onNavRunning?.(navState !== 'stopped') }, [navState, onNavRunning])

  // Update parent about nav readiness state
  useEffect(() => {
    onNavReady?.(navReady)
    // Keep initializing=true until nav is fully ready OR it's confirmed
    // stopped. The third branch below matters just as much as the first
    // two: App.jsx's navInitializing defaults to false and is otherwise
    // only ever set true by the Start button's own onClick — so on a hard
    // refresh (or first mount) while the stack is already running in the
    // background, nothing told it that. The button fell through to its
    // "not initializing, not ready" case and showed "Start Argo" — as if
    // nothing were running — for however long it took navReady to resolve
    // (a /status poll, then a separate /rosapi/action_servers poll).
    // Treating 'unknown'/'starting'/'running'-but-not-yet-ready as
    // "initializing" closes that gap: the button shows the real
    // in-progress state instead of inviting a redundant Start click.
    if (navState === 'stopped') {
      onNavInitializing?.(false)
    } else if (navReady || navProgress.status === 'ERROR') {
      // ERROR: the launch script gave up — release the Start button so the
      // user can retry instead of staring at a disabled "Starting…" forever.
      onNavInitializing?.(false)
    } else {
      onNavInitializing?.(true)
    }
  }, [navReady, onNavReady, navState, navProgress.status, onNavInitializing])

  // Real step-by-step progress from sh/start_argo_nav_ui.sh itself (see
  // backend/launcher.py's GET /nav_progress) — without this, a failure
  // partway through the ~90s+ startup looked identical to it just still
  // being slow: an indefinite "Waiting for Nav2" spinner with no way to
  // tell which node broke or that it broke at all, short of SSHing in.
  useEffect(() => {
    if (navState !== 'running' || navMap !== selectedMap) {
      setNavProgress({ status: null, message: null, percent: 0 })
      onNavProgressRef.current?.(null, 0)
      return
    }
    let cancelled = false
    const check = () => {
      fetch(`${launcherUrl}/nav_progress`)
        .then(r => r.json())
        .then(d => { if (!cancelled) { setNavProgress({ ...d, message: friendlyNavMessage(d.message) }); onNavProgressRef.current?.(friendlyNavMessage(d.message) || null, d.percent || 0) } })
        .catch(() => {})
    }
    check()
    const id = setInterval(check, 2000)
    return () => { cancelled = true; clearInterval(id) }
  }, [navState, navMap, selectedMap, launcherUrl])

  const checkNav = useCallback(() => {
    const epoch = navEpochRef.current
    const stale = () => epoch !== navEpochRef.current || navStoppingRef.current
    fetch(`${launcherUrl}/status`)
      .then(r => r.json())
      .then(d => {
        if (stale()) return
        navFailRef.current = 0
        setNavState(d.running ? 'running' : 'stopped')
        setNavMap(d.running ? d.map : null)
      })
      .catch(() => {
        if (stale()) return
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
    if (navStoppingRef.current) { showToast('Still stopping — try again in a moment', 'info'); onNavInitializing?.(false); return false }
    navEpochRef.current += 1
    setNavState('starting')
    showToast(`Starting navigation on ${selectedMap}…`, 'info')
    try {
      const r = await fetch(`${launcherUrl}/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: 'navigate', map: selectedMap }),
      })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      if (navPollRef.current) clearInterval(navPollRef.current)
      navPollRef.current = setInterval(checkNav, 3000)
      return true
    } catch (e) {
      setNavState('stopped')
      onNavInitializing?.(false)
      showToast(e?.message?.startsWith('HTTP') ? `Launcher refused to start (${e.message})` : 'Could not reach launcher — is launcher.py running on the robot?', 'danger')
      return false
    }
  }

  const stopNav = async () => {
    // All four status-pill states in the header (navigating / rosbridge
    // unreachable / error / still starting up) wire onClick straight to
    // this function, and read as passive status text rather than a
    // button — so a single misclick (e.g. tapping the "waiting for
    // Nav2" spinner out of impatience) used to kill the stack instantly,
    // including mid-startup before it ever had a chance to finish.
    if (!window.confirm('Stop Argo now? Motors and navigation will be switched off.')) return false
    // Freeze status polling and drop any in-flight replies until the stop settles.
    navStoppingRef.current = true
    navEpochRef.current += 1
    if (navPollRef.current) { clearInterval(navPollRef.current); navPollRef.current = null }
    try {
      const r = await fetch(`${launcherUrl}/stop`, { method: 'POST' })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      setNavState('stopped'); setNavMap(null); setEstopped(false)
      setNavProgress({ status: null, message: null, percent: 0 })
      onNavInitializing?.(false); onNavReady?.(false); onNavPoseSet?.(false); onNavProgress?.(null, 0)
      showToast('Argo stopped', 'info')
      return true
    } catch (e) {
      showToast(e?.message?.startsWith('HTTP') ? `Stop failed (${e.message})` : 'Could not reach launcher', 'danger')
      return false
    } finally {
      navStoppingRef.current = false
      navEpochRef.current += 1
      checkNav()   // resync with what the launcher really reports
    }
  }

  useEffect(() => {
    // A slow response for a map we've already switched away from must never overwrite the current one.
    let stale = false
    setTables({})
    fetch(`${launcherUrl}/waypoints/${selectedMap}`)
      .then(r => r.json())
      .then(d => { if (!stale) setTables(d || {}) })
      .catch(() => { if (!stale) setTables({}) })
    return () => { stale = true }
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

  // Status of the current/last plain single-destination trip (POST
  // /nav/goto — e.g. "Go to kitchen"), polled.
  useEffect(() => {
    let cancelled = false
    const load = () => {
      fetch(`${launcherUrl}/nav/goto/status`)
        .then(r => r.json())
        .then(d => { if (!cancelled) setNavGotoStatus(d) })
        .catch(() => {})
    }
    load()
    const id = setInterval(load, 3000)
    return () => { cancelled = true; clearInterval(id) }
  }, [launcherUrl])


  // Left rail's Status stat card, driven entirely by real backend state now
  // (no more local nav simulation): the plain /nav/goto trip's phase, else Idle.
  useEffect(() => {
    if (navGotoStatus.running && navGotoStatus.phase_text) {
      setCurStatus(navGotoStatus.phase_text)
      setCurPos(navGotoStatus.destination || curPos)
    } else if (!navGotoStatus.running) {
      setCurStatus('Idle')
      setGoalMarker(null)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [navGotoStatus])

  // Same polling pattern — independent state, own endpoint,
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

  // Open orders per table + today's totals come from the POS order ledger (backend/pos_api.py); when that service
  // isn't running, fall back to the launcher's database-backed /orders so the card still works.
  const [today, setToday] = useState(null)
  useEffect(() => {
    let cancelled = false
    const day0 = new Date(); day0.setHours(0, 0, 0, 0)
    const load = async () => {
      try {
        const [bt, sm] = await Promise.all([
          fetch(`${menuApi}/orders/by-table`, { cache: 'no-store' }).then(r => r.json()),
          fetch(`${menuApi}/orders/summary?from=${encodeURIComponent(day0.toISOString())}&to=${encodeURIComponent(new Date(day0.getTime() + 86400000).toISOString())}`, { cache: 'no-store' }).then(r => r.json()),
        ])
        if (!cancelled) { setOrders(bt || {}); setToday(sm) }
      } catch {
        try { const d = await fetch(`${launcherUrl}/orders/${selectedMap}`).then(r => r.json()); if (!cancelled) { setOrders(d || {}); setToday(null) } } catch { /* offline */ }
      }
    }
    load()
    const id = setInterval(load, 3000)
    return () => { cancelled = true; clearInterval(id) }
  }, [launcherUrl, selectedMap, menuApi])

  const clearOrder = (key) => {
    setOrders(prev => { const next = { ...prev }; delete next[key]; return next })            // optimistic
    fetch(`${menuApi}/orders/clear-table`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ table: String(key), actor: 'staff' }) })
      .then(r => { if (!r.ok) throw new Error(); return r.json() })
      .catch(() => fetch(`${launcherUrl}/orders/${selectedMap}/${key}`, { method: 'DELETE' }).then(r => r.json()).then(d => { if (!d.ok) throw new Error() }).catch(() => showToast('Could not clear order', 'danger')))
  }

  // "Dispatch": mark the table's ready order as out for delivery in the ledger, then send the robot there.
  const dispatchOrder = (tableName) => {
    const num = String(parseInt((tableName.match(/\d+/) || [''])[0], 10))
    const ids = orders[num]?.ids
    if (ids?.length) fetch(`${menuApi}/orders/${ids[ids.length - 1]}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status: 'out_for_delivery', actor: 'staff' }) }).catch(() => {})
    goToDestination(tableName)
  }

  useEffect(() => {
    const tick = () => {
      const now = new Date()
      const h = now.getHours()
      setGreeting(h >= 5 && h < 12 ? 'Good Morning' : h >= 12 && h < 17 ? 'Good Afternoon' : h >= 17 && h < 21 ? 'Good Evening' : 'Good Night')
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

  // Plain, conversation-free trip to any named waypoint, backend-managed
  // via POST /nav/goto (shells out to nav_bridge.py directly). Status polled via navGotoStatus above.
  // Used both by "Go to kitchen" and each table card's own "Navigate"
  // button (a pure nav-stack smoke test, deliberately bypassing Sonic).
  const goToDestination = useCallback((name) => {
    if (navGotoStatus.running) return
    const dest = destinations.find(d => d.name === name)
    if (!dest) {
      showToast(`${name} not found`, 'danger')
      return
    }
    setGoalMarker({ x: dest.x, y: dest.y })
    // Publish goal to /goal_pose topic
    ros.publish('/goal_pose', 'geometry_msgs/PoseStamped', {
      header: { frame_id: 'map', stamp: { sec: 0, nanosec: 0 } },
      pose: { position: { x: dest.x, y: dest.y, z: 0 }, orientation: { x: 0, y: 0, z: 0, w: 1 } },
    })
    showToast(`Navigating to ${name}`, 'ok')
    addActivity(name, 'Navigate')
  }, [destinations, showToast, addActivity, ros])

  // No confirm() dialog here on purpose, unlike stopNav() — this is a real
  // emergency stop (cuts motor commands immediately if the robot is
  // physically misbehaving); a confirmation dialog would defeat the point
  // of "immediately". Kills only serial_bridge, not the rest of the nav
  // stack (SLAM/planner/etc. keep running), so resuming doesn't require a
  // full restart.
  const sendZeroVelocity = () => {
    if (!ros?.connection?.isConnected) return
    try {
      const cmdVelTopic = ros.topic('/cmd_vel', 'geometry_msgs/Twist')
      cmdVelTopic?.publish({ linear: { x: 0, y: 0, z: 0 }, angular: { x: 0, y: 0, z: 0 } })
    } catch (e) {
      console.log('Could not send zero velocity:', e)
    }
  }

  const estop = () => {
    sendZeroVelocity()
    fetch(`${launcherUrl}/estop`, { method: 'POST' })
      .then(r => { if (!r.ok) throw new Error(r.status); setEstopped(true); showToast('E-STOP: motors cut', 'danger') })
      .catch(() => showToast('Could not reach launcher for E-STOP', 'danger'))
  }

  // Joystick button: bring up serial_bridge alone so manual driving works
  // without waiting for SLAM/Nav2. Fire-and-forget — the pad opens immediately.
  const [teleopStatus, setTeleopStatus] = useState(null) // null|'starting'|'started'|'already_running'|'nav_starting'|'estopped'|'error'
  const startTeleop = () => {
    setShowTeleopPad(true)
    setTeleopStatus('starting')
    fetch(`${launcherUrl}/teleop/start`, { method: 'POST' })
      .then(async r => {
        const d = await r.json().catch(() => ({}))
        setTeleopStatus(d.status || 'error')
        if (d.status === 'estopped') showToast('Emergency stop is active — resume the motors to drive', 'danger')
        else if (d.status === 'started') showToast('Drive controls ready', 'ok')
        else if (d.status === 'already_running') showToast('Drive controls ready', 'ok')
        else if (d.status === 'nav_starting') showToast('Argo is still starting up — driving will be available shortly', 'ok')
      })
      .catch(() => { setTeleopStatus('error'); showToast('Could not reach the robot to enable driving', 'danger') })
  }

  const stopTeleop = () => {
    setShowTeleopPad(false)
    setTeleopStatus(null)
    fetch(`${launcherUrl}/teleop/stop`, { method: 'POST' })
      .then(r => r.json())
      .then(d => {
        if (d.status === 'stopped') showToast('Drive controls closed — motors switched off', 'ok')
        else if (d.status === 'nav_running') showToast('Drive controls closed', 'ok')
        else showToast('Drive controls closed', 'ok')
      })
      .catch(() => showToast('Could not reach the robot to switch motors off', 'danger'))
  }

  const estopResume = () => {
    fetch(`${launcherUrl}/estop/resume`, { method: 'POST' })
      .then(r => { if (!r.ok) throw new Error(r.status); setEstopped(false); showToast('Motors resumed', 'ok') })
      .catch(() => showToast('Could not reach launcher to resume motors', 'danger'))
  }

  // Shared by the 2D canvas and the 3D viewer.
  const tableMarkerList = Object.entries(tables)
    .filter(([key]) => key !== '0')
    .map(([key, t]) => ({ key, name: t.name || `Table ${key}`, x: t.x, y: t.y }))
  // Goal/Patrol/Pose are click-on-the-2D-canvas tools.
  const canvasTools = connected

  // ───────────────────────── live data for the cards ─────────────────────────
  const menu = usePosMenu(launcherUrl, menuApi)
  const orderList = normalizeOrders(orders)
  const tableCount = tableMarkerList.filter(m => /table/i.test(m.name)).length
  const busyTables = new Set(orderList.map(o => String(parseInt(o.table, 10))))
  const orderTables = [...busyTables]
  const orderValue = orderList.reduce((sum, o) => sum + Number(o.total || 0), 0)
  const itemCount = orderList.reduce((n, o) => n + o.items.reduce((s, i) => s + (i.qty || 1), 0), 0)
  const delivering = orderList.filter(o => o.meta.tab === 'delivering').length

  const trip = navGotoStatus.running
    ? { title: 'Navigating', sub: navGotoStatus.destination ? `To ${navGotoStatus.destination}` : (navGotoStatus.phase_text || 'On a trip'), busy: true }
    : goalMarker || (plannedPath && plannedPath.length > 1)
      ? { title: 'Navigating', sub: 'To the selected goal', busy: true }
      : { title: 'Idle', sub: navReady ? 'Ready for a task' : 'Navigation not ready', busy: false }

  const hour = new Date().getHours()
  const subtitle = !connected ? 'Not connected to Argo — live data is unavailable.'
    : estopped ? 'Emergency stop is active — motors are cut.'
    : orderList.length === 0 ? 'No active orders right now.'
    : `${orderList.length} active order${orderList.length === 1 ? '' : 's'}${delivering ? ` · ${delivering} delivering` : ''}.`
  const kpis = today ? [
    { label: "Today's Revenue", icon: <Ic.Cutlery />, value: money(today.sales, menu.currency), note: today.open ? `${money(today.open_value, menu.currency)} open` : null },
    { label: 'Total Orders', icon: <Ic.Doc />, value: today.orders, note: today.cancelled ? `${today.cancelled} cancelled` : null },
    { label: 'Active Tables', icon: <Ic.TableIc />, value: tableCount ? `${busyTables.size} / ${tableCount}` : `${busyTables.size}`, bar: tableCount ? busyTables.size / tableCount : null },
  ] : [
    { label: 'Open Orders Value', icon: <Ic.Cutlery />, value: money(orderValue, menu.currency), note: itemCount ? `${itemCount} item${itemCount === 1 ? '' : 's'}` : null },
    { label: 'Active Orders', icon: <Ic.Doc />, value: orderList.length },
    { label: 'Active Tables', icon: <Ic.TableIc />, value: tableCount ? `${busyTables.size} / ${tableCount}` : `${busyTables.size}`, bar: tableCount ? busyTables.size / tableCount : null },
  ]

  // Per-map furniture layout (tables / serve table / printer / …): /layouts/<map>.json, optional.
  const [layout, setLayout] = useState(null)
  useEffect(() => {
    let stale = false
    setLayout(null)
    fetch(`/layouts/${selectedMap}.json`).then(r => (r.ok ? r.json() : null)).then(d => { if (!stale) setLayout(d?.objects ? d : null) }).catch(() => {})
    return () => { stale = true }
  }, [selectedMap])
  const mapWrapRef = useRef(null)
  const [panel, setPanel] = useState(() => { try { return localStorage.getItem('argo_live_panel') === 'tables' ? 'tables' : 'map' } catch { return 'map' } })
  const choosePanel = (p) => { setPanel(p); try { localStorage.setItem('argo_live_panel', p) } catch { /* private mode */ } }
  const goalTableName = goalMarker ? (tableMarkerList.find(m => Math.hypot(m.x - goalMarker.x, m.y - goalMarker.y) < 0.7)?.name ?? null) : null
  const [mapKey, setMapKey] = useState(0)
  const [actionsOpen, setActionsOpen] = useState(false)
  const fullscreen = () => { const el = mapWrapRef.current; if (!el) return; document.fullscreenElement ? document.exitFullscreen() : el.requestFullscreen?.() }

  const toolBtn = (label, on, onClick, { disabled = !canvasTools, title, cls = '' } = {}) => (
    <button className={`a-btn ${cls} ${on ? 'on' : ''}`} onClick={onClick} disabled={disabled} title={title}>{label}</button>
  )

  return (
    <div className="a-content fit" >
      <Hero greeting={greeting} night={hour >= 19 || hour < 5} subtitle={subtitle} kpis={kpis} />

      {/* ── Live map ── */}
      <section className="a-card a-mapcard" id="dash-activity">
        <div className="a-maphead">
          <h3>{panel === 'map' ? 'Live Map' : 'Tables'}</h3>
          <div className="a-seg">
            {[['map', 'Map'], ['tables', 'Tables']].map(([id, label]) => (
              <button key={id} className={panel === id ? 'on' : ''} onClick={() => choosePanel(id)} title={id === 'map' ? 'Floor plan with the robot' : 'One card per table with its live order'}>{label}</button>
            ))}
          </div>
          <div className="a-tools">
            <select className="a-select" value={selectedMap} disabled={navReady || availableMaps.length === 0}
              title={navReady ? 'Locked — stop navigation to switch maps' : 'Select the map to load'}
              onChange={(e) => { if (!navReady) { localStorage.setItem('argo_selected_map', e.target.value); window.location.reload() } }}>
              {availableMaps.length === 0 ? <option>{selectedMap}</option> : availableMaps.map(m => <option key={m} value={m}>{m}</option>)}
            </select>
            <div className="a-actwrap">
              <button className={`a-btn ${actionsOpen || goalSetMode || patrolSetMode || poseMode || freeRoamActive || showTeleopPad ? 'on' : ''}`}
                onClick={() => setActionsOpen(v => !v)} aria-expanded={actionsOpen} title="Map actions">Actions ▾</button>
              {actionsOpen && <>
                <div onClick={() => setActionsOpen(false)} style={{ position: 'fixed', inset: 0, zIndex: 58 }} />
                <div className="a-actpop" role="menu" onClick={() => setActionsOpen(false)}>
                  {panel === 'map' && <>
                    {toolBtn('Goal', goalSetMode, () => setGoalSetMode(v => !v), { title: 'Click the map to send Argo somewhere' })}
                    {toolBtn('Patrol', patrolSetMode, () => setPatrolSetMode(v => !v), { title: 'Set a patrol route' })}
                    {toolBtn('Stop patrol', false, () => { if (connected) { ros.publish('/patrol/stop', 'std_msgs/Empty', {}); showToast?.('Patrol stopped', 'ok') } }, { disabled: !connected, cls: 'danger', title: 'Stop the patrol route' })}
                    {toolBtn('Free Roam', freeRoamActive, () => { if (connected) { setFreeRoamActive(v => !v) } }, { disabled: !connected, title: 'Autonomous free-roam exploration' })}
                    {toolBtn(poseMode ? 'Cancel Pose' : 'Pose', poseMode, () => setPoseMode(v => !v), { title: !connected ? "Can't set pose — not connected" : 'Click where Argo is standing, then drag toward where it faces' })}
                  </>}
                  {toolBtn('Drive', showTeleopPad, () => (showTeleopPad ? stopTeleop() : startTeleop()), { disabled: false, title: 'Open the manual drive pad' })}
                  {panel === 'map' && <>
                    {toolBtn('Full screen', false, fullscreen, { disabled: false, title: 'Full screen' })}
                    {toolBtn('Reset view', false, () => { setMapKey(k => k + 1); showToast?.('Map view reset', 'info') }, { disabled: false, title: 'Reset the map zoom and position' })}
                  </>}
                </div>
              </>}
            </div>
          </div>
        </div>
        <div className="a-mapwrap" ref={mapWrapRef}>
          {panel === 'tables' ? (
            <TableCards tables={tableMarkerList} orders={orders} currency={menu.currency} goalTable={goalTableName}
              onSend={goToDestination} onClear={clearOrder} />
          ) :
            <LiveMap key={mapKey}
              mapData={mapData} robotPose={robotPose} robotStale={poseStale} goalPose={goalMarker} plannedPath={plannedPath}
              tableMarkers={tableMarkerList} orderTables={orderTables} layout={layout} connected={connected}
              poseEstimateMode={poseMode} goalSetMode={goalSetMode} patrolSetMode={patrolSetMode}
              onPoseEstimate={({ wx, wy, theta }) => { onSetInitialPose?.(wx, wy, theta); onNavPoseSet?.(true); setPoseMode(false); showToast?.('Pose set', 'ok') }}
              onGoalSet={({ wx, wy, theta }) => {
                setGoalMarker({ x: wx, y: wy }); setGoalSetMode(false)
                ros.publish('/goal_pose', 'geometry_msgs/PoseStamped', {
                  header: { frame_id: 'map', stamp: { sec: 0, nanosec: 0 } },
                  pose: { position: { x: wx, y: wy, z: 0 }, orientation: { x: 0, y: 0, z: Math.sin((theta ?? 0) / 2), w: Math.cos((theta ?? 0) / 2) } },
                })
                showToast?.('Navigating to goal', 'ok')
              }}
              onPatrolSet={({ wx, wy, theta }) => {
                setPatrolSetMode(false)
                ros.publish('/patrol/start', 'geometry_msgs/PoseStamped', {
                  header: { frame_id: 'map', stamp: { sec: 0, nanosec: 0 } },
                  pose: { position: { x: wx, y: wy, z: 0 }, orientation: { x: 0, y: 0, z: Math.sin(theta / 2), w: Math.cos(theta / 2) } },
                })
                showToast?.('Patrol route set', 'ok')
              }}
              onTableMarkerClick={(t, at) => setSelectedTable({ ...t, at })}
            />
          }
        </div>
        <TelemetryStrip drive={driveTelemetry} sensors={sensorDistances} shield={shieldStatus} />
      </section>

      {freeRoamActive && (
        <div className="argo-legacy" style={{ position: 'fixed', top: 70, right: 24, zIndex: 70, width: 400, maxHeight: '78vh', overflowY: 'auto', margin: 0, minHeight: 0, padding: 16, boxShadow: '0 20px 60px rgba(0,0,0,.35)' }}>
          <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 6 }}><button onClick={() => setFreeRoamActive(false)} style={{ color: 'var(--muted)', fontSize: 18 }}>✕</button></div>
          <FreeRoamPanel mapData={mapData} robotPose={robotPose} connected={connected} showToast={showToast}
            launcherUrl={launcherUrl} mapName={selectedMap || 'office_map'} />
        </div>
      )}

      {/* ── Robot / orders / menu ── */}
      <div className="a-row3">
        <RobotStatusCard connected={connected} battery={battery} task={trip} estopped={estopped} onResume={estopResume}
          navSteps={showStartup ? (navProgress.steps || []) : null} navPercent={showStartup ? (navProgress.percent || 0) : 0} />
        <ActiveOrdersCard list={orderList} currency={menu.currency} onDispatch={dispatchOrder} onClear={clearOrder} onViewAll={onViewOrders} />
        <PosMenuCard menu={menu} />
      </div>

      {/* Activity / alerts popover (bell in the top bar) */}
      {showActivityPanel && (
        <div className="a-float" style={{ top: 70, right: 28, width: 340, padding: 18 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
            <b style={{ fontFamily: 'var(--font-heading)', fontSize: 16 }}>Recent Activity</b>
            <button onClick={() => setShowActivityPanel(false)} style={{ color: 'var(--a-muted)', fontSize: 18 }}>✕</button>
          </div>
          {activity.length === 0
            ? <div style={{ color: 'var(--a-muted)', fontSize: 13 }}>No dashboard-initiated trips yet.</div>
            : activity.map((a, i) => (
              <div key={i} style={{ display: 'flex', justifyContent: 'space-between', padding: '8px 0', borderTop: i ? '1px solid var(--a-line)' : 'none', fontSize: 13 }}>
                <span>{a.task} → <b>{a.dest}</b></span><span style={{ color: 'var(--a-muted)' }}>{a.time}</span>
              </div>))}
          <div style={{ borderTop: '1px solid var(--a-line)', marginTop: 10, paddingTop: 10, fontSize: 13, color: estopped || !connected ? 'var(--a-danger)' : 'var(--a-ok)', fontWeight: 600 }}>
            {!connected ? 'Not connected to Argo' : estopped ? 'Emergency stop active' : 'Argo is online'}
          </div>
        </div>
      )}

      {/* Drive pad — floats bottom-right while open (opened from the Drive button in the map toolbar) */}
      {showTeleopPad && (
        <div style={{ position: 'fixed', bottom: 24, right: 24, zIndex: 60, background: '#17140f', color: '#fff', borderRadius: 18, padding: 20, minWidth: 220, boxShadow: '0 16px 48px rgba(0,0,0,.35)' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <div className="label-xs">Drive Controls</div>
              {teleopStatus && (() => { const m = TELEOP_BADGE[teleopStatus] || [teleopStatus, 'var(--muted)']; return <span style={{ fontSize: 9.5, fontWeight: 700, color: m[1] }}>● {m[0]}</span> })()}
            </div>
            <button onClick={stopTeleop} title="Close drive controls" style={{ color: 'var(--muted)', padding: 4, fontSize: 16 }}>✕</button>
          </div>
          <TeleopPad connected={connected} compact />
        </div>
      )}

      {/* Table popover — opens where the table was clicked on the map */}
      {selectedTable && (() => {
        const orderTableNo = tableNumberFromName(selectedTable.name, selectedTable.key)
        const raw = orders[orderTableNo]
        const order = raw && raw.items && raw.items.length ? raw : null
        const x = Math.min((selectedTable.at?.x ?? 300) + 18, window.innerWidth - 330)
        const y = Math.min((selectedTable.at?.y ?? 200) + 12, window.innerHeight - 260)
        return (
          <>
            <div onClick={() => setSelectedTable(null)} style={{ position: 'fixed', inset: 0, zIndex: 59 }} />
            <div className="a-float" style={{ left: Math.max(12, x), top: Math.max(12, y), width: 300, padding: 18, zIndex: 60 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
                <b style={{ fontFamily: 'var(--font-heading)', fontSize: 18 }}>{selectedTable.name}</b>
                <button onClick={() => setSelectedTable(null)} style={{ color: 'var(--a-muted)', fontSize: 18 }}>✕</button>
              </div>
              <div style={{ fontSize: 12, color: 'var(--a-muted)', marginBottom: 12 }}>📍 {Number(selectedTable.x).toFixed(1)} m, {Number(selectedTable.y).toFixed(1)} m</div>
              {order ? (
                <div style={{ marginBottom: 12, paddingBottom: 12, borderBottom: '1px dashed var(--a-line-strong)' }}>
                  {order.items.slice(0, 4).map((it, i) => (
                    <div key={it.id || i} style={{ display: 'flex', justifyContent: 'space-between', fontSize: 13, marginBottom: 3 }}>
                      <span>{it.qty}× {it.name}</span><span>{money(it.qty * it.price, menu.currency)}</span>
                    </div>))}
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 6, fontWeight: 700, fontSize: 13.5 }}><span>Total</span><span>{money(order.total, menu.currency)}</span></div>
                </div>
              ) : <div style={{ fontSize: 13, color: 'var(--a-muted)', marginBottom: 12 }}>No open order.</div>}
              <button className="a-btn gold" style={{ width: '100%', justifyContent: 'center' }} onClick={() => { goToDestination(selectedTable.name); setSelectedTable(null) }}>Navigate here</button>
            </div>
          </>
        )
      })()}
    </div>
  )
})

DashboardHomeComponent.displayName = 'DashboardHome'
export default DashboardHomeComponent
