import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { supabase } from '../../lib/supabase'
import { useAuth } from '../../lib/AuthContext'

// Platform-admin-only route (RequireAuth doesn't gate this specifically —
// the query itself only returns rows because of app.is_platform_admin() in
// clients_select; a non-admin hitting this URL just sees an empty list).
export default function Clients() {
  const { setSelectedClientId } = useAuth()
  const navigate = useNavigate()
  const [clients, setClients] = useState([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    supabase
      .from('clients')
      .select('client_id, company_name, business_type, account_status')
      .order('company_name')
      .then(({ data }) => { setClients(data || []); setLoading(false) })
  }, [])

  const pick = (clientId) => {
    setSelectedClientId(clientId)
    navigate('/')
  }

  return (
    <div style={{ maxWidth: 900, margin: '0 auto' }}>
      <div style={{ fontFamily: 'var(--font-heading)', fontWeight: 700, fontSize: 20, marginBottom: 20 }}>
        All clients (platform admin)
      </div>

      {loading ? (
        <p style={{ color: 'var(--muted)' }}>Loading…</p>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {clients.map(c => (
            <div key={c.client_id} className="glass-dense" style={{ padding: '12px 16px', display: 'flex', alignItems: 'center', gap: 14 }}>
              <div style={{ flex: 1 }}>
                <div style={{ fontWeight: 600, fontSize: 14 }}>{c.company_name}</div>
                <div style={{ fontSize: 11.5, color: 'var(--muted)' }}>{c.business_type} · {c.account_status}</div>
              </div>
              <button className="btn-ghost" style={{ padding: '6px 14px', fontSize: 12 }} onClick={() => pick(c.client_id)}>
                View
              </button>
            </div>
          ))}
          {clients.length === 0 && <p style={{ color: 'var(--muted)' }}>No clients yet.</p>}
        </div>
      )}
    </div>
  )
}
