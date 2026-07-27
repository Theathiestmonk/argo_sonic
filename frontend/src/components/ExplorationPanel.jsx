import { useState, useEffect, useRef } from 'react'
import MapCanvas from './MapCanvas'
import TeleopPad from './TeleopPad'
import { ros } from '../ros'

// ── Segmented mode toggle ─────────────────────────────────────────────────────
function ModeToggle({ mode, onAuto, onManual, disabled }) {
  const opts = [
    { id: 'auto',   label: 'Auto Explore', active: mode === 'auto',   gold: true,  onClick: onAuto   },
    { id: 'manual', label: "I'll Drive",   active: mode === 'manual', gold: false, onClick: onManual },
  ]
  return (
    <div style={{
      display: 'flex', background: 'rgba(255,255,255,0.04)',
      border: '1px solid rgba(255,255,255,0.08)', borderRadius: 16, padding: 4, gap: 4,
      opacity: disabled ? 0.4 : 1, pointerEvents: disabled ? 'none' : 'auto',
      transition: 'opacity 0.2s',
    }}>
      {opts.map(opt => (
        <button
          key={opt.id}
          onClick={opt.onClick}
          style={{
            flex: 1, padding: '11px 8px', borderRadius: 13, fontSize: 13, fontWeight: 700,
            whiteSpace: 'nowrap', textAlign: 'center',
            background: opt.active
              ? opt.gold ? 'rgba(226,179,92,0.18)' : 'rgba(127,168,232,0.16)'
              : 'transparent',
            border: `1px solid ${opt.active
              ? opt.gold ? 'rgba(226,179,92,0.42)' : 'rgba(127,168,232,0.42)'
              : 'transparent'}`,
            color: opt.active
              ? opt.gold ? 'var(--gold-bright)' : 'var(--blue)'
              : 'var(--muted)',
            boxShadow: opt.active
              ? opt.gold ? '0 4px 16px rgba(226,179,92,0.12)' : '0 4px 16px rgba(127,168,232,0.12)'
              : 'none',
            transition: 'all 0.2s',
          }}
        >
          {opt.label}
        </button>
      ))}
    </div>
  )
}

// ── Main panel ────────────────────────────────────────────────────────────────
export default function ExplorationPanel({ mapData, robotPose, frontiers, connected, showToast, onDone, launcherUrl, startRetrying, stopRetrying }) {
  const [mode, setMode]           = useState(null)
  const [saveName, setSaveName]   = useState('my_map')
  const [stackState, setStackState] = useState('unknown') // 'unknown'|'starting'|'running'|'stopped'
  const [saving, setSaving]        = useState(false)  // map save in progress
  const [mapsDir, setMapsDir]      = useState(null)   // src/argo_mini/maps on the robot, from launcher /config
  const pauseTopicRef = useRef(null)
  const pollRef = useRef(null)
  const failCountRef = useRef(0)

  useEffect(() => {
    pauseTopicRef.current = ros.topic('/exploration/paused', 'std_msgs/Bool')
    checkStack()
    fetch(`${launcherUrl}/config`).then(r => r.json()).then(d => setMapsDir(d.maps_dir)).catch(() => {})
    return () => clearInterval(pollRef.current)
  }, [])

  // Poll stack status every 5 s so UI stays in sync
  useEffect(() => {
    const id = setInterval(checkStack, 5000)
    return () => clearInterval(id)
  }, [])

  // The fast 3 s poll kicked off by startStack() is only needed while
  // waiting for the stack to come up — stop it once we know either way,
  // otherwise it runs forever stacked on top of the 5 s poll above.
  useEffect(() => {
    if (stackState !== 'starting' && pollRef.current) {
      clearInterval(pollRef.current)
      pollRef.current = null
    }
  }, [stackState])

  const checkStack = async () => {
    try {
      const r = await fetch(`${launcherUrl}/status`)
      const d = await r.json()
      failCountRef.current = 0
      setStackState(d.running ? 'running' : 'stopped')
      if (d.running && d.mode) setMode(d.mode)
    } catch {
      // A single dropped request shouldn't collapse a running session — only
      // treat the launcher as unreachable after repeated consecutive
      // failures. (Must use the failCountRef counter, not the stackState
      // variable, here: this closure is shared by a setInterval set up once
      // on mount, so any outer state it reads would be permanently stale —
      // the ref is the only thing guaranteed to reflect the live count.)
      failCountRef.current += 1
      if (failCountRef.current >= 3) {
        setStackState('stopped')
      }
    }
  }

  const startStack = async () => {
    if (!mode) return
    setStackState('starting')
    showToast(
      mode === 'manual' ? 'Starting SLAM mapping — please wait…' : 'Starting Argo stack — please wait…',
      'info',
    )
    try {
      await fetch(`${launcherUrl}/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode }),
      })
      // Only (re)connect rosbridge if we're not already connected — forcing
      // a reconnect here tears down and replaces the live connection object,
      // which would silently orphan anything (like TeleopPad) that isn't
      // listening for reconnects.
      if (!connected) startRetrying?.()
      // Also poll launcher status every 3 s for feedback
      pollRef.current = setInterval(checkStack, 3000)
    } catch {
      setStackState('stopped')
      showToast('Could not reach launcher — is launcher.py running on the robot?', 'danger')
    }
  }

  const stopStack = async () => {
    try {
      await fetch(`${launcherUrl}/stop`, { method: 'POST' })
      setStackState('stopped')
      setMode(null)
      stopRetrying?.()
      showToast('Argo stack stopped', 'info')
    } catch {
      showToast('Could not reach launcher', 'danger')
    }
  }

  // Mode is picked *before* the stack launches — it decides which script
  // the launcher runs (SLAM-only for manual, full Nav2 stack for auto) —
  // so these are just local-state setters, not live topic toggles.
  const selectAuto = () => {
    if (stackState === 'running' || stackState === 'starting') return
    setMode('auto')
  }

  const selectManual = () => {
    if (stackState === 'running' || stackState === 'starting') return
    setMode('manual')
  }

  useEffect(() => {
    if (stackState === 'running' && mode === 'auto') {
      pauseTopicRef.current?.publish({ data: false })
      showToast('Argo is now exploring automatically', 'ok')
    } else if (stackState === 'running' && mode === 'manual') {
      showToast('Manual mode — use controls to drive', 'info')
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stackState])

  const saveMap = () => {
    if (!connected) { showToast('Not connected to Argo', 'danger'); return }
    if (!mapsDir) { showToast('Maps folder not known yet — try again in a moment', 'danger'); return }
    if (saving) return
    const path = `${mapsDir}/${saveName.trim() || 'my_map'}`
    setSaving(true)

    // Step 1 — Serialize SLAM Toolbox pose graph (.posegraph + .data)
    // Equivalent: ros2 service call /slam_toolbox/serialize_map ...
    const serializeSvc = ros.service('/slam_toolbox/serialize_map', 'slam_toolbox/SerializePoseGraph')
    serializeSvc?.callService(
      { filename: path },
      () => {
        showToast('Pose graph saved ✓  — saving map image…', 'info')

        // Step 2 — Save occupancy grid (.pgm + .yaml) for Nav2 / AMCL
        // Equivalent: ros2 run nav2_map_server map_saver_cli -f <path>
        const mapSvc = ros.service('/slam_toolbox/save_map', 'slam_toolbox/SaveMap')
        mapSvc?.callService(
          { name: { data: path } },
          () => {
            setSaving(false)
            showToast(`Map saved → ${path}`, 'ok')
          },
          () => {
            setSaving(false)
            showToast('Map image save failed (pose graph OK)', 'danger')
          },
        )
      },
      () => {
        setSaving(false)
        showToast('Pose graph save failed', 'danger')
      },
    )
  }

  const frontierCount = frontiers?.length ?? 0
  const isStarting = stackState === 'starting'

  return (
    <div style={{
      display: 'grid', gridTemplateColumns: '300px 1fr', gap: 20,
      height: 'calc(100vh - 140px)', maxWidth: 1200, margin: '0 auto',
      animation: 'slideUp 0.35s ease',
    }}>

      {/* ── Left panel ── */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12, overflowY: 'auto', paddingRight: 2 }}>

        {/* Mode toggle — pick before launch, since it decides which script runs */}
        <div className="glass-dense" style={{ padding: 18 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14 }}>
            <div className="label-xs">How should Argo map?</div>
            {connected && <span style={{ fontSize: 10.5, fontWeight: 700, padding: '3px 10px', borderRadius: 99, background: 'rgba(59,240,155,0.1)', border: '1px solid rgba(59,240,155,0.25)', color: 'var(--ok)' }}>Connected</span>}
          </div>
          <ModeToggle mode={mode} onAuto={selectAuto} onManual={selectManual} disabled={stackState === 'running' || isStarting} />
          {stackState === 'running' && (
            <p style={{ fontSize: 12, color: 'var(--muted)', marginTop: 12, lineHeight: 1.6 }}>
              <strong style={{ color: '#fff' }}>Stop stack</strong> below to choose a different mode.
            </p>
          )}
          {stackState !== 'running' && !isStarting && !mode && (
            <p style={{ fontSize: 12, color: 'var(--muted)', marginTop: 12, lineHeight: 1.6 }}>
              Choose how Argo should map your space, then start the stack below.
            </p>
          )}
        </div>

        {/* Stack launch gate — show when stack not running (independent of rosbridge) */}
        {stackState !== 'running' && (
          <div className="glass-dense" style={{ padding: 20, animation: 'slideUp 0.2s ease' }}>
            <div className="label-xs" style={{ marginBottom: 14 }}>Robot stack</div>

            {isStarting ? (
              <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '14px 16px', borderRadius: 14, background: 'rgba(226,179,92,0.07)', border: '1px solid rgba(226,179,92,0.2)' }}>
                <div style={{ width: 14, height: 14, borderRadius: '50%', border: '2px solid var(--gold)', borderTopColor: 'transparent', animation: 'spin-slow 0.9s linear infinite', flexShrink: 0 }} />
                <div>
                  <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--gold-bright)' }}>Starting up…</div>
                  <div style={{ fontSize: 11.5, color: 'var(--muted)', marginTop: 2 }}>
                    {mode === 'manual' ? 'SLAM and sensors are launching' : 'SLAM, Nav2 and sensors are launching'}
                  </div>
                </div>
              </div>
            ) : (
              <>
                <button
                  onClick={startStack}
                  disabled={!mode}
                  style={{
                    width: '100%', padding: '14px 0', borderRadius: 14,
                    fontSize: 14, fontWeight: 800, fontFamily: 'var(--font-heading)',
                    background: mode ? 'linear-gradient(180deg,rgba(226,179,92,0.22) 0%,rgba(226,179,92,0.08) 100%)' : 'rgba(255,255,255,0.04)',
                    border: `1px solid ${mode ? 'rgba(226,179,92,0.4)' : 'rgba(255,255,255,0.08)'}`,
                    color: mode ? 'var(--gold-bright)' : 'var(--muted)',
                    boxShadow: mode ? '0 8px 28px rgba(226,179,92,0.12)' : 'none',
                    transition: 'all 0.2s', cursor: mode ? 'pointer' : 'not-allowed',
                  }}
                  onMouseEnter={e => { if (mode) { e.currentTarget.style.background = 'rgba(226,179,92,0.22)'; e.currentTarget.style.boxShadow = '0 10px 32px rgba(226,179,92,0.22)' } }}
                  onMouseLeave={e => { if (mode) { e.currentTarget.style.background = 'linear-gradient(180deg,rgba(226,179,92,0.22) 0%,rgba(226,179,92,0.08) 100%)'; e.currentTarget.style.boxShadow = '0 8px 28px rgba(226,179,92,0.12)' } }}
                >
                  {mode ? 'Start Argo' : 'Choose a mode above first'}
                </button>
                <p style={{ fontSize: 11.5, color: 'var(--muted)', marginTop: 12, lineHeight: 1.65 }}>
                  {mode === 'manual'
                    ? 'Starts SLAM and sensors only — you drive, no Nav2 required.'
                    : 'Starts SLAM, Nav2, sensors and the exploration stack on the robot automatically.'}
                </p>
              </>
            )}
          </div>
        )}

        {/* Auto mode status */}
        {stackState === 'running' && mode === 'auto' && (
          <div className="glass-card" style={{ padding: 22, animation: 'slideUp 0.2s ease' }}>
            <div className="label-xs" style={{ marginBottom: 16 }}>Exploration status</div>

            <div style={{
              display: 'flex', alignItems: 'center', gap: 14, padding: '16px 18px',
              borderRadius: 16,
              background: frontierCount > 0 ? 'rgba(59,240,155,0.07)' : 'rgba(255,255,255,0.03)',
              border: `1px solid ${frontierCount > 0 ? 'rgba(59,240,155,0.22)' : 'rgba(255,255,255,0.07)'}`,
            }}>
              <div style={{
                width: 12, height: 12, borderRadius: '50%', flexShrink: 0,
                background: frontierCount > 0 ? 'var(--ok)' : 'rgba(255,255,255,0.2)',
                boxShadow: frontierCount > 0 ? '0 0 10px var(--ok)' : 'none',
                animation: frontierCount > 0 ? 'pulse-dot 1.5s infinite' : 'none',
              }} />
              <div>
                <div style={{ fontSize: 13.5, fontWeight: 600, marginBottom: 3 }}>
                  {frontierCount > 0 ? 'Argo is exploring' : 'Waiting to begin…'}
                </div>
                <div style={{ fontSize: 11.5, color: 'var(--muted)' }}>
                  {frontierCount > 0
                    ? `${frontierCount} area${frontierCount !== 1 ? 's' : ''} left to map`
                    : 'Frontier explorer is starting up'}
                </div>
              </div>
            </div>

            <p style={{ fontSize: 12, color: 'var(--muted)', marginTop: 14, lineHeight: 1.65 }}>
              Argo will scan every corner on its own. Switch to <strong style={{ color: '#fff' }}>I'll Drive</strong> to take over at any time.
            </p>
          </div>
        )}

        {/* Manual D-pad */}
        {stackState === 'running' && mode === 'manual' && (
          <div className="glass-card" style={{ padding: 24, animation: 'slideUp 0.2s ease' }}>
            <div className="label-xs" style={{ marginBottom: 20 }}>Drive controls</div>
            <TeleopPad connected={connected} />
          </div>
        )}

        {/* Save map + remap */}
        {stackState === 'running' && mode && (
          <div className="glass-dense" style={{ padding: 18, animation: 'slideUp 0.22s ease' }}>
            <div className="label-xs" style={{ marginBottom: 12 }}>Save the map</div>
            <input
              value={saveName}
              onChange={e => setSaveName(e.target.value)}
              placeholder="Map name"
              style={{
                width: '100%', padding: '11px 14px', borderRadius: 12, fontSize: 13,
                background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(255,255,255,0.1)',
                color: '#fff', outline: 'none', marginBottom: 10,
                boxSizing: 'border-box',
              }}
            />
            <button
              onClick={saveMap}
              disabled={saving}
              className="btn-ok"
              style={{ width: '100%', justifyContent: 'center', padding: '11px 0', opacity: saving ? 0.6 : 1, display: 'flex', alignItems: 'center', gap: 8 }}
            >
              {saving && (
                <span style={{ width: 13, height: 13, borderRadius: '50%', border: '2px solid currentColor', borderTopColor: 'transparent', animation: 'spin-slow 0.8s linear infinite', display: 'inline-block', flexShrink: 0 }} />
              )}
              {saving ? 'Saving…' : 'Save map to robot'}
            </button>
          </div>
        )}

        {/* Done */}
        {stackState === 'running' && mode && (
          <button
            onClick={onDone}
            className="btn-primary"
            style={{ width: '100%', padding: '15px 0', fontFamily: 'var(--font-heading)', fontSize: 14 }}
          >
            Map is ready — label places
          </button>
        )}

        {/* Remap / Stop stack */}
        {stackState === 'running' && mode && (
          <div style={{ display: 'flex', gap: 8 }}>
            <button
              onClick={() => {
                // Mode is locked to whichever script is running until the
                // stack is stopped — Remap just re-pauses exploration (auto
                // mode) so the operator can go over the space again.
                if (mode === 'auto') pauseTopicRef.current?.publish({ data: true })
                showToast('Exploration reset — drive or explore again', 'info')
              }}
              style={{
                flex: 1, padding: '11px 0', borderRadius: 13, fontSize: 12.5, fontWeight: 600,
                background: 'transparent', border: '1px solid rgba(255,255,255,0.08)',
                color: 'var(--muted)', cursor: 'pointer', transition: 'all 0.2s',
              }}
              onMouseEnter={e => { e.currentTarget.style.borderColor = 'rgba(255,255,255,0.18)'; e.currentTarget.style.color = '#fff' }}
              onMouseLeave={e => { e.currentTarget.style.borderColor = 'rgba(255,255,255,0.08)'; e.currentTarget.style.color = 'var(--muted)' }}
            >
              Remap
            </button>
            <button
              onClick={stopStack}
              style={{
                padding: '11px 16px', borderRadius: 13, fontSize: 12.5, fontWeight: 600,
                background: 'rgba(255,94,94,0.07)', border: '1px solid rgba(255,94,94,0.2)',
                color: 'var(--danger)', cursor: 'pointer', transition: 'all 0.2s',
              }}
              onMouseEnter={e => { e.currentTarget.style.background = 'rgba(255,94,94,0.15)' }}
              onMouseLeave={e => { e.currentTarget.style.background = 'rgba(255,94,94,0.07)' }}
            >
              Stop stack
            </button>
          </div>
        )}

        {/* Placeholder when no mode selected */}
        {!mode && (
          <div style={{ textAlign: 'center', color: 'var(--muted)', fontSize: 13, lineHeight: 1.7, padding: '20px 12px' }}>
            Choose a mapping mode above<br/>to get started
          </div>
        )}
      </div>

      {/* ── Right: live map ── */}
      <div className="glass-dense" style={{ padding: 16, display: 'flex', flexDirection: 'column' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12, padding: '0 4px' }}>
          <div style={{ fontFamily: 'var(--font-heading)', fontWeight: 700, fontSize: 16 }}>Live Map</div>
          <div style={{ display: 'flex', gap: 18, fontSize: 12, color: 'var(--muted)' }}>
            {frontierCount > 0 && (
              <span style={{ display: 'flex', alignItems: 'center', gap: 6, color: 'var(--blue)' }}>
                <span style={{ width: 7, height: 7, borderRadius: '50%', background: 'currentColor', display: 'inline-block' }} />
                {frontierCount} areas left
              </span>
            )}
            <span style={{ display: 'flex', alignItems: 'center', gap: 6, color: 'var(--gold-bright)' }}>
              <span style={{ width: 7, height: 7, borderRadius: '50%', background: 'currentColor', display: 'inline-block' }} />
              Argo
            </span>
          </div>
        </div>
        <div style={{ flex: 1, minHeight: 0 }}>
          <MapCanvas mapData={mapData} robotPose={robotPose} frontiers={frontiers} />
        </div>
        {!mode && (
          <div style={{ textAlign: 'center', padding: '14px 0 4px', color: 'var(--muted)', fontSize: 13 }}>
            Choose a mode to start mapping
          </div>
        )}
      </div>
    </div>
  )
}
