import { useState } from 'react'
import { Navigate } from 'react-router-dom'
import { useAuth } from '../lib/AuthContext'

export default function Login() {
  const { session, loading, signIn } = useAuth()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState(null)
  const [submitting, setSubmitting] = useState(false)

  if (!loading && session) return <Navigate to="/" replace />

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError(null)
    setSubmitting(true)
    const { error: signInError } = await signIn(email, password)
    setSubmitting(false)
    if (signInError) setError(signInError.message)
  }

  return (
    <div style={{ display: 'flex', height: '100vh', alignItems: 'center', justifyContent: 'center', padding: 24 }}>
      <form onSubmit={handleSubmit} className="glass-dense" style={{ padding: 32, width: '100%', maxWidth: 380 }}>
        <div style={{ fontFamily: 'var(--font-heading)', fontWeight: 700, fontSize: 20, marginBottom: 4 }}>
          Argo Fleet Dashboard
        </div>
        <p style={{ fontSize: 13, color: 'var(--muted)', marginBottom: 24 }}>
          Sign in with the account your robot's setup created for you.
        </p>

        <label className="label-xs" style={{ display: 'block', marginBottom: 6 }}>Email</label>
        <input
          type="email" required autoFocus value={email} onChange={e => setEmail(e.target.value)}
          style={{
            width: '100%', padding: '10px 12px', marginBottom: 16, borderRadius: 8,
            background: 'rgba(255,255,255,0.04)', border: '1px solid var(--border-glass)', color: 'var(--text)',
          }}
        />

        <label className="label-xs" style={{ display: 'block', marginBottom: 6 }}>Password</label>
        <input
          type="password" required value={password} onChange={e => setPassword(e.target.value)}
          style={{
            width: '100%', padding: '10px 12px', marginBottom: 20, borderRadius: 8,
            background: 'rgba(255,255,255,0.04)', border: '1px solid var(--border-glass)', color: 'var(--text)',
          }}
        />

        {error && (
          <div style={{ fontSize: 12.5, color: 'var(--danger)', marginBottom: 16 }}>{error}</div>
        )}

        <button type="submit" disabled={submitting} className="btn-primary" style={{ width: '100%' }}>
          {submitting ? 'Signing in…' : 'Sign in'}
        </button>

        <p style={{ fontSize: 11.5, color: 'var(--muted)', marginTop: 18, lineHeight: 1.6 }}>
          No self-serve signup — accounts are created by your robot vendor.
          Contact them if you don't have credentials yet.
        </p>
      </form>
    </div>
  )
}
