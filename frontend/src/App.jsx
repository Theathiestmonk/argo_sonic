import { useState, useEffect, useRef, useCallback } from 'react'
import { ros } from './ros'
import { loadSavedMap } from './mapLoader'
import EnvironmentSelector from './components/EnvironmentSelector'
import ExplorationPanel    from './components/ExplorationPanel'
import TablesPanel         from './components/TablesPanel'
import SettingsPanel       from './components/SettingsPanel'
import DashboardHome       from './components/DashboardHome'
import Shell, { useRestaurant } from './ui/Shell'
import MenuPage from './ui/MenuPage'
import OrdersPage from './ui/OrdersPage'
import * as Ic from './ui/icons'

// Golden theme styling
if (typeof document !== 'undefined') {
  const style = document.createElement('style')
  style.textContent = `
    /* Dropdown golden styling */
    select {
      color-scheme: dark;
    }
    select option {
      background-color: #2a2a3e;
      color: #e2b35c;
    }
    select option:checked {
      background: #e2b35c;
      color: #1a1a2e;
    }

    /* Range slider golden styling */
    input[type="range"] {
      accent-color: #e2b35c;
    }
    input[type="range"]::-webkit-slider-thumb {
      background: #e2b35c;
    }
    input[type="range"]::-moz-range-thumb {
      background: #e2b35c;
    }

    /* Hide scrollbars completely while keeping scroll functionality */
    ::-webkit-scrollbar {
      display: none !important;
      width: 0 !important;
      height: 0 !important;
    }
    ::-webkit-scrollbar-track {
      display: none !important;
    }
    ::-webkit-scrollbar-thumb {
      display: none !important;
    }
    html, body, main, div, section, aside {
      scrollbar-width: none !important;
      -ms-overflow-style: none !important;
    }
    * {
      -ms-overflow-style: none !important;
    }
  `
  document.head.appendChild(style)
}

const STEPS = [
  { label: 'Choose Space',  short: 'Space' },
  { label: 'Map the Area',  short: 'Map'   },
  { label: 'Set Locations', short: 'Places' },
]

// Last known robot pose per map, kept in the browser so the marker survives a page refresh.
// (slam_toolbox's /pose only publishes when it processes a new scan, so a robot standing still sends nothing.)
const POSE_KEY = 'argo_last_pose'
function loadPose(map) {
  try { const p = (JSON.parse(localStorage.getItem(POSE_KEY)) || {})[map]; return p && Number.isFinite(p.x) && Number.isFinite(p.y) ? { x: p.x, y: p.y, theta: p.theta || 0 } : null } catch { return null }
}
function savePose(map, pose) {
  try { const all = JSON.parse(localStorage.getItem(POSE_KEY)) || {}; all[map] = { x: pose.x, y: pose.y, theta: pose.theta, at: Date.now() }; localStorage.setItem(POSE_KEY, JSON.stringify(all)) } catch { /* private mode */ }
}
const yawOf = (q) => Math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))

function menuApiUrl(rosUrl) {
  // menu service (backend/pos_api.py) listens on :8890 of the same host
  return rosUrl.replace(/^ws/, 'http').replace(/:\d+$/, ':8890')
}

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
  const [demoMode] = useState(() => new URLSearchParams(window.location.search).get('demo') === 'true')
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
  const [navRunning, setNavRunning] = useState(true)   // false once the launcher reports the stack stopped → Stop button disabled
  const [navPoseSet, setNavPoseSet] = useState(false)
  const [navPct, setNavPct] = useState(0)
  const [navBusy, setNavBusy] = useState(false)   // a Start/Stop request is in flight — ignore extra clicks
  const [navProgress, setNavProgress] = useState(null)   // live step-by-step message (e.g. "Starting Serial Bridge"), shown on the Start Argo button while initializing
  const retryRef = useRef(null)
  const [ordersCount, setOrdersCount] = useState(0)
  const restaurant = useRestaurant()
  useEffect(() => {                         // sidebar badge = open orders in the POS ledger (when its service is up)
    let dead = false
    const load = () => fetch(`${menuApiUrl(rosUrl)}/orders/summary`, { cache: 'no-store' }).then(r => r.json()).then(d => { if (!dead && typeof d.open === 'number') setOrdersCount(d.open) }).catch(() => {})
    load(); const id = setInterval(load, 5000)
    return () => { dead = true; clearInterval(id) }
  }, [rosUrl])

  const [mapData, setMapData]     = useState(null)
  const [costmapData, setCostmapData] = useState(null)
  const [robotPose, setRobotPose] = useState(() => loadPose(localStorage.getItem('argo_selected_map') || 'office_map'))
  const [poseStale, setPoseStale] = useState(true)       // true until a live pose has arrived (or after it stops arriving)
  const lastLiveRef = useRef(0)
  const lastSavedRef = useRef(0)
  const mapOdomRef = useRef(null)                         // map→odom from /tf  {x, y, yaw}
  const odomFullRef = useRef(null)                        // odom→base from /odom {x, y, yaw}
  const selectedMapRef = useRef(null)
  const runningMapRef = useRef(null)                      // the map navigation is actually running (poses are only valid in that map's frame)
  const [frontiers, setFrontiers] = useState([])
  const [plannedPath, setPlannedPath] = useState([])
  // Bottom-center telemetry card (DashboardHome) — speed/wheels from
  // serial_bridge.py, obstacle distances from safety_shield.py. Grouped
  // into two objects rather than ~8 separate useState hooks.
  const [driveTelemetry, setDriveTelemetry] = useState({ speed: null, angularVel: null, wheelLeft: null, wheelRight: null })
  // /safety_shield/status: {shield,depth,lidar,us}; ts = last message time
  const [shieldStatus, setShieldStatus] = useState({ ts: 0 })
  const [sensorDistances, setSensorDistances] = useState({ lidar: null, depth: null, usFrontLeft: null, usFrontRight: null })
  // Same map-frame localized pose as robotPose above (slam_toolbox's
  // /pose) but kept in a ref too, not just state — arrival-checking polls
  // this on an interval and doesn't need a re-render on every tick the way
  // the on-screen marker (robotPose state) does.
  const mapPoseRef = useRef(null)
  // Raw /odom position (not map-frame-corrected) at the moment it was last
  // received — used only to re-anchor the local costmap (see the
  // /local_costmap/costmap subscription below) into map-frame coordinates,
  // never for the on-screen robot marker itself (mapPoseRef/robotPose
  // above are the map-frame-correct ones for that).
  const odomPoseRef = useRef(null)
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
      const publish = (x, y, theta) => {
        mapPoseRef.current = { x, y }
        lastLiveRef.current = Date.now()
        setRobotPose({ x, y, theta })
        if (Date.now() - lastSavedRef.current > 1000 && runningMapRef.current) { lastSavedRef.current = Date.now(); savePose(runningMapRef.current, { x, y, theta }) }
      }
      const t = ros.topic('/pose', 'geometry_msgs/msg/PoseWithCovarianceStamped', { throttle_rate: 200 })
      t?.subscribe(msg => {
        const { x, y } = msg.pose.pose.position
        publish(x, y, yawOf(msg.pose.pose.orientation))
      })
      // /pose is silent while the robot is stationary, so the marker also comes from the TF tree: the map→odom
      // correction slam_toolbox keeps publishing, composed with the latest odometry. Live right after a refresh.
      const compose = () => {
        const mo = mapOdomRef.current, od = odomFullRef.current
        if (!mo || !od) return
        const c = Math.cos(mo.yaw), sn = Math.sin(mo.yaw)
        publish(mo.x + c * od.x - sn * od.y, mo.y + sn * od.x + c * od.y, mo.yaw + od.yaw)
      }
      subRefs.current.composePose = compose
      const tf = ros.topic('/tf', 'tf2_msgs/msg/TFMessage', { throttle_rate: 50, queue_length: 1 })
      tf?.subscribe(msg => {
        for (const tr of msg.transforms ?? []) {
          if (tr.header?.frame_id === 'map' && tr.child_frame_id === 'odom') {
            mapOdomRef.current = { x: tr.transform.translation.x, y: tr.transform.translation.y, yaw: yawOf(tr.transform.rotation) }
            compose()
          }
        }
      })
      subRefs.current.tf = tf
      subRefs.current.pose = t
    }
    if (!subRefs.current.odom) {
      // Raw dead-reckoning is fine here (unlike /pose above) for the
      // telemetry card — only the instantaneous twist is used there, never
      // accumulated position, so drift doesn't matter. Position IS also
      // captured now (odomPoseRef), but only to re-anchor the local
      // costmap subscription below into map-frame coordinates.
      const t = ros.topic('/odom', 'nav_msgs/msg/Odometry', { throttle_rate: 200 })
      t?.subscribe(msg => {
        setDriveTelemetry(d => ({ ...d, speed: msg.twist.twist.linear.x, angularVel: msg.twist.twist.angular.z }))
        odomPoseRef.current = { x: msg.pose.pose.position.x, y: msg.pose.pose.position.y }
        odomFullRef.current = { x: msg.pose.pose.position.x, y: msg.pose.pose.position.y, yaw: yawOf(msg.pose.pose.orientation) }
        subRefs.current.composePose?.()
      })
      subRefs.current.odom = t
    }
    if (!subRefs.current.costmap) {
      // Nav2's local costmap (MPPI's own obstacle view — ObstacleLayer +
      // InflationLayer, see nav2.yaml) — a small rolling window in the
      // ODOM frame (global_frame: odom there), not map, so it can't be
      // drawn using its own reported origin directly the way /map is: odom
      // and map frames slowly diverge (see /pose's own comment above on
      // exactly this drift), so a naive origin would drift out of true
      // alignment with the static map over time/distance. Re-anchored here
      // instead: the costmap's world origin, minus the robot's raw odom
      // position at that same moment, plus the robot's real map-frame
      // position (mapPoseRef) — i.e. "this many metres from the robot,
      // wherever the robot actually is" rather than trusting odom's own
      // absolute coordinates. A reasonable approximation for a ~6m window
      // (translation only, no heading-drift correction) — the amount an
      // odom/map heading estimate typically diverges over a few metres is
      // negligible next to what this is for (an approximate live overlay,
      // not a safety-critical position).
      const t = ros.topic('/local_costmap/costmap', 'nav_msgs/OccupancyGrid', { throttle_rate: 500 })
      t?.subscribe(msg => {
        const mapPose = mapPoseRef.current
        const odomPose = odomPoseRef.current
        if (!mapPose || !odomPose) return   // no fix yet on either frame — skip this one, next message tries again
        setCostmapData({
          width: msg.info.width, height: msg.info.height,
          resolution: msg.info.resolution,
          origin: {
            x: msg.info.origin.position.x + (mapPose.x - odomPose.x),
            y: msg.info.origin.position.y + (mapPose.y - odomPose.y),
          },
          data: msg.data,
        })
      })
      subRefs.current.costmap = t
    }
    if (!subRefs.current.wheelSpeeds) {
      // serial_bridge.py's per-wheel measured speed — /odom's twist only
      // has the combined chassis velocity, not each side individually.
      const t = ros.topic('/wheel_speeds', 'std_msgs/msg/Float32MultiArray', { throttle_rate: 200 })
      t?.subscribe(msg => {
        const [left, right] = msg.data ?? []
        setDriveTelemetry(d => ({ ...d, wheelLeft: left, wheelRight: right }))
      })
      subRefs.current.wheelSpeeds = t
    }
    if (!subRefs.current.safetyLidar) {
      const t = ros.topic('/safety_shield/lidar_distance', 'std_msgs/msg/Float32', { throttle_rate: 200 })
      t?.subscribe(msg => setSensorDistances(d => ({ ...d, lidar: msg.data })))
      subRefs.current.safetyLidar = t
    }
    if (!subRefs.current.safetyStatus) {
      const t = ros.topic('/safety_shield/status', 'std_msgs/msg/String', { throttle_rate: 500 })
      t?.subscribe(msg => {
        try { setShieldStatus({ ...JSON.parse(msg.data), ts: Date.now() }) } catch { /* ignore malformed */ }
      })
      subRefs.current.safetyStatus = t
    }
    if (!subRefs.current.safetyDepth) {
      const t = ros.topic('/safety_shield/depth_distance', 'std_msgs/msg/Float32', { throttle_rate: 200 })
      t?.subscribe(msg => setSensorDistances(d => ({ ...d, depth: msg.data })))
      subRefs.current.safetyDepth = t
    }
    // Ultrasonic — serial_bridge.py publishes each corner directly (not
    // routed through the safety shield), so these subscribe straight to it.
    const US_TOPICS = [
      ['/us/front_left',  'usFrontLeft'],
      ['/us/front_right', 'usFrontRight'],
    ]
    US_TOPICS.forEach(([topic, key]) => {
      const refKey = `us_${key}`
      if (!subRefs.current[refKey]) {
        const t = ros.topic(topic, 'sensor_msgs/msg/Range', { throttle_rate: 200 })
        t?.subscribe(msg => setSensorDistances(d => ({ ...d, [key]: msg.range })))
        subRefs.current[refKey] = t
      }
    })
  }, [])

  useEffect(() => { selectedMapRef.current = selectedMap }, [selectedMap])
  // /map is latched and only re-published on map changes, so after a reload
  // the live subscription can stay empty. If nothing arrived shortly after
  // load, show the saved map until a live one replaces it.
  useEffect(() => {
    if (demoMode) return
    let dead = false
    const id = setTimeout(() => {
      setMapData(cur => {
        if (!cur) {
          loadSavedMap(launcherUrl(rosUrl), selectedMap)
            .then(m => { if (m && !dead) setMapData(prev => prev ?? m) })
            .catch(() => {})
        }
        return cur
      })
    }, 2500)
    return () => { dead = true; clearTimeout(id) }
  }, [selectedMap, rosUrl, demoMode])
  useEffect(() => {                                       // which map is navigation running? (launcher /status)
    let dead = false
    const load = () => fetch(`${launcherUrl(rosUrl)}/status`).then(r => r.json()).then(d => { if (!dead) runningMapRef.current = d.running && d.map ? d.map : null }).catch(() => {})
    load(); const id = setInterval(load, 10000)
    return () => { dead = true; clearInterval(id) }
  }, [rosUrl])
  useEffect(() => {                                       // a different map → show that map's last known pose until a live one arrives
    if (Date.now() - lastLiveRef.current > 4000) setRobotPose(loadPose(selectedMap))
  }, [selectedMap])
  useEffect(() => {
    const id = setInterval(() => setPoseStale(Date.now() - lastLiveRef.current > 4000), 1000)
    return () => clearInterval(id)
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

  // Demo mode - load dashboard without real connection
  useEffect(() => {
    if (!demoMode) return
    setView('dashboard')
    setConnected(true)
    setMapData({
      width: 100,
      height: 100,
      resolution: 0.05,
      origin: { x: -2.5, y: -2.5 },
      data: new Array(10000).fill(0)
    })
    // Set initial robot position at Kitchen
    setRobotPose({ x: 1.27, y: -0.40, theta: 0 })
    // Set initial battery state
    setBattery({
      battery_percent: 75,
      charging: false,
      connected: true,
      estimated_remaining_hours: 4.5,
      estimated_charge_remaining_hours: 0
    })
  }, [demoMode])

  useEffect(() => { if (!demoMode) connect(); return () => clearInterval(retryRef.current) }, [])

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

  // The UI equivalent of RViz's "2D Pose Estimate" tool. There's no AMCL in
  // this stack at all — slam_toolbox's own localization mode (see
  // nav.launch.py) replaces both map_server AND amcl, and its scan-matcher
  // doesn't strictly need an initial pose the way AMCL's particle filter
  // would. This still helps convergence speed and disambiguates
  // symmetric-looking spaces (e.g. a row of identical tables), and the
  // robot is headless (no RViz/DISPLAY, see DEPLOYMENT.md §4) so it's
  // otherwise a manual SSH+RViz step every time "navigate" mode restarts.
  // Same topic, message type, and covariance RViz itself publishes, so
  // slam_toolbox's localization behaves exactly as if it came from RViz.
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
    setRobotPose({ x: wx, y: wy, theta })
    if (runningMapRef.current || selectedMapRef.current) savePose(runningMapRef.current || selectedMapRef.current, { x: wx, y: wy, theta })
    showToast('Robot position set — localizing…', 'info')
  }, [showToast])


  const shared = { mapData, robotPose, frontiers, plannedPath, connected, showToast, launcherUrl: launcherUrl(rosUrl), startRetrying, stopRetrying }

  const activeNav = showSettings ? 'settings' : view === 'menu' ? 'menu' : view === 'orders' ? 'orders' : view === 'wizard' ? (step === 2 ? 'tables' : 'map') : 'overview'
  const goDashboard = () => { setShowSettings(false); setView('dashboard'); setStep(0) }
  const onNav = (id) => {
    if (id === 'overview') goDashboard()
    else if (id === 'menu') { setShowSettings(false); setView('menu') }
    else if (id === 'orders') { setShowSettings(false); setView('orders') }
    else if (id === 'tables') { setShowSettings(false); setView('wizard'); setStep(2) }
    else if (id === 'map') { setShowSettings(false); setView('wizard'); setStep(1) }
    else if (id === 'settings') setShowSettings(true)
  }

  const topActions = (
    <>
      {editingUrl && (
        <form onSubmit={e => { e.preventDefault(); connect(); setEditingUrl(false) }} style={{ display: 'flex', gap: 6 }}>
          <input autoFocus value={rosUrl} onChange={e => setRosUrl(e.target.value)}
            style={{ padding: '7px 12px', borderRadius: 10, fontSize: 12, border: '1px solid var(--a-line-strong)', width: 190 }} />
          <button type="submit" className="a-btn ok">Connect</button>
          <button type="button" onClick={() => setEditingUrl(false)} className="a-btn">✕</button>
        </form>
      )}
      {navInitializing ? (
        <button className="a-btn gold-soft" disabled title={navProgress || 'Starting Argo…'}><span className="a-spin" />Starting…</button>
      ) : navReady ? (
        <span className="a-pill" title="Navigation is up and ready"><i />Argo running</span>
      ) : (
        <button className="a-btn gold" disabled={navBusy} onClick={async () => {
          if (navBusy) return
          setNavBusy(true); setNavInitializing(true)
          try { await dashboardRef.current?.startNav?.() } finally { setNavBusy(false) }
        }}>▶ Start Argo</button>
      )}
      <button className="a-btn gold-outline" disabled={navBusy || !navRunning} title={navRunning ? 'Stop Argo' : 'Argo is already stopped'} onClick={async () => {
        if (navBusy || !navRunning) return
        setNavBusy(true)
        try { await dashboardRef.current?.stopNav?.() } finally { setNavBusy(false) }
      }}>■ Stop Argo</button>
      <button className="a-iconbtn" onClick={() => dashboardRef.current?.toggleActivityPanel?.()} title="Recent activity and alerts"><Ic.Bell width="22" height="22" /></button>
    </>
  )

  return (
    <Shell active={activeNav} onNav={onNav} badges={{ orders: ordersCount }} connected={connected} restaurant={restaurant} top={topActions}>
      {view === null && <div style={{ textAlign: 'center', color: 'var(--a-muted)', padding: '80px 0' }}>Loading…</div>}

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
          costmapData={costmapData}
          robotPose={robotPose}
          poseStale={poseStale}
          plannedPath={plannedPath}
          driveTelemetry={driveTelemetry}
          sensorDistances={sensorDistances}
          shieldStatus={shieldStatus}
          battery={battery}
          onOpenSettings={() => setShowSettings(true)}
          onAddMap={() => { setView('wizard'); setStep(0); setSelectedEnv(null) }}
          onFreeRoam={() => { setView('wizard'); setStep(1) }}
          onNavInitializing={setNavInitializing}
          onNavReady={setNavReady}
          onNavRunning={setNavRunning}
          onNavPoseSet={setNavPoseSet}
          onNavProgress={(msg, pct) => { setNavProgress(msg); setNavPct(pct || 0) }}
          onViewOrders={() => setView('orders')}
          onSelectMap={setSelectedMap}
          menuApi={menuApiUrl(rosUrl)}
        />
      )}

      {view === 'orders' && !showSettings && <OrdersPage api={menuApiUrl(rosUrl)} launcherUrl={launcherUrl(rosUrl)} selectedMap={selectedMap} showToast={showToast} />}
      {view === 'menu' && !showSettings && <MenuPage api={menuApiUrl(rosUrl)} showToast={showToast} />}

      {view !== null && (showSettings || view === 'wizard') && (
        <div className="argo-legacy">
          {showSettings ? (
            <SettingsPanel launcherUrl={launcherUrl(rosUrl)} selectedMap={selectedMap} onSelectMap={setSelectedMap}
              onClose={() => setShowSettings(false)} showToast={showToast} />
          ) : (
            <>
              <nav style={{ display: 'flex', alignItems: 'center', gap: 4, marginBottom: 22, flexWrap: 'wrap' }}>
                {STEPS.map((st, i) => (
                  <div key={st.label} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                    <button onClick={() => i < step && setStep(i)} style={{
                      display: 'flex', alignItems: 'center', gap: 7, padding: '7px 12px', borderRadius: 99, fontSize: 12.5, fontWeight: 600,
                      background: i === step ? 'rgba(226,179,92,0.12)' : i < step ? 'rgba(59,240,155,0.08)' : 'rgba(255,255,255,0.03)',
                      border: `1px solid ${i === step ? 'rgba(226,179,92,0.38)' : i < step ? 'rgba(59,240,155,0.25)' : 'var(--border-glass)'}`,
                      color: i === step ? 'var(--gold-bright)' : i < step ? 'var(--ok)' : 'var(--muted)', cursor: i < step ? 'pointer' : 'default' }}>
                      <span style={{ width: 18, height: 18, borderRadius: 6, fontSize: 10, fontWeight: 800, background: 'rgba(255,255,255,0.06)', display: 'grid', placeItems: 'center' }}>{i < step ? '✓' : i + 1}</span>
                      {st.label}
                    </button>
                    {i < STEPS.length - 1 && <div style={{ width: 18, height: 1, background: 'var(--border-glass)' }} />}
                  </div>
                ))}
              </nav>
              {step === 0 && <EnvironmentSelector onSelect={env => { setSelectedEnv(env); setStep(1) }} onNewMap={() => { setSelectedEnv(null); setStep(1) }} />}
              {step === 1 && <ExplorationPanel {...shared} onDone={() => setStep(2)} />}
              {step === 2 && (
                <>
                  <TablesPanel {...shared} selectedMap={selectedMap} onNavigate={sendNavGoal} />
                  <div style={{ textAlign: 'center', marginTop: 28 }}>
                    <button onClick={goDashboard} className="btn-primary" style={{ fontSize: 15, padding: '15px 32px', display: 'inline-flex' }}>
                      Done — back to Overview
                      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
                    </button>
                  </div>
                </>
              )}
            </>
          )}
        </div>
      )}

      {toast && <div className={`a-toast ${toast.type}`}>{toast.msg}</div>}
    </Shell>
  )
}
