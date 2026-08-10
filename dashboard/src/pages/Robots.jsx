import { useEffect, useState } from 'react'
import { supabase } from '../lib/supabase'
import { useLocation } from '../lib/LocationContext'

const STATUS_COLOR = {
  active: 'var(--ok)', idle: 'var(--blue)', charging: 'var(--blue)',
  offline: 'var(--muted)', maintenance: 'var(--gold-bright)', error: 'var(--danger)', retired: 'var(--muted)',
}

export default function Robots() {
  const { selectedLocationId } = useLocation()
  const [robots, setRobots] = useState([])
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!selectedLocationId) return
    setLoading(true)
    supabase
      .from('robots')
      .select('robot_id, robot_uid, current_status, last_seen_at, ip_address')
      .eq('location_id', selectedLocationId)
      .order('robot_uid')
      .then(({ data }) => { setRobots(data || []); setLoading(false) })
  }, [selectedLocationId])

  if (!selectedLocationId) return <p style={{ color: 'var(--muted)' }}>No location selected yet.</p>

  return (
    <div style={{ maxWidth: 900, margin: '0 auto' }}>
      <div style={{ fontFamily: 'var(--font-heading)', fontWeight: 700, fontSize: 20, marginBottom: 20 }}>Robots</div>

      {loading ? (
        <p style={{ color: 'var(--muted)' }}>Loading…</p>
      ) : robots.length === 0 ? (
        <p style={{ color: 'var(--muted)' }}>No robots at this location yet.</p>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {robots.map(r => (
            <div key={r.robot_id} className="glass-dense" style={{ padding: '12px 16px', display: 'flex', alignItems: 'center', gap: 14 }}>
              <span style={{ width: 9, height: 9, borderRadius: 99, background: STATUS_COLOR[r.current_status] || 'var(--muted)' }} />
              <div style={{ flex: 1 }}>
                <div style={{ fontWeight: 600, fontSize: 14 }}>{r.robot_uid}</div>
                <div style={{ fontSize: 11.5, color: 'var(--muted)' }}>
                  {r.current_status} · last seen {r.last_seen_at ? new Date(r.last_seen_at).toLocaleString() : 'never'}
                </div>
              </div>
              <div style={{ fontSize: 11.5, color: 'var(--muted)' }}>{r.ip_address || ''}</div>
            </div>
          ))}
        </div>
      )}

      <p style={{ fontSize: 11.5, color: 'var(--muted)', marginTop: 20 }}>
        Read-only — robot control and navigation stay on the on-site local
        dashboard, not here.
      </p>
    </div>
  )
}
