import { createClient } from '@supabase/supabase-js'

const url = import.meta.env.VITE_SUPABASE_URL
const anonKey = import.meta.env.VITE_SUPABASE_ANON_KEY

if (!url || !anonKey) {
  // Fail loud at startup rather than a confusing "fetch failed" the first
  // time a page tries to query — this is almost always a missing .env.local.
  throw new Error(
    'VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY are not set. Copy .env.example to .env.local and fill them in.'
  )
}

// Single shared client for the whole app — supabase-js manages the session
// (localStorage) and auto-refreshes the JWT internally; every query made
// through this client automatically carries the current user's token, which
// is what Postgres RLS policies (sonic/auth_rls_schema.sql) check.
export const supabase = createClient(url, anonKey, {
  auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true },
})
