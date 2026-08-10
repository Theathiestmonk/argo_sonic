import { createContext, useContext, useEffect, useState } from 'react'
import { supabase } from './supabase'
import { useAuth } from './AuthContext'

const LocationContext = createContext(null)

// Locations for the currently-selected client (most clients have exactly
// one — this still works, it's just a one-item dropdown). Platform admins
// additionally get a client picker (see pages/admin/Clients.jsx) that sets
// selectedClientId in AuthContext; this context reacts to that.
export function LocationProvider({ children }) {
  const { selectedClientId } = useAuth()
  const [locations, setLocations] = useState([])
  const [selectedLocationId, setSelectedLocationId] = useState(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!selectedClientId) {
      setLocations([])
      setSelectedLocationId(null)
      return
    }
    let cancelled = false
    setLoading(true)
    supabase
      .from('locations')
      .select('location_id, location_name, city, voice_nav_enabled')
      .eq('client_id', selectedClientId)
      .then(({ data }) => {
        if (cancelled) return
        setLocations(data || [])
        setSelectedLocationId(prev =>
          data?.some(l => l.location_id === prev) ? prev : data?.[0]?.location_id ?? null
        )
        setLoading(false)
      })
    return () => { cancelled = true }
  }, [selectedClientId])

  return (
    <LocationContext.Provider value={{ locations, selectedLocationId, setSelectedLocationId, loading }}>
      {children}
    </LocationContext.Provider>
  )
}

export function useLocation() {
  const ctx = useContext(LocationContext)
  if (!ctx) throw new Error('useLocation() must be used inside <LocationProvider>')
  return ctx
}
