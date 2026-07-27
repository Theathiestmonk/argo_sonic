import { useState, useEffect, useCallback, useRef } from 'react'
import { ros } from '../ros'
import RadialNav from './RadialNav'

// Faithful React port of frontend/public/dashboard.html's layout and copy —
// same rail (Argo Status → What do you need → Where should Argo go → Send
// Argo), same stats row, same "Saved Places" grid, same Recent Activity /
// Alerts panels. The only real change from the original: places come from
// GET /waypoints/<selectedMap> (live, per-map) instead of localStorage.
const TASKS = ['Deliver', 'Call Argo', 'Take order', 'Billing', 'Room service']

export default function DashboardHome({ launcherUrl, selectedMap, connected, showToast, onNavigate, onAddMap, onOpenSettings }) {
  const [tables, setTables]         = useState({})
  const [selectedTask, setSelectedTask] = useState('Deliver')
  const [selectedDest, setSelectedDest] = useState(null)
  const [curPos, setCurPos]         = useState('Home')
  const [curStatus, setCurStatus]   = useState('Idle')
  const [taskLabel, setTaskLabel]   = useState('—')
  const [activity, setActivity]     = useState([])
  const [greeting, setGreeting]     = useState('Good day')
  const [time, setTime]             = useState('—')

  // Nav2 + SLAM-localization stack — this is what actually lets a goal reach
  // the robot; picking a map here only decides which waypoints.json to read.
  const [navState, setNavState] = useState('unknown') // 'unknown'|'starting'|'running'|'stopped'
  const [navMap, setNavMap]     = useState(null)
  const navPollRef = useRef(null)
  const navFailRef = useRef(0)

  // navState === 'running' only means the launcher's wrapper *process* is
  // alive — start_argo_nav_ui.sh itself takes 90+ real seconds (camera wait,
  // costmap wait, several lifecycle configure/activate steps) before Nav2 can
  // actually accept a goal. Confirm that separately over rosbridge by
  // checking whether /navigate_to_pose's action-status topic actually
  // exists yet, via rosapi (standard on every rosbridge_suite install —
  // ROS2 actions always expose <name>/_action/status as a plain topic).
  const [navActionReady, setNavActionReady] = useState(false)
  useEffect(() => {
    if (navState !== 'running' || navMap !== selectedMap) {
      setNavActionReady(false)
      return
    }
    let cancelled = false
    const check = () => {
      const svc = ros.service('/rosapi/topics', 'rosapi/Topics')
      svc?.callService({}, res => {
        if (!cancelled) setNavActionReady((res.topics || []).includes('/navigate_to_pose/_action/status'))
      }, () => {})
    }
    check()
    const id = setInterval(check, 2000)
    return () => { cancelled = true; clearInterval(id) }
  }, [navState, navMap, selectedMap])

  const navReady = navState === 'running' && navMap === selectedMap && navActionReady

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
    { name: 'Home', x: home.x, y: home.y, qz: home.qz ?? 0, qw: home.qw ?? 1 },
    ...entries.map(([key, t]) => ({ name: t.name || `Table ${key}`, x: t.x, y: t.y, qz: t.qz ?? 0, qw: t.qw ?? 1 })),
  ].filter((d, i, arr) => arr.findIndex(x => x.name === d.name) === i) // drop duplicate names, keep the first (real Home wins)

  const addActivity = useCallback((dest, task) => {
    const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    setActivity(prev => [{ dest, task, time }, ...prev].slice(0, 6))
  }, [])

  const sendArgo = useCallback((destNameOverride) => {
    const destName = destNameOverride ?? selectedDest
    if (!destName) { showToast('Select a destination first', 'danger'); return }
    if (!navReady) { showToast(`Start navigation on ${selectedMap} first`, 'danger'); return }
    const dest = destinations.find(d => d.name === destName)
    if (!dest) return

    setCurStatus('Moving')
    setTaskLabel(`${selectedTask} → ${destName}`)
    onNavigate(dest.x, dest.y, dest.qz, dest.qw, `Argo is heading to ${destName}`)
    addActivity(destName, selectedTask)

    setTimeout(() => {
      setCurPos(destName)
      setCurStatus('Arrived')
      showToast(`Argo arrived at ${destName}`, 'ok')
    }, 1800)
  }, [selectedDest, selectedTask, destinations, onNavigate, showToast, addActivity, navReady, selectedMap])

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
            <button className="btn-ghost" style={{ flex: 1 }} onClick={recall} disabled={!navReady}>Send Home</button>
            <button className="btn-primary" style={{ flex: 1 }} onClick={() => sendArgo()} disabled={!connected || !navReady}>Confirm</button>
          </div>
          {!navReady && (
            <p style={{ fontSize: 11, color: 'var(--muted)', marginTop: 8 }}>
              {navState === 'running' && navMap === selectedMap
                ? 'Nav2 is still starting up — this takes a minute or two.'
                : `Navigation isn't running for ${selectedMap} yet — start it above right.`}
            </p>
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
            ) : navState === 'running' && navMap === selectedMap ? (
              // Wrapper process is up, but Nav2's own ~90s+ lifecycle chain
              // (camera wait, costmap wait, several lifecycle activations)
              // hasn't actually finished yet — don't claim "ready" early.
              <button
                onClick={stopNav}
                title="Nav2 is still starting up — click to cancel"
                style={{
                  padding: '9px 16px', borderRadius: 99, fontSize: 12.5, fontWeight: 600,
                  background: 'rgba(127,168,232,0.1)', border: '1px solid rgba(127,168,232,0.3)', color: 'var(--blue)',
                  display: 'flex', alignItems: 'center', gap: 8,
                }}
              >
                <span style={{ width: 12, height: 12, borderRadius: '50%', border: '2px solid currentColor', borderTopColor: 'transparent', animation: 'spin-slow 0.8s linear infinite', flexShrink: 0 }} />
                Waiting for Nav2 (~1–2 min)…
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
            <button onClick={onAddMap} style={{ color: 'var(--gold-bright)', fontSize: 13, fontWeight: 600 }}>Add places →</button>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))', gap: 16, marginBottom: 36 }}>
            {entries.length === 0 ? (
              <div style={{ gridColumn: '1/-1', textAlign: 'center', padding: '56px 20px', color: 'var(--muted)', fontSize: 14, lineHeight: 1.8 }}>
                No places saved yet.<br/>
                <button onClick={onAddMap} style={{ color: 'var(--gold-bright)', fontWeight: 700 }}>Run the setup flow</button> to map your space and label locations — then they'll appear here.
              </div>
            ) : entries.map(([key, t]) => {
              const label = t.name || `Table ${key}`
              return (
                <div
                  key={key}
                  className="glass-card"
                  onClick={() => setSelectedDest(label)}
                  style={{
                    borderLeft: `3px solid ${selectedDest === label ? 'var(--gold)' : 'rgba(255,255,255,0.15)'}`,
                    padding: '18px 20px', cursor: 'pointer',
                    boxShadow: selectedDest === label ? '0 0 0 2px rgba(226,179,92,0.2), var(--shadow-fluid)' : undefined,
                  }}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
                    <div style={{ fontFamily: 'var(--font-heading)', fontSize: 16, fontWeight: 800 }}>{label}</div>
                    <div style={{ fontSize: 10, padding: '4px 10px', borderRadius: 99, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em', background: 'rgba(255,255,255,0.05)', border: '1px solid var(--border-glass)', color: 'var(--muted)', whiteSpace: 'nowrap' }}>Saved</div>
                  </div>
                  <div style={{ fontSize: 11.5, color: 'var(--muted)', marginTop: 8 }}>{Number(t.x).toFixed(1)} m, {Number(t.y).toFixed(1)} m</div>
                  <button
                    onClick={(e) => { e.stopPropagation(); setSelectedDest(label); sendArgo(label) }}
                    style={{ marginTop: 14, width: '100%', padding: 10, borderRadius: 11, fontSize: 12.5, fontWeight: 700, background: 'rgba(226,179,92,0.08)', border: '1px solid rgba(226,179,92,0.22)', color: 'var(--gold-bright)' }}
                  >
                    Send Argo here
                  </button>
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
    </div>
  )
}
