import { useCallback, useEffect, useState } from 'react'
import { supabase } from '../lib/supabase'
import { useLocation } from '../lib/LocationContext'

const POLL_MS = 5000

// Remote mirror of backend/launcher.py's _read_orders_db() (per-table
// active-visit orders) and _clear_table_order() (close = clear).
export default function Orders() {
  const { selectedLocationId } = useLocation()
  const [tables, setTables] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    if (!selectedLocationId) return
    const { data, error: queryError } = await supabase
      .from('service_points')
      .select(`
        service_point_id, label,
        visits!inner (
          visit_id, visit_status, checked_in_at,
          orders (
            order_id, order_status, total_amount, placed_at,
            order_items ( quantity, unit_price, menu_items ( item_name ) )
          )
        )
      `)
      .eq('location_id', selectedLocationId)
      .eq('visits.visit_status', 'active')
      .order('label')
    if (queryError) { setError(queryError.message); return }
    setTables(data || [])
    setError(null)
  }, [selectedLocationId])

  useEffect(() => {
    setLoading(true)
    load().finally(() => setLoading(false))
    const id = setInterval(load, POLL_MS)
    return () => clearInterval(id)
  }, [load])

  const closeTable = async (visitId) => {
    const { error: updateError } = await supabase
      .from('visits')
      .update({ visit_status: 'closed', checked_out_at: new Date().toISOString() })
      .eq('visit_id', visitId)
    if (updateError) { setError(updateError.message); return }
    load()
  }

  if (!selectedLocationId) return <p style={{ color: 'var(--muted)' }}>No location selected yet.</p>

  return (
    <div style={{ maxWidth: 900, margin: '0 auto' }}>
      <div style={{ fontFamily: 'var(--font-heading)', fontWeight: 700, fontSize: 20, marginBottom: 20 }}>
        Tables &amp; orders
      </div>

      {error && <div style={{ color: 'var(--danger)', fontSize: 13, marginBottom: 12 }}>{error}</div>}

      {loading ? (
        <p style={{ color: 'var(--muted)' }}>Loading…</p>
      ) : tables.length === 0 ? (
        <p style={{ color: 'var(--muted)' }}>No active tables right now.</p>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {tables.map(sp => {
            const visit = sp.visits?.[0]
            if (!visit) return null
            const allItems = (visit.orders || []).flatMap(o => o.order_items || [])
            const total = (visit.orders || []).reduce((sum, o) => sum + Number(o.total_amount || 0), 0)
            return (
              <div key={sp.service_point_id} className="glass-dense" style={{ padding: '14px 18px' }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
                  <div style={{ fontWeight: 700 }}>{sp.label}</div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                    <div style={{ fontSize: 13, color: 'var(--gold-bright)', fontWeight: 700 }}>{total.toFixed(2)}</div>
                    <button className="btn-ghost" style={{ padding: '5px 12px', fontSize: 11.5 }} onClick={() => closeTable(visit.visit_id)}>
                      Close table
                    </button>
                  </div>
                </div>
                {allItems.length === 0 ? (
                  <div style={{ fontSize: 12.5, color: 'var(--muted)' }}>No items ordered yet.</div>
                ) : (
                  <div style={{ fontSize: 12.5, color: 'var(--muted)' }}>
                    {allItems.map((it, i) => (
                      <span key={i}>
                        {it.quantity}x {it.menu_items?.item_name}{i < allItems.length - 1 ? ', ' : ''}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
