import { useState, useEffect, useCallback, useRef, forwardRef, useImperativeHandle } from 'react'
import { ros } from '../ros'
import RadialNav from './RadialNav'
import MapCanvas from './MapCanvas'
import TeleopPad from './TeleopPad'

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

const DashboardHomeComponent = forwardRef(({ launcherUrl, selectedMap, connected, showToast, onSetInitialPose, mapData, robotPose, onAddMap, onOpenSettings, onActivityToggle, onNavInitializing, onNavReady, onNavPoseSet, onNavProgress }, ref) => {
  const [tables, setTables]         = useState({})
  const [poseMode, setPoseMode]     = useState(false)   // pose-estimate drag mode on the always-visible map card
  const [curPos, setCurPos]         = useState('Home')
  const [curStatus, setCurStatus]   = useState('Idle')
  // Blue goal marker on the map — set whenever a table action or "Go to
  // kitchen" is clicked (see dispatchVoiceAction/goToKitchen below),
  // cleared once that trip is no longer running (below).
  const [goalMarker, setGoalMarker] = useState(null)
  const [taskLabel, setTaskLabel]   = useState('—')
  const [activity, setActivity]     = useState([])
  const [greeting, setGreeting]     = useState('Good day')
  const [time, setTime]             = useState('—')
  const [voiceStatus, setVoiceStatus] = useState({ running: false, action: null, map: null, table: null, phase: null, phase_text: null })
  const [navGotoStatus, setNavGotoStatus] = useState({ running: false, destination: null, phase: null, phase_text: null })
  const [battery, setBattery] = useState({ connected: false, charging: false, battery_percent: 0, estimated_remaining_hours: 0, estimated_charge_remaining_hours: 0 })
  const [orders, setOrders]         = useState({})
  const [expandedBills, setExpandedBills] = useState(() => new Set())
  const [showTeleopPad, setShowTeleopPad] = useState(false)
  const [showActivityPanel, setShowActivityPanel] = useState(false)

  useImperativeHandle(ref, () => ({
    toggleActivityPanel: () => setShowActivityPanel(prev => !prev),
    startNav: startNav,
    stopNav: stopNav,
    estop: estop,
    setPoseMode: setPoseMode,
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
    // Keep initializing=true until nav is fully ready OR it stops
    if (navState === 'stopped') {
      onNavInitializing?.(false)
    } else if (navReady) {
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

  // Live BMS reading (GET /battery, backend/launcher.py) — drives the
  // "Working Hours" stat card below. 10s is plenty; the pack's own state
  // doesn't change meaningfully faster than that.
  useEffect(() => {
    let cancelled = false
    const load = () => {
      fetch(`${launcherUrl}/battery`)
        .then(r => r.json())
        .then(d => { if (!cancelled) setBattery(d) })
        .catch(() => {})
    }
    load()
    const id = setInterval(load, 10000)
    return () => { cancelled = true; clearInterval(id) }
  }, [launcherUrl])

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
  const workingHoursLabel = !battery.connected ? (
    <span style={{
      display: 'inline-block', width: 14, height: 14, borderRadius: '50%',
      border: '2px solid rgba(255,255,255,0.15)', borderTopColor: 'var(--muted)',
      animation: 'spin-slow 0.8s linear infinite', verticalAlign: 'middle',
    }} />
  ) : battery.charging ? `Charging · About ${formatHm(battery.estimated_charge_remaining_hours)} until full`
    // Discharging value is unchanged — still just the plain "5h 30m" it always was.
    : formatHm(battery.estimated_remaining_hours)
  const workingHoursSub = !battery.connected ? (
    <span style={{ animation: 'pulse-dot 1.4s ease-in-out infinite' }}>Connecting to battery…</span>
  ) : battery.charging ? `${Math.round(battery.battery_percent)}%`
    : `${Math.round(battery.battery_percent)}% battery`
  // Card label swaps to "Charging" while plugged in — the value below it is
  // a completely different number (time-to-full vs. time-remaining), so the
  // label should say which one you're looking at, not just the subtitle.
  const workingHoursLabelText = battery.connected && battery.charging ? 'Charging' : 'Working Hours'
  // Status color now drives the CARD BACKGROUND (a soft tint), not the
  // value text — the value text is always plain white so it stays equally
  // readable regardless of state; the tint alone carries the ok/low/
  // charging signal, same way the rest of this dashboard uses color.
  const workingHoursRgb = !battery.connected ? '160,160,160'   // muted grey
    : battery.charging ? '127,168,232'                          // blue
    : battery.battery_percent < 20 ? '255,65,65'                // danger red
    : battery.battery_percent < 40 ? '226,179,92'                // gold
    : '59,240,155'                                               // ok green
  const ThunderboltIcon = () => (
    <svg
      width="13" height="13" viewBox="0 0 24 24" fill="var(--blue)"
      style={{ animation: 'pulse-dot 0.9s ease-in-out infinite', flexShrink: 0 }}
    >
      <path d="M13 2 3 14h7l-1 8 10-12h-7l1-8z" />
    </svg>
  )

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

  // Kitchen is reachable via the Home card's own "Go to kitchen" button, so
  // hide its duplicate card from the grid. Docker has no UI trigger at all
  // right now (not needed) but stays hidden the same way in case it's
  // wired back in later.
  const HIDDEN_CARD_NAMES = ['Kitchen', 'Docker']
  const visibleEntries = entries.filter(([, t]) => !HIDDEN_CARD_NAMES.includes(t.name))

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

  // "Go to kitchen" — a plain, conversation-free trip, backend-managed via
  // POST /nav/goto (shells out to nav_bridge.py directly, same as
  // main_agent.py's own navigate_and_wait()). Status polled via
  // navGotoStatus above.
  const goToKitchen = useCallback(() => {
    if (voiceStatus.running) {
      showToast('Sonic is busy with a table — wait for that session to finish', 'warn')
      return
    }
    if (navGotoStatus.running) return
    const dest = destinations.find(d => d.name === 'Kitchen')
    if (dest) setGoalMarker({ x: dest.x, y: dest.y })
    fetch(`${launcherUrl}/nav/goto`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ destination: 'Kitchen', map: selectedMap }),
    })
      .then(r => r.json())
      .then(d => { if (!d.ok) showToast(d.error === 'voice_session_busy' ? 'Sonic is busy with a table' : 'Could not start the trip', 'danger') })
      .catch(() => showToast('Could not reach launcher', 'danger'))
    addActivity('Kitchen', 'Go to kitchen')
  }, [destinations, showToast, addActivity, selectedMap, launcherUrl, voiceStatus, navGotoStatus])

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
              ['Location', curPos, '', '#fff', false, 13],
              ['Status', curStatus, '', curStatus === 'Moving' ? 'var(--gold)' : 'var(--ok)', false, 13],
              ['Task', taskLabel, '', 'var(--gold-bright)', true, 13],
            ].map(([k, v, s, color, wide, valueSize]) => (
              <div key={k} className="glass-card" style={{ padding: 12, gridColumn: wide ? '1 / -1' : undefined }}>
                <div style={{ fontSize: 9.5, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 700 }}>{k}</div>
                <div style={{ fontSize: valueSize, fontWeight: 700, marginTop: 5, color, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{v}</div>
                <div style={{ fontSize: 10.5, color: 'var(--muted)', marginTop: 2 }}>{s}</div>
              </div>
            ))}

            {/* Working Hours — its own card, not the generic map above: the
                percent/charging signal lives in the background tint here
                instead of the value text color (which stays plain white),
                and it swaps its own label + gets a blinking bolt icon while
                charging. */}
            <div
              className="glass-card"
              style={{
                padding: 12, gridColumn: '1 / -1',
                background: `rgba(${workingHoursRgb},0.14)`,
                border: `1px solid rgba(${workingHoursRgb},0.35)`,
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 9.5, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 700 }}>
                {workingHoursLabelText}
                {battery.connected && battery.charging && <ThunderboltIcon />}
              </div>
              <div style={{
                fontSize: battery.charging ? 15 : 18, fontWeight: 700, marginTop: 5, color: '#fff',
                whiteSpace: battery.charging ? 'normal' : 'nowrap', lineHeight: 1.3,
                overflow: 'hidden', textOverflow: 'ellipsis',
              }}>
                {workingHoursLabel}
              </div>
              <div style={{ fontSize: 10.5, color: 'rgba(255,255,255,0.65)', marginTop: 2 }}>{workingHoursSub}</div>
            </div>
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

        {/* ── Live map — always visible here (not tucked behind a modal),
            so it's obvious at a glance whether map data is actually
            arriving. Pose-setting now happens right on this same card
            instead of a separate popup: the previous "📍 Pose" button
            opened a modal that read the exact same mapData prop, so a
            missing map looked identical either way — the modal added an
            extra click without adding any real information, and (being
            gated only on navState, not on `connected`) could be opened
            while rosbridge itself was down, in which case mapData/robotPose
            can never arrive no matter how long you wait. ── */}
        <div className="glass-card" style={{ padding: 12 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
            <div style={{ fontSize: 9.5, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 700 }}>
              Live Map
            </div>
            {navState === 'running' && navActionReady && (
              <button
                onClick={() => connected && setPoseMode(v => !v)}
                disabled={!connected}
                title={!connected
                  ? "Can't set pose — not connected to Argo"
                  : (poseMode ? 'Cancel' : "Click where Argo is standing, then drag toward where it's facing")}
                style={{
                  padding: '4px 10px', borderRadius: 8, fontSize: 10.5, fontWeight: 700,
                  background: poseMode ? 'rgba(255,65,65,0.12)' : 'rgba(59,240,155,0.12)',
                  border: `1px solid ${poseMode ? 'rgba(255,65,65,0.3)' : 'rgba(59,240,155,0.3)'}`,
                  color: poseMode ? 'var(--danger)' : 'var(--ok)',
                  cursor: connected ? 'pointer' : 'not-allowed',
                  opacity: connected ? 1 : 0.5,
                }}
              >
                {poseMode ? '✕ Cancel' : '📍 Set Pose'}
              </button>
            )}
          </div>
          <div style={{ height: 260, borderRadius: 12, overflow: 'hidden' }}>
            <MapCanvas
              mapData={mapData}
              robotPose={robotPose}
              goalPose={goalMarker}
              poseEstimateMode={poseMode}
              onPoseEstimate={({ wx, wy, theta }) => {
                onSetInitialPose?.(wx, wy, theta)
                onNavPoseSet?.(true)
                setPoseMode(false)
                showToast?.('Pose set', 'ok')
              }}
            />
          </div>
          {!mapData && (
            <div style={{ fontSize: 10.5, color: connected ? 'var(--muted)' : 'var(--danger)', marginTop: 8, lineHeight: 1.5 }}>
              {connected
                ? 'Waiting for map data from Argo…'
                : "Not connected to Argo — the map can't load until the connection is back."}
            </div>
          )}
        </div>

      </aside>

      {/* ── Main ── */}
      <main style={{ paddingLeft: 20, borderLeft: '1px solid var(--border-glass)' }}>
        {/* Saved places grid */}
        <section id="dash-places">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
            <div style={{ fontFamily: 'var(--font-heading)', fontSize: 19, fontWeight: 700 }}>Locations</div>
            <div style={{ display: 'flex', gap: 16, alignItems: 'center' }}>
              <button onClick={onAddMap} style={{ color: 'var(--muted)', fontSize: 13, fontWeight: 600 }} title="Full setup wizard — remap the space, build a new map, etc.">Setup wizard →</button>
            </div>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 16, marginBottom: 36 }}>
            {/* Home Card */}
            <div
              className="glass-card"
              style={{
                padding: '18px 20px',
                display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 12,
                minHeight: '280px',
              }}
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
                {(() => {
                  // Real, backend-sourced status for a kitchen trip, whether
                  // it was this button that started it or main_agent.py's
                  // own automatic return trip at the end of an order.
                  const kitchenBusy = (navGotoStatus.running && navGotoStatus.destination === 'Kitchen')
                    || voiceStatus.phase === 'heading_to_kitchen'
                  const kitchenText = voiceStatus.phase === 'heading_to_kitchen'
                    ? voiceStatus.phase_text
                    : (navGotoStatus.destination === 'Kitchen' ? navGotoStatus.phase_text : null)
                  return (
                    <button
                      onClick={(e) => { e.stopPropagation(); goToKitchen() }}
                      disabled={kitchenBusy}
                      style={{
                        padding: '9px 8px', borderRadius: 10, fontSize: 11.5, fontWeight: 700,
                        background: kitchenBusy ? 'rgba(59,240,155,0.15)' : 'rgba(226,179,92,0.08)',
                        border: `1px solid ${kitchenBusy ? 'rgba(59,240,155,0.35)' : 'rgba(226,179,92,0.22)'}`,
                        color: kitchenBusy ? 'var(--ok)' : 'var(--gold-bright)',
                        opacity: kitchenBusy ? 0.85 : 1,
                        cursor: kitchenBusy ? 'not-allowed' : 'pointer',
                      }}
                    >
                      {kitchenBusy ? (kitchenText || 'Heading to Kitchen…') : 'Go to kitchen'}
                    </button>
                  )
                })()}
              </div>
            </div>

            {visibleEntries.length === 0 ? (
              <div style={{ gridColumn: '1/-1', textAlign: 'center', padding: '56px 20px', color: 'var(--muted)', fontSize: 14, lineHeight: 1.8 }}>
                No places saved yet.<br/>
                <button onClick={onAddMap} style={{ color: 'var(--gold-bright)', fontWeight: 700 }}>Run the setup flow</button> to map your space and label locations — then they'll appear here.
              </div>
            ) : visibleEntries.map(([key, t]) => {
              const label = t.name || `Table ${key}`
              // Postgres/orders is a separate, numbered system (service_points.label
              // is always "Table N") independent of whatever this waypoint is
              // actually named — orders stays keyed by that extracted number,
              // same as backend/launcher.py's _read_orders_db() already does.
              const orderTableNo = tableNumberFromName(label, key)
              const orderRaw = orders[orderTableNo]
              const order = orderRaw && orderRaw.items && orderRaw.items.length ? orderRaw : null
              const billOpen = expandedBills.has(key)
              return (
                <div
                  key={key}
                  className="glass-card"
                  style={{ padding: '18px 20px' }}
                >
                  <div style={{ fontFamily: 'var(--font-heading)', fontSize: 16, fontWeight: 800 }}>{label}</div>
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
                          onClick={(e) => { e.stopPropagation(); clearOrder(orderTableNo) }}
                          title="Clear this table's order history"
                          style={{ marginTop: 8, fontSize: 10.5, fontWeight: 600, color: 'var(--danger)', opacity: 0.75, padding: '2px 6px' }}
                        >
                          Clear
                        </button>
                    </div>
                  )}

                  {/* Direct action buttons — fire immediately with this table
                      as destination. main_agent.py owns the whole trip once
                      this click starts it; the label reflects its real
                      reported phase (voiceStatus.phase_text) while active. */}
                  <div style={{ marginTop: 14, display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 6 }}>
                    {TABLE_ACTIONS.map(([task, actLabel]) => {
                      const blocked = voiceStatus.running && voiceStatus.table !== label
                      const active = voiceStatus.running && voiceStatus.table === label && voiceStatus.action === TASK_TO_ACTION[task]
                      return (
                        <button
                          key={task}
                          disabled={blocked}
                          onClick={(e) => {
                            e.stopPropagation()
                            dispatchVoiceAction(label, task)
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
                          {active ? (voiceStatus.phase_text || 'On the way…') : actLabel}
                        </button>
                      )
                    })}
                  </div>

                  {/* Cancel — only shown on the table that's actually
                      locked right now (this session's own table). Kills the
                      running main_agent.py session via /voice/stop, which
                      frees the lock immediately (voiceStatus.running goes
                      false on the next poll) instead of waiting for the
                      order flow to finish or time out on its own. */}
                  {voiceStatus.running && voiceStatus.table === label && (
                    <button
                      onClick={(e) => { e.stopPropagation(); cancelVoice(label) }}
                      title="Cancel order processing and free this table"
                      style={{
                        marginTop: 6, width: '100%', padding: '7px 8px', borderRadius: 10,
                        fontSize: 11, fontWeight: 700, color: 'var(--danger)',
                        background: 'rgba(224,90,90,0.08)', border: '1px solid rgba(224,90,90,0.25)',
                        cursor: 'pointer',
                      }}
                    >
                      Cancel
                    </button>
                  )}
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

    </div>
  )
})

DashboardHomeComponent.displayName = 'DashboardHome'
export default DashboardHomeComponent
