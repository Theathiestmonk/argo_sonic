import { Navigate } from 'react-router-dom'
import { useAuth } from '../lib/AuthContext'

export default function RequireAuth({ children }) {
  const { session, loading, membershipsLoaded, memberships, isPlatformAdmin } = useAuth()

  if (loading || (session && !membershipsLoaded)) {
    return (
      <div style={{ display: 'flex', height: '100vh', alignItems: 'center', justifyContent: 'center', color: 'var(--muted)' }}>
        Loading…
      </div>
    )
  }

  if (!session) return <Navigate to="/login" replace />

  if (!isPlatformAdmin && memberships.length === 0) {
    return (
      <div style={{ display: 'flex', height: '100vh', alignItems: 'center', justifyContent: 'center', padding: 24 }}>
        <div className="glass-dense" style={{ padding: 32, maxWidth: 420, textAlign: 'center' }}>
          <div style={{ fontFamily: 'var(--font-heading)', fontWeight: 700, fontSize: 17, marginBottom: 10 }}>
            No restaurant linked to your account
          </div>
          <p style={{ fontSize: 13.5, color: 'var(--muted)', lineHeight: 1.7 }}>
            Your login isn't associated with any client yet. Contact whoever
            set up your robot to link your account.
          </p>
        </div>
      </div>
    )
  }

  return children
}
