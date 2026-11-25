import { useState, useEffect, useRef } from 'react'
import MapPreviewThumb from './MapPreviewThumb'
import MapWaypointViewer from './MapWaypointViewer'

// One labelled range slider; calls onCommit(value) 400 ms after the last change
// so dragging doesn't fire a request (and a ros2 param set) per pixel.
function RangeRow({ label, hint, value, range, onCommit, disabled }) {
  const [v, setV] = useState(value)
  const timer = useRef(null)
  useEffect(() => { if (value != null) setV(value) }, [value])
  useEffect(() => () => clearTimeout(timer.current), [])
  if (!range) return null
  const decimals = range.step < 1 ? 2 : 0
  return (
    <div style={{ marginBottom: 20, opacity: disabled ? 0.5 : 1 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
        <span style={{ fontWeight: 700, fontSize: 14 }}>{label}</span>
        <span style={{ color: 'var(--gold-bright)', fontWeight: 700, fontSize: 14 }}>
          {Number(v).toFixed(decimals)} {range.unit}
        </span>
      </div>
      <input
        type="range" min={range.min} max={range.max} step={range.step} value={v ?? range.min}
        disabled={disabled}
        onChange={e => {
          const n = Number(e.target.value)
          setV(n)
          clearTimeout(timer.current)
          timer.current = setTimeout(() => onCommit(n), 400)
        }}
        style={{ width: '100%', marginTop: 8, accentColor: 'var(--gold)' }}
        aria-label={label}
      />
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, color: 'var(--muted)' }}>
        <span>{range.min}</span><span>{hint}</span><span>{range.max}</span>
      </div>
    </div>
  )
}

// Voice-agent audio: pick mic/speaker device, mute either, tune wake-word sensitivity.
// Mute and sensitivity apply live; device changes apply the next time the voice agent starts.
function VoiceAgentCard({ launcherUrl, showToast, card, title, sub }) {
  const [voice, setVoice] = useState(null)
  const [devs, setDevs] = useState({ inputs: [], outputs: [] })

  useEffect(() => {
    fetch(`${launcherUrl}/settings`).then(r => r.json()).then(d => setVoice(d.voice)).catch(() => {})
    fetch(`${launcherUrl}/audio/devices`).then(r => r.json()).then(setDevs).catch(() => {})
  }, [launcherUrl])

  const save = (patch, msg) => {
    setVoice(v => ({ ...v, ...patch }))
    fetch(`${launcherUrl}/settings/voice`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch),
    }).then(r => r.json()).then(d => { if (d.voice) setVoice(d.voice); showToast?.(msg) })
      .catch(() => showToast?.('Launcher not reachable'))
  }

  const sel = { width: '100%', marginTop: 6, padding: '8px 10px', borderRadius: 8, background: 'rgba(255,255,255,0.05)',
    color: 'var(--text)', border: '1px solid var(--border-glass)' }
  const label = { fontWeight: 700, fontSize: 14, display: 'block', marginTop: 14 }
  const Mute = ({ k, text }) => (
    <button className="btn-ghost" style={{ fontSize: 13, marginRight: 8, marginTop: 10,
      borderColor: voice?.[k] ? '#ff9f43' : undefined, color: voice?.[k] ? '#ff9f43' : undefined }}
      onClick={() => save({ [k]: !voice[k] }, `${text} ${!voice[k] ? 'muted' : 'on'}`)}>
      {voice?.[k] ? `${text}: muted` : `${text}: on`}
    </button>
  )
  const Device = ({ k, list, text }) => (
    <label style={label}>{text}
      <select style={sel} value={voice?.[k] ?? ''} onChange={e => save({ [k]: e.target.value }, `${text} saved – restart the voice agent to use it`)}>
        <option value="">System default</option>
        {list.map(d => <option key={d.id} value={d.name}>{d.name}</option>)}
      </select>
    </label>
  )

  const control = (action, msg) =>
    fetch(`${launcherUrl}/settings/voice/control`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action }),
    }).then(r => r.json()).then(d => {
      setVoice(v => ({ ...v, running: d.running, nav_ready: d.nav_ready,
        voice_enabled: action === 'stop' ? false : action === 'silence' ? v.voice_enabled : true }))
      showToast?.(d.error || msg)
    }).catch(() => showToast?.('Launcher not reachable'))

  // refresh the running/stopped badge while this page is open
  useEffect(() => {
    const t = setInterval(() => fetch(`${launcherUrl}/settings`).then(r => r.json())
      .then(d => setVoice(v => v && d.voice ? { ...v, running: d.voice.running, nav_ready: d.voice.nav_ready } : v)).catch(() => {}), 5000)
    return () => clearInterval(t)
  }, [launcherUrl])

  const state = !voice ? '' : voice.running ? 'Running' : !voice.voice_enabled ? 'Stopped by admin'
    : !voice.nav_ready ? 'Waiting for navigation' : 'Starting…'

  return (
    <div className="glass-card" style={card}>
      <div style={title}>Voice agent control</div>
      <p style={sub}>Start, stop or restart "Hi Sonic". If it keeps talking or behaves unexpectedly, press Stop talking, or Stop to switch the whole agent off until you start it again.</p>
      {voice && <>
        <div style={{ fontSize: 14, marginBottom: 6 }}>Status: <b style={{ color: voice.running ? '#2ecc71' : '#ff9f43' }}>{state}</b></div>
        <button className="btn-ghost" style={{ fontSize: 13, marginRight: 8, marginTop: 6, borderColor: '#ff6b6b', color: '#ff6b6b' }}
          onClick={() => control('silence', 'Stopped talking')} disabled={!voice.running}>Stop talking now</button>
        <button className="btn-ghost" style={{ fontSize: 13, marginRight: 8, marginTop: 6 }}
          onClick={() => control('stop', 'Voice agent stopped')} disabled={!voice.voice_enabled && !voice.running}>Stop voice agent</button>
        <button className="btn-ghost" style={{ fontSize: 13, marginRight: 8, marginTop: 6 }}
          onClick={() => control('start', voice.nav_ready ? 'Voice agent starting' : 'Enabled – it starts when navigation is ready')} disabled={voice.running}>Start voice agent</button>
        <button className="btn-ghost" style={{ fontSize: 13, marginTop: 6 }}
          onClick={() => control('restart', 'Voice agent restarting')}>Restart</button>
      </>}
      <div style={{ ...title, marginTop: 26 }}>Voice agent audio</div>
      <p style={sub}>Which microphone and speaker "Hi Sonic" uses. Mute and wake sensitivity apply instantly; device changes apply when the voice agent restarts.</p>
      {devs.error && <div style={{ color: '#ff9f43', fontSize: 12 }}>No audio devices found ({devs.error})</div>}
      {!voice && <div style={{ color: 'var(--muted)' }}>Loading…</div>}
      {voice && <>
        <Mute k="mic_muted" text="Microphone" />
        <Mute k="speaker_muted" text="Speaker" />
        <Device k="mic_device" list={devs.inputs || []} text="Microphone device" />
        <Device k="speaker_device" list={devs.outputs || []} text="Speaker device" />
        <div style={{ marginTop: 18 }}>
          <RangeRow label="Wake-word sensitivity threshold" hint="lower = wakes easier" value={voice.wake_threshold}
            range={{ min: 0.2, max: 0.95, step: 0.05, unit: '' }}
            onCommit={n => save({ wake_threshold: n }, 'Wake sensitivity updated')} />
        </div>
      </>}
    </div>
  )
}

function RobotControls({ launcherUrl, showToast }) {
  const [cfg, setCfg] = useState(null)
  const [err, setErr] = useState(false)

  useEffect(() => {
    fetch(`${launcherUrl}/settings`).then(r => r.json()).then(setCfg).catch(() => setErr(true))
  }, [launcherUrl])

  const post = (path, body, okMsg) =>
    fetch(`${launcherUrl}${path}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    })
      .then(r => r.json())
      .then(d => {
        if (d.ok === false) { showToast?.(d.errors?.join(', ') || 'Could not apply setting'); return }
        setCfg(c => ({ ...c, ...d }))
        showToast?.(d.note || okMsg)
      })
      .catch(() => showToast?.('Launcher not reachable'))

  const card = { padding: 22 }
  const title = { fontFamily: 'var(--font-heading)', fontWeight: 700, fontSize: 17, marginBottom: 4 }
  const sub = { color: 'var(--muted)', fontSize: 13, marginBottom: 18 }

  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', gap: 16, marginBottom: 36 }}>
      <div className="glass-card" style={card}>
        <div style={title}>Navigation speed</div>
        <p style={sub}>Limits for autonomous driving. Applied live if the nav stack is running, and remembered for next start.</p>
        {err && <div style={{ color: 'var(--muted)' }}>Launcher not reachable.</div>}
        {!cfg && !err && <div style={{ color: 'var(--muted)' }}>Loading…</div>}
        {cfg && <>
          <RangeRow label="Max driving speed" hint="forward" value={cfg.max_linear} range={cfg.ranges?.max_linear}
            onCommit={n => post('/settings/motion', { max_linear: n }, 'Driving speed updated')} />
          <RangeRow label="Max turning speed" hint="rotation" value={cfg.max_angular} range={cfg.ranges?.max_angular}
            onCommit={n => post('/settings/motion', { max_angular: n }, 'Turning speed updated')} />
        </>}
      </div>
      <div className="glass-card" style={card}>
        <div style={title}>Audio</div>
        <p style={sub}>Speaker and microphone levels on the robot's USB audio device.</p>
        {cfg && <>
          <RangeRow label="Speaker volume" hint="output" value={cfg.speaker} range={cfg.ranges?.speaker} disabled={cfg.speaker == null}
            onCommit={n => post('/settings/audio', { speaker: n }, 'Speaker volume updated')} />
          <RangeRow label="Microphone volume" hint="input gain" value={cfg.mic} range={cfg.ranges?.mic} disabled={cfg.mic == null}
            onCommit={n => post('/settings/audio', { mic: n }, 'Microphone volume updated')} />
        </>}
      </div>
      <VoiceAgentCard launcherUrl={launcherUrl} showToast={showToast} card={card} title={title} sub={sub} />
    </div>
  )
}

export default function SettingsPanel({ launcherUrl, selectedMap, onSelectMap, onClose, showToast }) {
  const [maps, setMaps] = useState(null) // null = loading
  // Separate from `maps` — a map can exist (yaml/pgm + slam_toolbox's own
  // posegraph/data, always alongside it under the same base name) without
  // having a trained NTFields model yet, or after the model's gone stale
  // relative to a re-saved map. argo_sonic_nav.py's own by-hand menu
  // already shows this; the UI-launched path skipped it entirely until now.
  const [ntfieldsModels, setNtfieldsModels] = useState(null)

  useEffect(() => {
    fetch(`${launcherUrl}/maps`)
      .then(r => r.json())
      .then(d => setMaps(d.maps ?? []))
      .catch(() => setMaps([]))
    fetch(`${launcherUrl}/ntfields_models`)
      .then(r => r.json())
      .then(d => setNtfieldsModels(d.models ?? []))
      .catch(() => setNtfieldsModels([]))
  }, [launcherUrl])

  return (
    <div style={{ maxWidth: 1180, margin: '0 auto', animation: 'slideUp 0.35s ease' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 28 }}>
        <div>
          <h2 style={{ fontFamily: 'var(--font-heading)', fontSize: 28, fontWeight: 800, letterSpacing: -0.5 }}>
            Settings
          </h2>
          <p style={{ color: 'var(--muted)', marginTop: 8, fontSize: 15 }}>
            Pick which saved map's tables and waypoints to work with.
          </p>
        </div>
        <button onClick={onClose} className="btn-ghost" style={{ fontSize: 13 }}>Close</button>
      </div>

      <RobotControls launcherUrl={launcherUrl} showToast={showToast} />

      {maps === null && (
        <div style={{ textAlign: 'center', color: 'var(--muted)', padding: '40px 0' }}>Loading maps…</div>
      )}

      {maps !== null && maps.length === 0 && (
        <div style={{ textAlign: 'center', color: 'var(--muted)', padding: '40px 0' }}>
          No saved maps found on the robot yet.
        </div>
      )}

      {maps !== null && maps.length > 0 && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(230px, 1fr))', gap: 14 }}>
          {maps.map(name => {
            const sel = selectedMap === name
            const hasModel = ntfieldsModels?.includes(name) ?? null // null = still loading
            return (
              <button
                key={name}
                onClick={() => onSelectMap(name)}
                className="glass-card"
                style={{
                  padding: 18, textAlign: 'left', cursor: 'pointer',
                  borderColor: sel ? 'rgba(226,179,92,0.42)' : 'var(--border-glass)',
                  boxShadow: sel ? '0 0 0 2px rgba(226,179,92,0.42), var(--shadow-fluid)' : 'var(--shadow-fluid)',
                  transform: sel ? 'translateY(-3px)' : undefined,
                  transition: 'all 0.25s cubic-bezier(0.25,0.8,0.25,1)',
                  position: 'relative',
                }}
              >
                <MapPreviewThumb launcherUrl={launcherUrl} mapName={name} width="100%" height={130} />
                <div style={{
                  fontFamily: 'var(--font-heading)', fontWeight: 700, fontSize: 15,
                  color: 'var(--text)', marginTop: 14,
                }}>
                  {name}
                </div>

                {/* NTFields model availability — argo_sonic_nav.py's planner
                    silently degrades to a non-functional state if this is
                    missing rather than refusing to start, so surfacing it
                    here (before a table visit is already underway) matters. */}
                {hasModel !== null && (
                  <div style={{
                    marginTop: 6, fontSize: 10.5, fontWeight: 700, padding: '3px 8px',
                    borderRadius: 99, display: 'inline-block',
                    background: hasModel ? 'rgba(59,240,155,0.1)' : 'rgba(255,159,67,0.12)',
                    border: `1px solid ${hasModel ? 'rgba(59,240,155,0.28)' : 'rgba(255,159,67,0.3)'}`,
                    color: hasModel ? 'var(--ok)' : '#ff9f43',
                  }}>
                    {hasModel ? 'Route model ready' : 'No route model yet'}
                  </div>
                )}

                {sel && (
                  <div style={{
                    position: 'absolute', top: 14, right: 14,
                    width: 24, height: 24, borderRadius: 8,
                    background: 'rgba(226,179,92,0.18)', border: '1px solid rgba(226,179,92,0.42)',
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    color: 'var(--gold-bright)', fontSize: 13, fontWeight: 800,
                  }}>✓</div>
                )}
              </button>
            )
          })}
        </div>
      )}

      {selectedMap && maps?.includes(selectedMap) && (
        <div style={{ marginTop: 32 }}>
          <div style={{ fontFamily: 'var(--font-heading)', fontWeight: 700, fontSize: 17, marginBottom: 4 }}>
            {selectedMap} — waypoints
          </div>
          <p style={{ color: 'var(--muted)', fontSize: 13, marginBottom: 16 }}>
            Click a pin (or a row on the right) to select it. Use the pencil to rename it, or the pin icon
            to arm a move, then click a new spot on the map to reposition it.
          </p>
          <MapWaypointViewer launcherUrl={launcherUrl} mapName={selectedMap} showToast={showToast} />
        </div>
      )}
    </div>
  )
}
