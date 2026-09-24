import { useState, useEffect, useCallback, useRef, forwardRef, useImperativeHandle } from 'react'
import { ros } from '../ros'
import RadialNav from './RadialNav'
import MapCanvas from './MapCanvas'
import TeleopPad from './TeleopPad'
import TelemetryCard from './TelemetryCard'
import Robot3DViewer from './Robot3DViewer'
import FreeRoamPanel from './FreeRoamPanel'

// React port of frontend/public/dashboard.html's layout and copy — same
// stats row, same "Saved Places" grid, same Recent Activity / Alerts
// panels. Places come from GET /waypoints/<selectedMap> (live, per-map)
// instead of localStorage. Navigation itself is never triggered from here
// directly — every trip is either a table action (below, backend-managed by
// main_agent.py via POST /voice/start) or the "Go to kitchen" button
// (backend-managed by POST /nav/goto) — this component only displays status
// polled back from those, it never publishes a Nav2 goal itself. Same plain
// '$'-prefixed formatting TablesPanel.jsx uses for the same reason — no
// shared access to menu-data.js's currency/tax settings here.
const money = (n) => '$' + Number(n || 0).toFixed(2)

// A waypoint's JSON key (e.g. "3") is just its arbitrary position in the
// waypoints/<map>.json file — it has NO guaranteed relationship to the
// table's actual number. main_agent.py's nav_bridge lookup ("Table {N}")
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

// Maps a table-card task to the voice action backend/launcher.py's
// POST /voice/start expects.
const TASK_TO_ACTION = {
  'Take order':   'order',
  'Deliver':      'deliver',
  'Billing':      'bill',
  'Room service': 'room_service',
}

// Direct per-card buttons on a Saved Place — [task, button label].
const TABLE_ACTIONS = [
  ['Take order',   'Take Order'],
  ['Deliver',      'Deliver'],
  ['Billing',      'Send Bill'],
]

const DashboardHomeComponent = forwardRef(({ launcherUrl, selectedMap, connected, showToast, onSetInitialPose, mapData, robotPose, plannedPath, driveTelemetry, sensorDistances, onAddMap, onFreeRoam, onOpenSettings, onActivityToggle, onNavInitializing, onNavReady, onNavPoseSet, onNavProgress }, ref) => {
  const [tables, setTables]         = useState({})
  const [poseMode, setPoseMode]     = useState(false)   // pose-estimate drag mode on the always-visible map card
  const [goalSetMode, setGoalSetMode] = useState(false)
  const [patrolSetMode, setPatrolSetMode] = useState(false)
  const [curPos, setCurPos]         = useState('Home')
  const [curStatus, setCurStatus]   = useState('Idle')
  // Blue goal marker on the map — set whenever a table action, "Navigate",
  // or "Go to kitchen" is clicked (see dispatchVoiceAction/goToDestination
  // below), cleared once that trip is no longer running (below).
  const [goalMarker, setGoalMarker] = useState(null)
  const [taskLabel, setTaskLabel]   = useState('—')
  const [activity, setActivity]     = useState([])
  const [greeting, setGreeting]     = useState('Good day')
  const [time, setTime]             = useState('—')
  const [voiceStatus, setVoiceStatus] = useState({ running: false, action: null, map: null, table: null, phase: null, phase_text: null })
  const [navGotoStatus, setNavGotoStatus] = useState({ running: false, destination: null, phase: null, phase_text: null })
  const [orders, setOrders]         = useState({})
  const [showTeleopPad, setShowTeleopPad] = useState(false)
  const [showActivityPanel, setShowActivityPanel] = useState(false)
  const [showTranscript, setShowTranscript] = useState(false)
  const [transcript, setTranscript] = useState({ session_id: null, started_at: null, turns: [] })
  const [selectedTable, setSelectedTable] = useState(null)  // {key, name, x, y} when a table is clicked on map
  const transcriptBottomRef = useRef(null)
  const [shadowColor, setShadowColor] = useState(() => {
    try {
      return localStorage.getItem('shadowColor') || '#e2b35c'
    } catch {
      return '#e2b35c'
    }
  })
  const [modelLoading, setModelLoading] = useState(false)
  const [showRobotSettings, setShowRobotSettings] = useState(false)
  const [freeRoamActive, setFreeRoamActive] = useState(false)
  const [freeRoamStats, setFreeRoamStats] = useState({ goalsReached: 0, goalsFailed: 0 })

  // Save shadowColor to localStorage
  useEffect(() => {
    try {
      localStorage.setItem('shadowColor', shadowColor)
    } catch {
      // localStorage unavailable
    }
  }, [shadowColor])

  useImperativeHandle(ref, () => ({
    toggleActivityPanel: () => setShowActivityPanel(prev => !prev),
    startNav: startNav,
    stopNav: stopNav,
    estop: estop,
    setPoseMode: setPoseMode,
  }))

  // Close settings on outside click
  useEffect(() => {
    const handleClickOutside = (e) => {
      if (showRobotSettings && !e.target.closest('[data-robot-settings]')) {
        setShowRobotSettings(false)
      }
    }
    document.addEventListener('click', handleClickOutside)
    return () => document.removeEventListener('click', handleClickOutside)
  }, [showRobotSettings])

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

  const navReady = navState === 'running' && navMap === selectedMap && navActionReady

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
    } else if (navReady) {
      onNavInitializing?.(false)
    } else {
      onNavInitializing?.(true)
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
      onNavProgress?.(null)
      return
    }
    let cancelled = false
    const check = () => {
      fetch(`${launcherUrl}/nav_progress`)
        .then(r => r.json())
        .then(d => { if (!cancelled) { setNavProgress(d); onNavProgress?.(d.message || null) } })
        .catch(() => {})
    }
    check()
    const id = setInterval(check, 2000)
    return () => { cancelled = true; clearInterval(id) }
  }, [navState, navMap, selectedMap, launcherUrl, onNavProgress])

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

  // Is Sonic currently mid-conversation at some table? Same poll pattern as
  // TablesPanel.jsx's identical hook — used to show a busy banner and avoid
  // firing a second voice session the backend would just 409 anyway. Also
  // carries main_agent.py's own real phase/phase_text now (see
  // report_phase() in sonic/main_agent.py) — this is the sole source of
  // truth for "moving/arrived/taking order" status, not anything simulated
  // locally.
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

  // Voice transcript (GET /voice/transcript) — only polled while the panel
  // is actually open, unlike voiceStatus above which other UI state depends
  // on regardless of visibility.
  useEffect(() => {
    if (!showTranscript) return
    let cancelled = false
    const load = () => {
      fetch(`${launcherUrl}/voice/transcript`)
        .then(r => r.json())
        .then(d => { if (!cancelled) setTranscript(d) })
        .catch(() => {})
    }
    load()
    const id = setInterval(load, 3000)
    return () => { cancelled = true; clearInterval(id) }
  }, [launcherUrl, showTranscript])

  // Auto-scroll to the latest turn whenever the transcript updates.
  useEffect(() => {
    transcriptBottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [transcript])

  // Status of the current/last plain single-destination trip (POST
  // /nav/goto — e.g. "Go to kitchen"), same poll pattern as voiceStatus.
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
  // (no more local nav simulation): prefer main_agent.py's own phase while a
  // voice session is active, else the plain /nav/goto trip's phase, else Idle.
  useEffect(() => {
    if (voiceStatus.running && voiceStatus.phase_text) {
      setCurStatus(voiceStatus.phase_text)
      setCurPos(voiceStatus.table || curPos)   // already the full waypoint name, e.g. "Table 3" or "table 1"
      setTaskLabel(voiceStatus.action || taskLabel)
    } else if (navGotoStatus.running && navGotoStatus.phase_text) {
      setCurStatus(navGotoStatus.phase_text)
      setCurPos(navGotoStatus.destination || curPos)
    } else if (!voiceStatus.running && !navGotoStatus.running) {
      setCurStatus('Idle')
      setGoalMarker(null)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [voiceStatus, navGotoStatus])

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

  // Starts a table's voice action. This is now the ONLY thing this call
  // does — main_agent.py owns the entire physical trip (Kitchen -> table ->
  // conversation -> Kitchen) once POST /voice/start spawns it, and reports
  // real status back via /voice/status's phase/phase_text (see the useEffect
  // above). No direct Nav2 goal, no local arrival simulation, no auto-return
  // timer here anymore — those were racing main_agent.py's own navigation,
  // which is what caused the double-navigation seen in testing.
  const dispatchVoiceAction = useCallback((tableLabel, task) => {
    const dest = destinations.find(d => d.name === tableLabel)
    if (!dest || !dest.key || dest.key === '0') return   // '0' = Home — no table context
    const action = TASK_TO_ACTION[task]
    if (!action) return

    // dest.name (the waypoint's own name, e.g. "Table 3"/"table 1") is what
    // gets sent, searched for, and echoed back — never dest.key, which is
    // just this waypoint's arbitrary position in the waypoints JSON and has
    // no guaranteed relationship to which table it actually is. See
    // table_ref_for_nav() in main_agent.py, which searches for this exact
    // name too, instead of reconstructing a guess from a number.
    if (voiceStatus.running && voiceStatus.table !== dest.name) {
      showToast('Sonic is busy with another table — wait for that session to finish', 'warn')
      return
    }
    if (navGotoStatus.running) {
      showToast('Argo is on a trip right now — wait for it to finish', 'warn')
      return
    }

    setGoalMarker({ x: dest.x, y: dest.y })

    fetch(`${launcherUrl}/voice/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, map: selectedMap, table: dest.name }),
    })
      .then(r => r.json())
      .then(d => { if (!d.ok) showToast(d.error === 'voice_session_busy' ? 'Sonic is busy with another table' : 'Could not start Sonic', 'danger') })
      .catch(() => showToast('Could not reach launcher for the voice session', 'danger'))

    addActivity(tableLabel, task)
  }, [destinations, showToast, addActivity, selectedMap, launcherUrl, voiceStatus, navGotoStatus])

  // Plain, conversation-free trip to any named waypoint, backend-managed
  // via POST /nav/goto (shells out to nav_bridge.py directly, same as
  // main_agent.py's own navigate_and_wait() — no NLP, no order flow, just
  // the raw NavigateToPose goal). Status polled via navGotoStatus above.
  // Used both by "Go to kitchen" and each table card's own "Navigate"
  // button (a pure nav-stack smoke test, deliberately bypassing Sonic).
  const goToDestination = useCallback((name) => {
    if (voiceStatus.running) {
      showToast('Sonic is busy with a table — wait for that session to finish', 'warn')
      return
    }
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

  // Cancels whichever table's order-processing session is currently
  // running and frees that table's lock. Only one voice session runs at a
  // time system-wide, so /voice/stop always targets the right one — no
  // table param needed. Used by the per-card Cancel button, which only
  // renders on the table that's actually locked (voiceStatus.table === label).
  const cancelVoice = useCallback((label) => {
    fetch(`${launcherUrl}/voice/stop`, { method: 'POST' })
      .then(r => r.json())
      .then(d => {
        if (d.ok) {
          showToast(`Cancelled — ${label} is free again`, 'warn')
          addActivity(label, 'Cancelled')
        } else {
          showToast('Could not cancel — try again', 'danger')
        }
      })
      .catch(() => showToast('Could not reach launcher', 'danger'))
  }, [launcherUrl, showToast, addActivity])

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
      .then(() => { setEstopped(true); showToast('E-STOP: motors cut', 'danger') })
      .catch(() => showToast('Could not reach launcher for E-STOP', 'danger'))
  }

  const estopResume = () => {
    fetch(`${launcherUrl}/estop/resume`, { method: 'POST' })
      .then(() => { setEstopped(false); showToast('Motors resumed', 'ok') })
      .catch(() => showToast('Could not reach launcher to resume motors', 'danger'))
  }

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

  return (
    <div style={{ display: 'grid', gridTemplateRows: 'auto 1fr', gridTemplateColumns: '100%', animation: 'slideUp 0.35s ease', height: '100vh', overflow: 'hidden', gap: 0 }}>

      {/* ── Greeting (Full Width) ── */}
      <section id="dash-overview" style={{ padding: '16px 20px', backgroundColor: 'rgba(0,0,0,0.08)', borderBottom: '1px solid rgba(255,255,255,0.06)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 20 }}>
          <div style={{ minWidth: 'auto' }}>
            <h2 style={{ fontFamily: 'var(--font-heading)', fontSize: 28, fontWeight: 800, letterSpacing: '-0.5px', marginBottom: 2 }}>{greeting} ☀️</h2>
            <p style={{ color: 'var(--muted)', fontSize: 13, margin: 0 }}>Here's what's happening in your restaurant.</p>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 10, flex: 1 }}>
            {[
              ['Location', curPos, '#fff', 18],
              ['Status', curStatus, curStatus === 'Moving' ? 'var(--gold)' : 'var(--ok)', 18],
              ["Today's Revenue", '₹18,240', 'var(--gold-bright)', 18],
              ['Total Orders', '56', 'var(--gold-bright)', 18],
            ].map(([k, v, color, valueSize]) => (
              <div key={k} className="glass-card" style={{ padding: '18px 16px' }}>
                <div style={{ fontSize: 10, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.03em', fontWeight: 700, marginBottom: 6 }}>{k}</div>
                <div style={{ fontSize: valueSize, fontWeight: 700, color, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{v}</div>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ── Main Content + Robot (70% / 30%) ── */}
      <div style={{ display: 'grid', gridTemplateColumns: '70% 30%', gap: 0, overflow: 'hidden', height: '100%' }}>

      {/* ── Main Content (70%) ── */}
      <main style={{ display: 'flex', flexDirection: 'column', gap: 0, padding: 0, overflow: 'hidden', backgroundColor: 'rgba(0,0,0,0.08)', height: '100%' }}>


        {/* Live Map */}
        <section id="dash-activity" style={{ height: 'calc(100vh - 140px)', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '12px 12px 8px 12px', borderBottom: '1px solid rgba(255,255,255,0.06)' }}>
            <div style={{ fontFamily: 'var(--font-heading)', fontSize: 14, fontWeight: 700 }}>Location Map</div>
            <div style={{ display: 'flex', gap: 8 }}>
              {/* Goal Mode */}
              <button
                onClick={() => connected && setGoalSetMode(v => !v)}
                  disabled={!connected}
                  title={goalSetMode ? 'Cancel goal setting' : 'Click on map to set goal'}
                  style={{
                    padding: '6px 10px', borderRadius: 6, fontSize: 12, fontWeight: 700,
                    background: goalSetMode ? 'rgba(127,168,232,0.2)' : 'rgba(127,168,232,0.08)',
                    border: `1px solid ${goalSetMode ? 'rgba(127,168,232,0.6)' : 'rgba(127,168,232,0.3)'}`,
                    color: goalSetMode ? '#7fa8e8' : 'rgba(127,168,232,0.6)',
                    cursor: connected ? 'pointer' : 'not-allowed',
                    opacity: connected ? 1 : 0.5,
                  }}
                >
                  🎯 Goal
                </button>

                {/* Patrol Mode */}
                <button
                  onClick={() => connected && setPatrolSetMode(v => !v)}
                  disabled={!connected}
                  title={patrolSetMode ? 'Cancel patrol setting' : 'Set patrol route'}
                  style={{
                    padding: '6px 10px', borderRadius: 6, fontSize: 12, fontWeight: 700,
                    background: patrolSetMode ? 'rgba(185,140,245,0.2)' : 'rgba(185,140,245,0.08)',
                    border: `1px solid ${patrolSetMode ? 'rgba(185,140,245,0.6)' : 'rgba(185,140,245,0.3)'}`,
                    color: patrolSetMode ? '#b98cf5' : 'rgba(185,140,245,0.6)',
                    cursor: connected ? 'pointer' : 'not-allowed',
                    opacity: connected ? 1 : 0.5,
                  }}
                >
                  🔄 Patrol
                </button>

                {/* Stop Patrol */}
                <button
                  onClick={() => {
                    if (connected) {
                      ros.publish('/patrol/stop', 'std_msgs/Empty', {})
                      showToast?.('Patrol stopped', 'ok')
                    }
                  }}
                  disabled={!connected}
                  title="Stop patrol route"
                  style={{
                    padding: '6px 10px', borderRadius: 6, fontSize: 12, fontWeight: 700,
                    background: 'rgba(240,59,59,0.08)',
                    border: '1px solid rgba(240,59,59,0.3)',
                    color: 'rgba(240,59,59,0.6)',
                    cursor: connected ? 'pointer' : 'not-allowed',
                    opacity: connected ? 1 : 0.5,
                  }}
                >
                  ⏹ Stop Patrol
                </button>

                {/* Free Roam */}
                <button
                  onClick={() => {
                    if (connected) {
                      setFreeRoamActive(v => !v)
                      showToast?.(freeRoamActive ? 'Free Roam closed' : 'Free Roam panel opened', 'ok')
                    }
                  }}
                  disabled={!connected}
                  title={freeRoamActive ? 'Close Free Roam' : 'Start autonomous free roam exploration'}
                  style={{
                    padding: '6px 10px', borderRadius: 6, fontSize: 12, fontWeight: 700,
                    background: freeRoamActive ? 'rgba(147,112,219,0.2)' : 'rgba(147,112,219,0.08)',
                    border: `1px solid ${freeRoamActive ? 'rgba(147,112,219,0.6)' : 'rgba(147,112,219,0.3)'}`,
                    color: freeRoamActive ? '#b98cf5' : 'rgba(147,112,219,0.6)',
                    cursor: connected ? 'pointer' : 'not-allowed',
                    opacity: connected ? 1 : 0.5,
                  }}
                >
                  🌀 Free Roam
                </button>

                {/* Pose Mode */}
                <button
                  onClick={() => connected && setPoseMode(v => !v)}
                  disabled={!connected}
                  title={!connected
                    ? "Can't set pose — not connected to Argo"
                    : (poseMode ? 'Cancel' : "Click where Argo is standing, then drag toward where it's facing")}
                  style={{
                    padding: '6px 10px', borderRadius: 6, fontSize: 12, fontWeight: 700,
                    background: poseMode ? 'rgba(59,240,155,0.2)' : 'rgba(59,240,155,0.08)',
                    border: `1px solid ${poseMode ? 'rgba(59,240,155,0.6)' : 'rgba(59,240,155,0.3)'}`,
                    color: poseMode ? '#3bf09b' : 'rgba(59,240,155,0.6)',
                    cursor: connected ? 'pointer' : 'not-allowed',
                    opacity: connected ? 1 : 0.5,
                  }}
                >
                  {poseMode ? '✕ Pose' : '📍 Pose'}
                </button>
              </div>
          </div>
          <div style={{ flex: 1, overflow: 'hidden', position: 'relative' }}>
            <MapCanvas
              mapData={mapData}
              robotPose={robotPose}
              goalPose={goalMarker}
              plannedPath={plannedPath}
              poseEstimateMode={poseMode}
              goalSetMode={goalSetMode}
              patrolSetMode={patrolSetMode}
              driveTelemetry={driveTelemetry}
              sensorDistances={sensorDistances}
              onPoseEstimate={({ wx, wy, theta }) => {
                onSetInitialPose?.(wx, wy, theta)
                onNavPoseSet?.(true)
                setPoseMode(false)
                showToast?.('Pose set', 'ok')
              }}
              onGoalSet={({ wx, wy }) => {
                setGoalMarker({ x: wx, y: wy })
                setGoalSetMode(false)
                // Publish goal to /goal_pose topic
                ros.publish('/goal_pose', 'geometry_msgs/PoseStamped', {
                  header: { frame_id: 'map', stamp: { sec: 0, nanosec: 0 } },
                  pose: { position: { x: wx, y: wy, z: 0 }, orientation: { x: 0, y: 0, z: 0, w: 1 } },
                })
                showToast?.('Navigating to goal', 'ok')
              }}
              onPatrolSet={({ wx, wy, theta }) => {
                setPatrolSetMode(false)
                // Publish patrol start goal
                ros.publish('/patrol/start', 'geometry_msgs/PoseStamped', {
                  header: { frame_id: 'map', stamp: { sec: 0, nanosec: 0 } },
                  pose: { position: { x: wx, y: wy, z: 0 }, orientation: { x: 0, y: 0, z: Math.sin(theta / 2), w: Math.cos(theta / 2) } },
                })
                showToast?.('Patrol route set', 'ok')
              }}
              // Table markers for integrated display
              tableMarkers={Object.entries(tables)
                .filter(([key]) => key !== '0')
                .map(([key, t]) => ({
                  key,
                  name: t.name || `Table ${key}`,
                  x: t.x,
                  y: t.y,
                }))}
              onTableMarkerClick={(table) => setSelectedTable(table)}
            />
          </div>
          {!mapData && (
            <div style={{ fontSize: 11, color: connected ? 'var(--muted)' : 'var(--danger)', padding: '12px', textAlign: 'center', lineHeight: 1.5 }}>
              {connected
                ? 'Waiting for map data from Argo…'
                : "Not connected to Argo — the map can't load until the connection is back."}
            </div>
          )}
        </section>

      </main>

      {/* ── Right Sidebar: Robot 3D Viewer (30%) ── */}
      <aside style={{ display: 'flex', flexDirection: 'column', gap: 10, padding: '12px 10px', borderLeft: '1px solid rgba(255,255,255,0.06)', overflow: 'hidden', background: 'rgba(0,0,0,0.1)' }}>
        {/* Robot Viewer - with Dropdown Settings */}
        <div style={{ position: 'relative' }}>
          {modelLoading && (
            <div style={{
              position: 'absolute', top: 0, left: 0, right: 0, bottom: 0,
              background: 'rgba(0,0,0,0.3)', display: 'flex', alignItems: 'center', justifyContent: 'center',
              zIndex: 15, borderRadius: 8
            }}>
              <div style={{ textAlign: 'center', color: 'var(--gold-bright)' }}>
                <div style={{ fontSize: 24, marginBottom: 8 }}>⟳</div>
                <div style={{ fontSize: 12, fontWeight: 600 }}>Loading...</div>
              </div>
            </div>
          )}
          <div style={{ position: 'relative', width: '100%', height: '400px' }}>
            <Robot3DViewer
              shadowColor={shadowColor}
              modelPath="/models/White.glb"
              onLoadStart={() => setModelLoading(true)}
              onLoadEnd={() => setModelLoading(false)}
            />

            {/* Settings Gear Button */}
            <button
              onClick={(e) => {
                e.stopPropagation()
                setShowRobotSettings(!showRobotSettings)
              }}
              style={{
                position: 'absolute', top: 8, right: 8,
                width: 32, height: 32, borderRadius: '50%',
                background: 'rgba(100,100,120,0.2)', border: '1px solid rgba(150,150,170,0.3)',
                color: 'rgba(200,200,220,0.7)', cursor: 'pointer', fontSize: 18,
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                zIndex: 10, backdropFilter: 'blur(8px)'
              }}
              title="Adjust shadow color"
            >
              ⚙
            </button>

            {/* Shadow Color Picker */}
            {showRobotSettings && (
              <div data-robot-settings style={{
                position: 'absolute', top: 45, left: 8,
                background: 'rgba(20,20,30,0.4)', border: '1px solid rgba(226,179,92,0.2)',
                borderRadius: 12, padding: 14, minWidth: 150,
                zIndex: 20, backdropFilter: 'blur(20px)',
                boxShadow: '0 8px 32px rgba(0,0,0,0.2)'
              }}>
                <div>
                  <div style={{ fontSize: 10, color: 'var(--muted)', textTransform: 'uppercase', fontWeight: 700, marginBottom: 8 }}>Shadow Color</div>
                  <input
                    type="color"
                    value={shadowColor}
                    onChange={(e) => setShadowColor(e.target.value)}
                    style={{ width: '100%', height: 36, borderRadius: 6, border: '1px solid rgba(226,179,92,0.4)', cursor: 'pointer' }}
                    title="Click to change drop-shadow color"
                  />
                </div>
              </div>
            )}
          </div>
        </div>

        {/* Free Roam Panel */}
        {freeRoamActive && (
          <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
            <FreeRoamPanel
              mapData={mapData}
              robotPose={robotPose}
              connected={connected}
              showToast={showToast}
              launcherUrl={launcherUrl}
              mapName={selectedMap}
            />
          </div>
        )}

        {/* SONIC Info + Status Card */}
        <div className="glass-card" style={{ padding: 10 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 7, marginBottom: 9 }}>
            <div style={{ width: 26, height: 26, borderRadius: '50%', border: '1.5px solid var(--gold-bright)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ color: 'var(--gold-bright)' }}>
                <circle cx="12" cy="12" r="1"/><path d="M3 12a9 9 0 1 0 18 0 9 9 0 0 0-18 0"/><path d="M12 7v5"/>
              </svg>
            </div>
            <div>
              <div style={{ fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase', fontWeight: 700, letterSpacing: '0.02em' }}>SONIC</div>
              <div style={{ fontSize: 14, fontWeight: 700, color: '#fff' }}>Idle</div>
            </div>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6, fontSize: 13 }}>
            <div>
              <div style={{ fontSize: 10, color: 'var(--muted)', textTransform: 'uppercase', fontWeight: 700, letterSpacing: '0.02em', marginBottom: 3 }}>Location</div>
              <div style={{ fontWeight: 600, fontSize: 13 }}>Home</div>
            </div>
            <div>
              <div style={{ fontSize: 10, color: 'var(--muted)', textTransform: 'uppercase', fontWeight: 700, letterSpacing: '0.02em', marginBottom: 3 }}>Battery</div>
              <div style={{ fontWeight: 600, color: 'var(--ok)', fontSize: 13 }}>70% - 4h</div>
            </div>
            <div>
              <div style={{ fontSize: 10, color: 'var(--muted)', textTransform: 'uppercase', fontWeight: 700, letterSpacing: '0.02em', marginBottom: 3 }}>Payload</div>
              <div style={{ fontWeight: 600, fontSize: 13 }}>0 / 20 kg</div>
            </div>
            <div>
              <div style={{ fontSize: 10, color: 'var(--muted)', textTransform: 'uppercase', fontWeight: 700, letterSpacing: '0.02em', marginBottom: 3 }}>Status</div>
              <div style={{ fontWeight: 600, color: 'var(--ok)', fontSize: 13 }}>Normal ✓</div>
            </div>
          </div>
        </div>


        {/* View Details Button */}
        <button style={{ padding: '9px 12px', borderRadius: 8, fontSize: 11, fontWeight: 700, background: 'rgba(226,179,92,0.08)', border: '1px solid rgba(226,179,92,0.25)', color: 'var(--gold-bright)', cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 4, marginTop: 12 }}>
          View Robot Details
          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" style={{ color: 'var(--gold-bright)' }}>
            <path d="m9 18 6-6-6-6"/>
          </svg>
        </button>
      </aside>

      </div>

      <RadialNav pages={radialPages} activePage="overview" />

      {/* Floating Activity/Alerts Panel — top right corner */}
      {showActivityPanel && (
        <div style={{ position: 'fixed', top: 80, right: 380, zIndex: 100 }}>
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

      {/* Floating voice transcript — sits directly above the Teleop Pad's
          own floating icon, same corner/collapse pattern. Shows the live
          STT/TTS turns between Sonic and the guest, read from Postgres via
          GET /voice/transcript (main_agent.py's db_log_turn() already
          writes every turn there — this only displays it). */}
      <div style={{ position: 'fixed', bottom: 84, right: 24, zIndex: 100 }}>
        {!showTranscript && (
        <button
          onClick={() => setShowTranscript(!showTranscript)}
          title="Toggle conversation transcript"
          style={{
            padding: '12px 14px', borderRadius: 12, fontSize: 16,
            background: 'rgba(226,179,92,0.14)', border: '1px solid rgba(226,179,92,0.4)',
            color: 'var(--gold-bright)', cursor: 'pointer', fontWeight: 700,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            transition: 'all 0.2s',
          }}
        >
          {/* Speech-bubble icon */}
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>
          </svg>
        </button>
        )}
        {showTranscript && (
          <div className="glass-card" style={{ padding: 20, marginTop: 12, width: 300 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14 }}>
              <div className="label-xs">Conversation</div>
              <button
                onClick={() => setShowTranscript(false)}
                title="Minimize transcript"
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
            <div style={{ maxHeight: 320, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 8 }}>
              {transcript.turns.length === 0 ? (
                <div style={{ fontSize: 12.5, color: 'var(--muted)', textAlign: 'center', padding: '12px 0' }}>
                  No conversation yet
                </div>
              ) : transcript.turns.map((t, i) => {
                const isRobot = t.role === 'robot'
                return (
                  <div key={i} style={{ display: 'flex', justifyContent: isRobot ? 'flex-start' : 'flex-end' }}>
                    <div style={{
                      maxWidth: '85%', padding: '7px 10px', borderRadius: 10, fontSize: 12.5, lineHeight: 1.4,
                      background: isRobot ? 'rgba(226,179,92,0.12)' : 'rgba(59,240,155,0.12)',
                      border: `1px solid ${isRobot ? 'rgba(226,179,92,0.3)' : 'rgba(59,240,155,0.3)'}`,
                      color: isRobot ? 'var(--gold-bright)' : 'var(--ok)',
                    }}>
                      {t.text}
                    </div>
                  </div>
                )
              })}
              <div ref={transcriptBottomRef} />
            </div>
          </div>
        )}
      </div>

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


      {/* Table Card Overlay - Displayed when a table is clicked on the map */}
      {selectedTable && mapData && (() => {
        // Calculate marker position on screen
        const W = 760, H = 520  // Canvas dimensions
        const scale = Math.min(W / mapData.width, H / mapData.height) * 1  // zoom = 1
        const offX = (W - mapData.width * scale) / 2
        const offY = (H - mapData.height * scale) / 2

        const col = (selectedTable.x - mapData.origin.x) / mapData.resolution
        const row = mapData.height - 1 - (selectedTable.y - mapData.origin.y) / mapData.resolution
        const markerCanvasX = offX + col * scale
        const markerCanvasY = offY + row * scale

        // Convert to screen coordinates (approximate - the map takes up 70% of left side)
        const mapContainerLeft = 0
        const mapContainerWidth = window.innerWidth * 0.7
        const screenX = mapContainerLeft + (markerCanvasX / W) * mapContainerWidth
        const screenY = 80 + (markerCanvasY / H) * (window.innerHeight - 80)  // 80px for header

        return (
          <div style={{ position: 'fixed', top: screenY + 40, left: screenX + 20, zIndex: 200 }}>
          <div style={{ padding: '20px 24px', maxWidth: '400px', animation: 'slideUp 0.3s ease', backgroundColor: 'rgba(0,0,0,0.9)', borderRadius: '8px', border: '1px solid rgba(128,0,0,0.3)' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
              <div style={{ fontFamily: 'var(--font-heading)', fontSize: 18, fontWeight: 800 }}>{selectedTable.name}</div>
              <button
                onClick={() => setSelectedTable(null)}
                title="Close"
                style={{
                  background: 'none', border: 'none', color: 'var(--muted)', cursor: 'pointer',
                  padding: '4px', fontSize: 20, display: 'flex', alignItems: 'center', justifyContent: 'center',
                }}
              >
                ✕
              </button>
            </div>

            <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 12 }}>
              📍 {Number(selectedTable.x).toFixed(1)}m, {Number(selectedTable.y).toFixed(1)}m
            </div>

            {(() => {
              const orderTableNo = tableNumberFromName(selectedTable.name, selectedTable.key)
              const orderRaw = orders[orderTableNo]
              const order = orderRaw && orderRaw.items && orderRaw.items.length ? orderRaw : null

              return (
                <>
                  {order && (
                    <div style={{ marginBottom: 12, paddingBottom: 12, borderBottom: '1px dashed rgba(226,179,92,0.2)' }}>
                      <div style={{ fontSize: 10, fontWeight: 700, color: 'var(--muted)', textTransform: 'uppercase', marginBottom: 6 }}>Order</div>
                      {order.items.slice(0, 3).map((it, i) => (
                        <div key={it.id || i} style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, marginBottom: 2 }}>
                          <span>{it.qty}×{it.name}</span>
                          <span style={{ fontFamily: 'monospace', fontSize: 11 }}>₹{(it.qty * it.price).toFixed(2)}</span>
                        </div>
                      ))}
                      <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 6, paddingTop: 6, borderTop: '1px solid rgba(255,255,255,0.06)', fontWeight: 700, fontSize: 12 }}>
                        <span>Total</span>
                        <span style={{ fontFamily: 'monospace' }}>₹{order.total.toFixed(2)}</span>
                      </div>
                    </div>
                  )}

                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6, marginBottom: 12 }}>
                    {['Take Order', 'Deliver'].map((action) => {
                      const task = action
                      const blocked = voiceStatus.running && voiceStatus.table !== selectedTable.name
                      const active = voiceStatus.running && voiceStatus.table === selectedTable.name && voiceStatus.action === TASK_TO_ACTION[task]
                      return (
                        <button
                          key={task}
                          disabled={blocked}
                          onClick={() => { dispatchVoiceAction(selectedTable.name, task); setSelectedTable(null) }}
                          style={{
                            padding: '8px 7px', borderRadius: 6, fontSize: 11, fontWeight: 700,
                            background: active ? 'rgba(59,240,155,0.15)' : 'rgba(226,179,92,0.08)',
                            border: `1px solid ${active ? 'rgba(59,240,155,0.35)' : 'rgba(226,179,92,0.22)'}`,
                            color: active ? 'var(--ok)' : 'var(--gold-bright)',
                            opacity: blocked ? 0.4 : 1,
                            cursor: blocked ? 'not-allowed' : 'pointer',
                          }}
                        >
                          {active ? '…' : action}
                        </button>
                      )
                    })}
                  </div>

                  <button
                    onClick={() => { goToDestination(selectedTable.name); setSelectedTable(null) }}
                    title="Navigate to this table"
                    style={{
                      width: '100%', padding: '8px 6px', borderRadius: 6, fontSize: 11, fontWeight: 700,
                      background: 'rgba(255,255,255,0.05)',
                      border: '1px solid rgba(255,255,255,0.14)',
                      color: 'rgba(255,255,255,0.75)',
                      cursor: 'pointer',
                      marginBottom: 12,
                    }}
                  >
                    Navigate
                  </button>

                  {voiceStatus.running && voiceStatus.table === selectedTable.name && (
                    <button
                      onClick={() => { cancelVoice(selectedTable.name); setSelectedTable(null) }}
                      style={{
                        width: '100%', padding: '8px 6px', borderRadius: 6, fontSize: 11, fontWeight: 700,
                        color: 'var(--danger)',
                        background: 'rgba(224,90,90,0.08)', border: '1px solid rgba(224,90,90,0.25)',
                        cursor: 'pointer',
                      }}
                    >
                      Cancel
                    </button>
                  )}
                </>
              )
            })()}
          </div>

          {/* Overlay backdrop */}
          <div onClick={() => setSelectedTable(null)} style={{
            position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
            background: 'rgba(0,0,0,0.1)', zIndex: -1
          }} />
        </div>
        )
      })()}

    </div>
  )
})

DashboardHomeComponent.displayName = 'DashboardHome'
export default DashboardHomeComponent
