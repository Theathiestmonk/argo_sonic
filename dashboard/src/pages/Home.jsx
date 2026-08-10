import { useEffect, useState } from 'react'
import { supabase } from '../lib/supabase'
import { useLocation } from '../lib/LocationContext'

export default function Home() {
  const { selectedLocationId, locations } = useLocation()
  const [counts, setCounts] = useState({ menuItems: null, activeVisits: null, robots: null })

  useEffect(() => {
    if (!selectedLocationId) return
    let cancelled = false
    Promise.all([
      supabase.from('menu_items').select('menu_item_id', { count: 'exact', head: true }).eq('location_id', selectedLocationId),
      supabase.from('visits').select('visit_id', { count: 'exact', head: true }).eq('location_id', selectedLocationId).eq('visit_status', 'active'),
      supabase.from('robots').select('robot_id', { count: 'exact', head: true }).eq('location_id', selectedLocationId),
    ]).then(([menu, visits, robots]) => {
      if (cancelled) return
      setCounts({ menuItems: menu.count ?? 0, activeVisits: visits.count ?? 0, robots: robots.count ?? 0 })
    })
    return () => { cancelled = true }
  }, [selectedLocationId])

  const location = locations.find(l => l.location_id === selectedLocationId)

  if (!selectedLocationId) {
    return <p style={{ color: 'var(--muted)' }}>No location selected yet.</p>
  }

  const tiles = [
    { label: 'Menu items', value: counts.menuItems },
    { label: 'Active tables', value: counts.activeVisits },
    { label: 'Robots', value: counts.robots },
  ]

  return (
    <div style={{ maxWidth: 900, margin: '0 auto' }}>
      <div style={{ fontFamily: 'var(--font-heading)', fontWeight: 700, fontSize: 20, marginBottom: 4 }}>
        {location?.location_name || 'Location'}
      </div>
      <p style={{ fontSize: 13, color: 'var(--muted)', marginBottom: 24 }}>
        {location?.city}
      </p>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: 14 }}>
        {tiles.map(t => (
          <div key={t.label} className="glass-dense" style={{ padding: 20 }}>
            <div className="label-xs" style={{ marginBottom: 8 }}>{t.label}</div>
            <div style={{ fontSize: 28, fontWeight: 700, fontFamily: 'var(--font-heading)' }}>
              {t.value === null ? '…' : t.value}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
