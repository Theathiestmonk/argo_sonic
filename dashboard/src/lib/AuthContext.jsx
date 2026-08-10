import { createContext, useContext, useEffect, useState, useCallback } from 'react'
import { supabase } from './supabase'

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [session, setSession] = useState(null)
  const [loading, setLoading] = useState(true)          // true until the FIRST session check resolves
  const [memberships, setMemberships] = useState([])     // [{client_id, role, clients: {company_name}}]
  const [isPlatformAdmin, setIsPlatformAdmin] = useState(false)
  const [membershipsLoaded, setMembershipsLoaded] = useState(false)
  const [selectedClientId, setSelectedClientId] = useState(null)

  const loadMemberships = useCallback(async () => {
    setMembershipsLoaded(false)
    const [membersRes, adminRes] = await Promise.all([
      supabase
        .from('client_members')
        .select('client_id, role, clients ( company_name )')
        .eq('is_active', true),
      // Own row only, per platform_admins_self policy — presence = admin.
      // This flag is cosmetic (drives which nav links render); every real
      // admin capability is enforced server-side by RLS via
      // app.is_platform_admin(), not by this client-side read.
      supabase.from('platform_admins').select('user_id').maybeSingle(),
    ])
    setMemberships(membersRes.data || [])
    setIsPlatformAdmin(!!adminRes.data)
    setSelectedClientId(prev => prev || membersRes.data?.[0]?.client_id || null)
    setMembershipsLoaded(true)
  }, [])

  useEffect(() => {
    let unsubscribed = false

    // Two-step hydrate: getSession() resolves the persisted/localStorage
    // session once on load; onAuthStateChange keeps it in sync after
    // (sign-in, sign-out, token refresh). Holding `loading` until the first
    // of these resolves avoids a login-page flash on every page refresh.
    supabase.auth.getSession().then(({ data }) => {
      if (unsubscribed) return
      setSession(data.session)
      setLoading(false)
      if (data.session) loadMemberships()
    })

    const { data: sub } = supabase.auth.onAuthStateChange((_event, next) => {
      setSession(next)
      if (next) {
        loadMemberships()
      } else {
        setMemberships([])
        setIsPlatformAdmin(false)
        setMembershipsLoaded(false)
        setSelectedClientId(null)
      }
    })

    return () => {
      unsubscribed = true
      sub.subscription.unsubscribe()
    }
  }, [loadMemberships])

  const signIn = (email, password) => supabase.auth.signInWithPassword({ email, password })
  const signOut = () => supabase.auth.signOut()

  const value = {
    session,
    user: session?.user ?? null,
    loading,
    memberships,
    membershipsLoaded,
    isPlatformAdmin,
    selectedClientId,
    setSelectedClientId,
    signIn,
    signOut,
    refreshMemberships: loadMemberships,
  }

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth() must be used inside <AuthProvider>')
  return ctx
}
