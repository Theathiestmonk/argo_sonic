# Argo Fleet Dashboard

Remote, multi-tenant web app for restaurant owners to log in and manage
their own menu/tables/robots, plus a cross-client view for the platform
admin (vendor). Separate from `frontend/` — that one is the local,
per-robot operator UI installed on each Jetson; this one is deployed
remotely and talks directly to Supabase.

Data access is entirely through Supabase's client SDK (`@supabase/supabase-js`)
using the public anon key — **Row Level Security is the only thing
protecting data**, not secrecy of that key (see
`sonic/auth_rls_schema.sql`). This app never touches `DATABASE_URL` or the
`service_role` key; those stay on the trusted robot backends
(`sonic/sonic_agent.py`, `backend/launcher.py`).

## Setup

Before running this, `sonic/auth_rls_schema.sql` must already be applied
(`python sonic/run_migrations.py`) and at least one login granted
(`python sonic/grant_dashboard_access.py ...`) — see `sonic/README.md`.

```bash
npm install
cp .env.example .env.local   # fill in VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY
                              # from Supabase: Project Settings -> API
npm run dev
```

## Deploying

This is a static Vite build — deploy `dist/` to Vercel/Netlify/any static
host after `npm run build`, with the same two `VITE_SUPABASE_*` env vars set
in the host's dashboard. It is never installed on a robot and is not part
of `sh/install-services.sh`.

## Structure

```
src/
  lib/supabase.js        createClient() — the one place the anon key is read
  lib/AuthContext.jsx     session (getSession + onAuthStateChange) + this
                          user's client_members / platform_admins rows
  lib/LocationContext.jsx locations for the selected client + which one is
                          "current" (drives every page's queries)
  components/RequireAuth.jsx  route guard — redirects to /login, or shows
                               a "no restaurant linked" screen if the user
                               has no membership and isn't a platform admin
  components/Layout.jsx   header (nav + client/location pickers + sign out)
  pages/Login.jsx, Home.jsx, Menu.jsx, Orders.jsx, Robots.jsx,
  pages/admin/Clients.jsx (platform-admin only, in practice — RLS returns
                           an empty list for anyone else who navigates here)
```

## Known sharp edges (v1, see sonic/auth_rls_schema.sql's plan doc for more)

- **Menu edits can race with the on-robot `menu.html`.** `backend/launcher.py`'s
  `POST /menu` does a full-overwrite save; this app writes row-by-row
  instead specifically to avoid clobbering concurrent edits from the other
  side, but two edits to the *same* item at the *same* time still
  last-writer-wins with no conflict warning.
- **Menu item images** are base64 data URIs, not links — `pages/Menu.jsx`'s
  list query deliberately excludes `image_url` and only fetches it when you
  open an item's editor, to avoid pulling megabytes on every menu load.
- No self-serve signup, password reset flows use Supabase's default hosted
  pages, and provisioning new clients/locations/robots is a
  `sonic/seed_db.py` / SQL-console operation, not a page in this app.
