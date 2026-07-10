import { useState, useEffect, useRef, useCallback } from 'react'
import { ros } from '../ros'

const ChUp    = () => <svg width="22" height="22" viewBox="0 0 24 24" fill="none"><polyline points="4 15 12 7 20 15" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"/></svg>
const ChDown  = () => <svg width="22" height="22" viewBox="0 0 24 24" fill="none"><polyline points="4 9 12 17 20 9"  stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"/></svg>
const ChLeft  = () => <svg width="22" height="22" viewBox="0 0 24 24" fill="none"><polyline points="15 4 7 12 15 20" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"/></svg>
const ChRight = () => <svg width="22" height="22" viewBox="0 0 24 24" fill="none"><polyline points="9 4 17 12 9 20"  stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"/></svg>
const StopIcon = () => <svg width="18" height="18" viewBox="0 0 18 18" fill="currentColor"><rect x="2" y="2" width="14" height="14" rx="3"/></svg>

export default function TeleopPad({ connected, compact = false }) {
  const holdRef  = useRef(null)
  const velRef   = useRef(null)
  const [active, setActive] = useState(null)
  const [speed, setSpeed]   = useState(0.5)

  useEffect(() => {
    velRef.current = ros.topic('/cmd_vel', 'geometry_msgs/Twist')
    return () => clearInterval(holdRef.current)
  }, [])

  const publish = useCallback((lx, az) => {
    velRef.current?.publish({
      linear: { x: lx * speed, y: 0, z: 0 },
      angular: { x: 0, y: 0, z: az },
    })
  }, [speed])

  const press = useCallback((dir, lx, az) => {
    if (!connected) return
    setActive(dir)
    publish(lx, az)
    holdRef.current = setInterval(() => publish(lx, az), 80)
  }, [connected, publish])

  const release = useCallback(() => {
    setActive(null)
    clearInterval(holdRef.current)
    velRef.current?.publish({ linear: { x: 0, y: 0, z: 0 }, angular: { x: 0, y: 0, z: 0 } })
  }, [])

  const stopNow = useCallback(() => {
    setActive(null)
    clearInterval(holdRef.current)
    velRef.current?.publish({ linear: { x: 0, y: 0, z: 0 }, angular: { x: 0, y: 0, z: 0 } })
  }, [])

  const sz = compact ? 68 : 80
  const br = compact ? 18 : 22

  const Btn = ({ id, icon, label, lx, az }) => {
    const on = active === id
    return (
      <button
        onPointerDown={e => { e.preventDefault(); press(id, lx, az) }}
        onPointerUp={release}
        onPointerLeave={release}
        style={{
          width: sz, height: sz, borderRadius: br,
          display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 4,
          background: on ? 'rgba(226,179,92,0.2)' : connected ? 'rgba(255,255,255,0.05)' : 'rgba(255,255,255,0.02)',
          border: `1.5px solid ${on ? 'rgba(226,179,92,0.55)' : connected ? 'rgba(255,255,255,0.12)' : 'rgba(255,255,255,0.04)'}`,
          color: on ? 'var(--gold-bright)' : connected ? 'rgba(255,255,255,0.75)' : 'rgba(255,255,255,0.18)',
          boxShadow: on ? '0 0 28px rgba(226,179,92,0.2), inset 0 1px 0 rgba(255,255,255,0.12)' : 'none',
          transform: on ? 'scale(0.93)' : 'scale(1)',
          transition: 'background 0.08s, border-color 0.08s, color 0.08s, transform 0.08s, box-shadow 0.08s',
          cursor: connected ? 'pointer' : 'not-allowed',
          userSelect: 'none', touchAction: 'none',
        }}
      >
        {icon}
        {!compact && <span style={{ fontSize: 8.5, fontWeight: 800, letterSpacing: '0.1em', opacity: 0.6 }}>{label}</span>}
      </button>
    )
  }

  const speedLabel = speed < 0.4 ? 'Slow' : speed < 0.72 ? 'Normal' : 'Fast'

  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 18 }}>
      <div style={{
        display: 'grid',
        gridTemplateAreas: `". up ." "left stop right" ". down ."`,
        gridTemplateColumns: `${sz}px ${sz}px ${sz}px`,
        gridTemplateRows: `${sz}px ${sz}px ${sz}px`,
        gap: compact ? 8 : 10,
      }}>
        <div style={{ gridArea: 'up' }}>    <Btn id="fwd"   icon={<ChUp />}    label="FORWARD" lx={0.3}  az={0}    /></div>
        <div style={{ gridArea: 'left' }}>  <Btn id="left"  icon={<ChLeft />}  label="LEFT"    lx={0}    az={0.7}  /></div>
        <div style={{ gridArea: 'stop' }}>
          <button
            onPointerDown={stopNow}
            style={{
              width: sz, height: sz, borderRadius: br,
              display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 4,
              background: 'rgba(255,94,94,0.1)', border: '1.5px solid rgba(255,94,94,0.28)',
              color: 'var(--danger)', cursor: 'pointer', userSelect: 'none', touchAction: 'none',
              transition: 'background 0.12s',
            }}
            onPointerEnter={e => { e.currentTarget.style.background = 'rgba(255,94,94,0.2)' }}
            onPointerLeave={e => { e.currentTarget.style.background = 'rgba(255,94,94,0.1)' }}
          >
            <StopIcon />
            {!compact && <span style={{ fontSize: 8.5, fontWeight: 800, letterSpacing: '0.1em' }}>STOP</span>}
          </button>
        </div>
        <div style={{ gridArea: 'right' }}> <Btn id="right" icon={<ChRight />} label="RIGHT"   lx={0}    az={-0.7} /></div>
        <div style={{ gridArea: 'down' }}>  <Btn id="back"  icon={<ChDown />}  label="BACK"    lx={-0.2} az={0}    /></div>
      </div>

      {/* Speed */}
      <div style={{ width: '100%', padding: '0 2px' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
          <span style={{ fontSize: 10, color: 'var(--muted)', fontWeight: 700, letterSpacing: '0.1em', textTransform: 'uppercase' }}>Speed</span>
          <span style={{ fontSize: 10.5, color: 'var(--gold-bright)', fontWeight: 700 }}>{speedLabel}</span>
        </div>
        <div style={{ position: 'relative', paddingBottom: 4 }}>
          <div style={{ height: 5, borderRadius: 99, background: 'rgba(255,255,255,0.07)' }}>
            <div style={{ height: '100%', width: `${((speed - 0.2) / 0.8) * 100}%`, background: 'linear-gradient(90deg,var(--gold),var(--gold-bright))', borderRadius: 99, transition: 'width 0.08s' }} />
          </div>
          <input
            type="range" min={0.2} max={1.0} step={0.05} value={speed}
            onChange={e => setSpeed(Number(e.target.value))}
            style={{ position: 'absolute', top: -8, left: 0, width: '100%', margin: 0, opacity: 0, cursor: 'pointer', height: 24 }}
          />
        </div>
      </div>

      {!connected && (
        <p style={{ fontSize: 11.5, color: 'rgba(255,94,94,0.7)', textAlign: 'center' }}>
          Connect to Argo to enable controls
        </p>
      )}
    </div>
  )
}
