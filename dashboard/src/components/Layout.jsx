import { NavLink, Outlet } from 'react-router-dom'
import { useAuth } from '../lib/AuthContext'
import { useLocation as useTenantLocation } from '../lib/LocationContext'

const navLinkStyle = ({ isActive }) => ({
  padding: '8px 14px', borderRadius: 8, fontSize: 13, fontWeight: 600,
  color: isActive ? 'var(--gold-bright)' : 'var(--muted)',
  background: isActive ? 'rgba(226,179,92,0.12)' : 'transparent',
})

export default function Layout() {
  const { user, memberships, selectedClientId, setSelectedClientId, isPlatformAdmin, signOut } = useAuth()
  const { locations, selectedLocationId, setSelectedLocationId } = useTenantLocation()

  return (
    <div style={{ minHeight: '100vh', display: 'flex', flexDirection: 'column' }}>
      <header
        className="glass-dense"
        style={{
          display: 'flex', alignItems: 'center', gap: 20, padding: '14px 24px',
          borderRadius: 0, borderBottom: '1px solid var(--border-glass)',
        }}
      >
        <div style={{ fontFamily: 'var(--font-heading)', fontWeight: 700, fontSize: 15, marginRight: 8 }}>
          Argo Fleet
        </div>

        <nav style={{ display: 'flex', gap: 6, flex: 1 }}>
          <NavLink to="/" end style={navLinkStyle}>Home</NavLink>
          <NavLink to="/menu" style={navLinkStyle}>Menu</NavLink>
          <NavLink to="/orders" style={navLinkStyle}>Orders</NavLink>
          <NavLink to="/robots" style={navLinkStyle}>Robots</NavLink>
          {isPlatformAdmin && <NavLink to="/admin/clients" style={navLinkStyle}>All clients</NavLink>}
        </nav>

        {memberships.length > 1 && (
          <select
            value={selectedClientId || ''}
            onChange={e => setSelectedClientId(e.target.value)}
            style={{ background: 'rgba(255,255,255,0.04)', border: '1px solid var(--border-glass)', color: 'var(--text)', borderRadius: 8, padding: '6px 10px', fontSize: 12.5 }}
          >
            {memberships.map(m => (
              <option key={m.client_id} value={m.client_id}>{m.clients?.company_name || m.client_id}</option>
            ))}
          </select>
        )}

        {locations.length > 1 && (
          <select
            value={selectedLocationId || ''}
            onChange={e => setSelectedLocationId(e.target.value)}
            style={{ background: 'rgba(255,255,255,0.04)', border: '1px solid var(--border-glass)', color: 'var(--text)', borderRadius: 8, padding: '6px 10px', fontSize: 12.5 }}
          >
            {locations.map(l => (
              <option key={l.location_id} value={l.location_id}>{l.location_name}</option>
            ))}
          </select>
        )}

        <div style={{ fontSize: 12, color: 'var(--muted)' }}>{user?.email}</div>
        <button onClick={signOut} className="btn-ghost" style={{ padding: '6px 12px', fontSize: 12 }}>Sign out</button>
      </header>

      <main style={{ flex: 1, padding: 24 }}>
        <Outlet />
      </main>
    </div>
  )
}
