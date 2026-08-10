-- =============================================================================
-- auth_rls_schema.sql
--
-- Multi-tenant authentication + Row Level Security for the remote dashboard
-- (dashboard/). Adds two tenancy tables (client_members, platform_admins)
-- linking Supabase's built-in auth.users to the existing clients/locations
-- hierarchy, a set of SECURITY DEFINER helper functions in a non-exposed
-- `app` schema, and RLS policies + grants on every table in `public`.
--
-- Requires a Supabase project (this file references auth.users, which only
-- exists there — a plain Postgres install has no auth schema).
--
-- Load order: robot_fleet_schema.sql -> restaurant_ops_schema.sql ->
-- db_schema.sql -> auth_rls_schema.sql (this file, 4th). See run_migrations.py.
--
-- UNLIKE the other three schema files, this one is written to be safe to
-- re-run against a database that already has data and already has this
-- file applied (CREATE ... IF NOT EXISTS / CREATE OR REPLACE / DROP POLICY
-- IF EXISTS before CREATE POLICY everywhere). You will iterate on policies;
-- "drop the whole database and start over" can't be the retry path for a
-- policy typo the way it is for the other three files.
--
-- sonic_agent.py and backend/launcher.py need ZERO changes because of this
-- file. Both connect via DATABASE_URL as the `postgres` role, which owns
-- every table in this schema and carries BYPASSRLS — Postgres skips RLS
-- entirely for the table owner and for BYPASSRLS roles. That is exactly
-- what must stay true, which is why:
--
--     THIS FILE MUST NEVER RUN `ALTER TABLE ... FORCE ROW LEVEL SECURITY`.
--
-- FORCE ROW LEVEL SECURITY would apply RLS even to the table owner, which
-- would break the robot backends' direct DATABASE_URL connection the
-- moment it ran. RLS here exists purely to gate the dashboard's
-- direct-from-browser Supabase queries (anon key + a logged-in user's JWT
-- via PostgREST), which never have BYPASSRLS and are not the table owner.
--
-- Verify the bypass assumption holds on your project before/after applying:
--     select rolname, rolbypassrls from pg_roles
--      where rolname in ('postgres','anon','authenticated','service_role');
--     -- 'postgres' must show rolbypassrls = true
--
-- Apply with:  psql "<DATABASE_URL>" -f auth_rls_schema.sql
-- =============================================================================

SET lock_timeout = '5s';   -- ENABLE ROW LEVEL SECURITY takes a brief ACCESS
                            -- EXCLUSIVE lock; fail fast instead of queueing
                            -- behind a robot's open transaction.

BEGIN;

-- -----------------------------------------------------------------------------
-- SECTION 1: TENANCY MAPPING
-- -----------------------------------------------------------------------------

-- 1.1 Which auth.users can act on behalf of which client (a client can have
-- several staff logins; a user is normally in exactly one client's team,
-- but nothing stops more). role gates write scope everywhere below —
-- owner/manager can edit config (menu, tables), all three can do
-- day-to-day ops (place/close orders, resolve staff calls).
CREATE TABLE IF NOT EXISTS client_members (
    member_id    UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id      UUID NOT NULL REFERENCES auth.users (id) ON DELETE CASCADE,
    client_id    UUID NOT NULL REFERENCES clients (client_id) ON DELETE CASCADE,
    role         VARCHAR(20) NOT NULL DEFAULT 'staff' CHECK (role IN ('owner', 'manager', 'staff')),
    is_active    BOOLEAN NOT NULL DEFAULT true,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, client_id)
);

-- 1.2 Platform admins (you, the vendor) — a table, not a JWT claim, so
-- revocation is immediate (next query) instead of waiting out a token's
-- ~1h TTL. Deliberately no self-service: only postgres/service_role can
-- write this table (see grants below) — populate it via
-- sonic/grant_dashboard_access.py --platform-admin.
CREATE TABLE IF NOT EXISTS platform_admins (
    user_id      UUID PRIMARY KEY REFERENCES auth.users (id) ON DELETE CASCADE,
    note         TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Note: the PRE-EXISTING `users` table (robot_fleet_schema.sql) and
-- `user_location_access` are a different, older concept (password_hash-based,
-- FK'd from tasks/orders/staff_calls/audit_log for attribution) and are left
-- entirely alone here — not reconciled with auth.users. Both get RLS enabled
-- below (vendor-only: only platform admins can read them through the
-- dashboard) but no client-facing policy. Reconciling them is a Phase 4 item.

-- -----------------------------------------------------------------------------
-- SECTION 2: HELPER FUNCTIONS (schema `app`, not exposed via PostgREST —
-- Supabase's default exposed_schemas is "public, graphql_public")
-- -----------------------------------------------------------------------------

CREATE SCHEMA IF NOT EXISTS app;
GRANT USAGE ON SCHEMA app TO authenticated;

-- Every function below is STABLE SECURITY DEFINER SET search_path = ''.
-- SECURITY DEFINER is load-bearing, not cosmetic: it runs the function as
-- its owner (postgres, BYPASSRLS), which is what lets a policy ON
-- client_members call a function that itself READS client_members without
-- infinite recursion. SET search_path = '' (with fully-qualified names
-- below) prevents search-path hijacking of a definer function.
--
-- Every function also takes NO arguments and every call site wraps it as
-- `(SELECT app.fn())` / `location_id IN (SELECT app.fn())` rather than a
-- bare `app.fn(some_column)`. Both are Supabase's documented RLS
-- performance guidance: a parameterless, argument-free call is
-- uncorrelated with the row being checked, so Postgres can cache it as a
-- single InitPlan evaluated once per statement instead of once per row.

CREATE OR REPLACE FUNCTION app.is_platform_admin()
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
    SELECT EXISTS (
        SELECT 1 FROM public.platform_admins pa WHERE pa.user_id = (SELECT auth.uid())
    );
$$;

-- Any active membership, any role — the read scope.
CREATE OR REPLACE FUNCTION app.my_client_ids()
RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
    SELECT cm.client_id FROM public.client_members cm
     WHERE cm.user_id = (SELECT auth.uid()) AND cm.is_active;
$$;

CREATE OR REPLACE FUNCTION app.my_location_ids()
RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
    SELECT l.location_id FROM public.locations l
     WHERE l.client_id IN (SELECT app.my_client_ids());
$$;

-- owner|manager only — the config-write scope (menu, tables, settings).
CREATE OR REPLACE FUNCTION app.my_admin_client_ids()
RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
    SELECT cm.client_id FROM public.client_members cm
     WHERE cm.user_id = (SELECT auth.uid()) AND cm.is_active AND cm.role IN ('owner', 'manager');
$$;

CREATE OR REPLACE FUNCTION app.my_admin_location_ids()
RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
    SELECT l.location_id FROM public.locations l
     WHERE l.client_id IN (SELECT app.my_admin_client_ids());
$$;

-- -----------------------------------------------------------------------------
-- SECTION 3: SUPPORTING INDEXES (the RLS EXISTS-joins below need these —
-- several were missing even before RLS)
-- -----------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_client_members_user     ON client_members (user_id);
CREATE INDEX IF NOT EXISTS idx_client_members_client   ON client_members (client_id);
CREATE INDEX IF NOT EXISTS idx_locations_client        ON locations (client_id);
CREATE INDEX IF NOT EXISTS idx_robots_client            ON robots (client_id);
CREATE INDEX IF NOT EXISTS idx_service_points_location  ON service_points (location_id);
CREATE INDEX IF NOT EXISTS idx_visits_location          ON visits (location_id);
CREATE INDEX IF NOT EXISTS idx_menu_categories_location ON menu_categories (location_id);
CREATE INDEX IF NOT EXISTS idx_menu_items_location      ON menu_items (location_id);
CREATE INDEX IF NOT EXISTS idx_order_items_order        ON order_items (order_id);
CREATE INDEX IF NOT EXISTS idx_map_waypoints_map        ON map_waypoints (map_id);
CREATE INDEX IF NOT EXISTS idx_cutlery_requests_sp      ON cutlery_requests (service_point_id);
CREATE INDEX IF NOT EXISTS idx_staff_calls_sp           ON staff_calls (service_point_id);

-- -----------------------------------------------------------------------------
-- SECTION 4: FIX A REAL CROSS-TENANT LEAK — visit_bill_totals
-- -----------------------------------------------------------------------------
-- This view (db_schema.sql) is owned by postgres, so by default it runs
-- with postgres's privileges (including BYPASSRLS) regardless of RLS on
-- visits/orders underneath — any authenticated user querying it through
-- PostgREST would see every client's bill totals. security_invoker makes it
-- run as the CALLING user instead, so it becomes correctly scoped for free
-- once visits/orders have their own policies (below).

ALTER VIEW visit_bill_totals SET (security_invoker = on);

-- -----------------------------------------------------------------------------
-- SECTION 5: DENY BY DEFAULT
-- -----------------------------------------------------------------------------
-- Supabase's default privileges already GRANT ALL on every table to anon
-- and authenticated — i.e. every table here is fully open right now,
-- regardless of RLS. Revoke everything first, enable RLS everywhere, THEN
-- grant back only what's needed per table below. This ordering matters: a
-- table nobody's gotten to yet in section 6/7 fails CLOSED, not open.

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM authenticated;

-- -----------------------------------------------------------------------------
-- SECTION 6: ENABLE RLS EVERYWHERE (all 39 pre-existing tables + the 2 new
-- ones). No FORCE — see the file header.
-- -----------------------------------------------------------------------------

ALTER TABLE clients                  ENABLE ROW LEVEL SECURITY;
ALTER TABLE locations                ENABLE ROW LEVEL SECURITY;
ALTER TABLE users                    ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_location_access     ENABLE ROW LEVEL SECURITY;
ALTER TABLE robot_models             ENABLE ROW LEVEL SECURITY;
ALTER TABLE component_catalog        ENABLE ROW LEVEL SECURITY;
ALTER TABLE component_instances      ENABLE ROW LEVEL SECURITY;
ALTER TABLE robots                   ENABLE ROW LEVEL SECURITY;
ALTER TABLE robot_component_history  ENABLE ROW LEVEL SECURITY;
ALTER TABLE firmware_versions        ENABLE ROW LEVEL SECURITY;
ALTER TABLE software_update_log      ENABLE ROW LEVEL SECURITY;
ALTER TABLE robot_telemetry          ENABLE ROW LEVEL SECURITY;
ALTER TABLE robot_alerts             ENABLE ROW LEVEL SECURITY;
ALTER TABLE charging_sessions        ENABLE ROW LEVEL SECURITY;
ALTER TABLE navigation_maps          ENABLE ROW LEVEL SECURITY;
ALTER TABLE map_waypoints            ENABLE ROW LEVEL SECURITY;
ALTER TABLE navigation_logs          ENABLE ROW LEVEL SECURITY;
ALTER TABLE tasks                    ENABLE ROW LEVEL SECURITY;
ALTER TABLE voice_commands           ENABLE ROW LEVEL SECURITY;
ALTER TABLE voice_intents            ENABLE ROW LEVEL SECURITY;
ALTER TABLE maintenance_records      ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log                ENABLE ROW LEVEL SECURITY;
ALTER TABLE service_points           ENABLE ROW LEVEL SECURITY;
ALTER TABLE customers                ENABLE ROW LEVEL SECURITY;
ALTER TABLE visits                   ENABLE ROW LEVEL SECURITY;
ALTER TABLE menu_categories          ENABLE ROW LEVEL SECURITY;
ALTER TABLE menu_items               ENABLE ROW LEVEL SECURITY;
ALTER TABLE menu_settings            ENABLE ROW LEVEL SECURITY;
ALTER TABLE orders                   ENABLE ROW LEVEL SECURITY;
ALTER TABLE order_items              ENABLE ROW LEVEL SECURITY;
ALTER TABLE order_status_history     ENABLE ROW LEVEL SECURITY;
ALTER TABLE payments                 ENABLE ROW LEVEL SECURITY;
ALTER TABLE order_feedback           ENABLE ROW LEVEL SECURITY;
ALTER TABLE menu_item_aliases        ENABLE ROW LEVEL SECURITY;
ALTER TABLE cutlery_requests         ENABLE ROW LEVEL SECURITY;
ALTER TABLE cutlery_request_items    ENABLE ROW LEVEL SECURITY;
ALTER TABLE staff_calls              ENABLE ROW LEVEL SECURITY;
ALTER TABLE sonic_dialogue_sessions  ENABLE ROW LEVEL SECURITY;
ALTER TABLE sonic_dialogue_turns     ENABLE ROW LEVEL SECURITY;
ALTER TABLE client_members           ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform_admins          ENABLE ROW LEVEL SECURITY;

-- -----------------------------------------------------------------------------
-- SECTION 7: TENANCY TABLE POLICIES (client_members / platform_admins)
-- -----------------------------------------------------------------------------

DROP POLICY IF EXISTS client_members_select ON client_members;
CREATE POLICY client_members_select ON client_members
    FOR SELECT TO authenticated
    USING (
        user_id = (SELECT auth.uid())                    -- always see your own membership row
        OR (SELECT app.is_platform_admin())
        OR client_id IN (SELECT app.my_client_ids())      -- see your team-mates
    );

DROP POLICY IF EXISTS client_members_write ON client_members;
CREATE POLICY client_members_write ON client_members
    FOR ALL TO authenticated
    USING      ( client_id IN (SELECT app.my_admin_client_ids()) )
    WITH CHECK ( client_id IN (SELECT app.my_admin_client_ids()) );

GRANT SELECT, INSERT, UPDATE, DELETE ON client_members TO authenticated;

-- Deliberately narrow: a user may see their OWN platform-admin row (so the
-- dashboard UI can show/hide the admin view) but that flag is cosmetic only
-- — every real admin capability is enforced by app.is_platform_admin()
-- inside the policies below, not by anything the client reads directly.
DROP POLICY IF EXISTS platform_admins_self ON platform_admins;
CREATE POLICY platform_admins_self ON platform_admins
    FOR SELECT TO authenticated
    USING ( user_id = (SELECT auth.uid()) );

GRANT SELECT ON platform_admins TO authenticated;
-- No INSERT/UPDATE/DELETE grant at all — only postgres/service_role
-- (sonic/grant_dashboard_access.py) manages this table.

-- -----------------------------------------------------------------------------
-- SECTION 8: clients / locations / robots — client-scoped, column-limited
-- writes on the first two (RLS is row-level; it can't stop an owner from
-- editing their own row's subscription_plan/account_status, so those stay
-- out of the UPDATE grant instead).
-- -----------------------------------------------------------------------------

DROP POLICY IF EXISTS clients_select ON clients;
CREATE POLICY clients_select ON clients
    FOR SELECT TO authenticated
    USING ( (SELECT app.is_platform_admin()) OR client_id IN (SELECT app.my_client_ids()) );

DROP POLICY IF EXISTS clients_update ON clients;
CREATE POLICY clients_update ON clients
    FOR UPDATE TO authenticated
    USING      ( client_id IN (SELECT app.my_admin_client_ids()) )
    WITH CHECK ( client_id IN (SELECT app.my_admin_client_ids()) );
-- No INSERT/DELETE policy — provisioning a new client stays a vendor
-- (service_role / SQL console / seed_db.py) operation.

GRANT SELECT ON clients TO authenticated;
GRANT UPDATE (company_name, contact_name, contact_email, contact_phone, billing_address)
    ON clients TO authenticated;

DROP POLICY IF EXISTS locations_select ON locations;
CREATE POLICY locations_select ON locations
    FOR SELECT TO authenticated
    USING ( (SELECT app.is_platform_admin()) OR client_id IN (SELECT app.my_client_ids()) );

DROP POLICY IF EXISTS locations_update ON locations;
CREATE POLICY locations_update ON locations
    FOR UPDATE TO authenticated
    USING      ( client_id IN (SELECT app.my_admin_client_ids()) )
    WITH CHECK ( client_id IN (SELECT app.my_admin_client_ids()) );

GRANT SELECT ON locations TO authenticated;
GRANT UPDATE (location_name, address_line, city, state, country, postal_code, timezone,
              wifi_ssid, voice_nav_enabled)
    ON locations TO authenticated;   -- NOT client_id — a second guard against tenant-hopping

DROP POLICY IF EXISTS robots_select ON robots;
CREATE POLICY robots_select ON robots
    FOR SELECT TO authenticated
    USING ( (SELECT app.is_platform_admin()) OR client_id IN (SELECT app.my_client_ids()) );
-- Read-only in v1 — robots are vendor-provisioned/managed, not client-edited.
-- (robots.client_id is nullable: an unassigned robot is invisible to every
-- client and visible only to platform admins — the correct default.)

GRANT SELECT ON robots TO authenticated;

-- -----------------------------------------------------------------------------
-- SECTION 9: direct-location tables
-- -----------------------------------------------------------------------------

-- 9.1 admin-write (config: tables, menu, settings, maps)
DROP POLICY IF EXISTS service_points_select ON service_points;
CREATE POLICY service_points_select ON service_points FOR SELECT TO authenticated
    USING ( (SELECT app.is_platform_admin()) OR location_id IN (SELECT app.my_location_ids()) );
DROP POLICY IF EXISTS service_points_write ON service_points;
CREATE POLICY service_points_write ON service_points FOR ALL TO authenticated
    USING      ( location_id IN (SELECT app.my_admin_location_ids()) )
    WITH CHECK ( location_id IN (SELECT app.my_admin_location_ids()) );
GRANT SELECT, INSERT, UPDATE, DELETE ON service_points TO authenticated;

DROP POLICY IF EXISTS menu_categories_select ON menu_categories;
CREATE POLICY menu_categories_select ON menu_categories FOR SELECT TO authenticated
    USING ( (SELECT app.is_platform_admin()) OR location_id IN (SELECT app.my_location_ids()) );
DROP POLICY IF EXISTS menu_categories_write ON menu_categories;
CREATE POLICY menu_categories_write ON menu_categories FOR ALL TO authenticated
    USING      ( location_id IN (SELECT app.my_admin_location_ids()) )
    WITH CHECK ( location_id IN (SELECT app.my_admin_location_ids()) );
GRANT SELECT, INSERT, UPDATE, DELETE ON menu_categories TO authenticated;

DROP POLICY IF EXISTS menu_items_select ON menu_items;
CREATE POLICY menu_items_select ON menu_items FOR SELECT TO authenticated
    USING ( (SELECT app.is_platform_admin()) OR location_id IN (SELECT app.my_location_ids()) );
DROP POLICY IF EXISTS menu_items_write ON menu_items;
CREATE POLICY menu_items_write ON menu_items FOR ALL TO authenticated
    USING      ( location_id IN (SELECT app.my_admin_location_ids()) )
    WITH CHECK ( location_id IN (SELECT app.my_admin_location_ids()) );
GRANT SELECT, INSERT, UPDATE, DELETE ON menu_items TO authenticated;

-- menu_settings.location_id is this table's PRIMARY KEY, not a plain FK
-- column — the same predicate still applies directly.
DROP POLICY IF EXISTS menu_settings_select ON menu_settings;
CREATE POLICY menu_settings_select ON menu_settings FOR SELECT TO authenticated
    USING ( (SELECT app.is_platform_admin()) OR location_id IN (SELECT app.my_location_ids()) );
DROP POLICY IF EXISTS menu_settings_write ON menu_settings;
CREATE POLICY menu_settings_write ON menu_settings FOR ALL TO authenticated
    USING      ( location_id IN (SELECT app.my_admin_location_ids()) )
    WITH CHECK ( location_id IN (SELECT app.my_admin_location_ids()) );
GRANT SELECT, INSERT, UPDATE, DELETE ON menu_settings TO authenticated;

DROP POLICY IF EXISTS navigation_maps_select ON navigation_maps;
CREATE POLICY navigation_maps_select ON navigation_maps FOR SELECT TO authenticated
    USING ( (SELECT app.is_platform_admin()) OR location_id IN (SELECT app.my_location_ids()) );
DROP POLICY IF EXISTS navigation_maps_write ON navigation_maps;
CREATE POLICY navigation_maps_write ON navigation_maps FOR ALL TO authenticated
    USING      ( location_id IN (SELECT app.my_admin_location_ids()) )
    WITH CHECK ( location_id IN (SELECT app.my_admin_location_ids()) );
GRANT SELECT, INSERT, UPDATE, DELETE ON navigation_maps TO authenticated;

-- 9.2 ops-write (day-to-day: any active member, not just owner/manager)
DROP POLICY IF EXISTS visits_select ON visits;
CREATE POLICY visits_select ON visits FOR SELECT TO authenticated
    USING ( (SELECT app.is_platform_admin()) OR location_id IN (SELECT app.my_location_ids()) );
DROP POLICY IF EXISTS visits_write ON visits;
CREATE POLICY visits_write ON visits FOR ALL TO authenticated
    USING      ( location_id IN (SELECT app.my_location_ids()) )
    WITH CHECK ( location_id IN (SELECT app.my_location_ids()) );
GRANT SELECT, INSERT, UPDATE, DELETE ON visits TO authenticated;

DROP POLICY IF EXISTS orders_select ON orders;
CREATE POLICY orders_select ON orders FOR SELECT TO authenticated
    USING ( (SELECT app.is_platform_admin()) OR location_id IN (SELECT app.my_location_ids()) );
DROP POLICY IF EXISTS orders_write ON orders;
CREATE POLICY orders_write ON orders FOR ALL TO authenticated
    USING      ( location_id IN (SELECT app.my_location_ids()) )
    WITH CHECK ( location_id IN (SELECT app.my_location_ids()) );
GRANT SELECT, INSERT, UPDATE, DELETE ON orders TO authenticated;

-- 9.3 read-only (system/voice-generated; dashboard doesn't create these)
DROP POLICY IF EXISTS tasks_select ON tasks;
CREATE POLICY tasks_select ON tasks FOR SELECT TO authenticated
    USING ( (SELECT app.is_platform_admin()) OR location_id IN (SELECT app.my_location_ids()) );
GRANT SELECT ON tasks TO authenticated;

-- -----------------------------------------------------------------------------
-- SECTION 10: via-parent tables (no location_id column of their own — scope
-- checked through a parent FK via a correlated EXISTS wrapping the same
-- uncorrelated `IN (SELECT app.my_location_ids())`, so it still collapses
-- to one cached InitPlan)
-- -----------------------------------------------------------------------------

-- 10.1 via orders.location_id, ops-write
DROP POLICY IF EXISTS order_items_select ON order_items;
CREATE POLICY order_items_select ON order_items FOR SELECT TO authenticated
    USING ( (SELECT app.is_platform_admin())
            OR EXISTS (SELECT 1 FROM orders o WHERE o.order_id = order_items.order_id
                        AND o.location_id IN (SELECT app.my_location_ids())) );
DROP POLICY IF EXISTS order_items_write ON order_items;
CREATE POLICY order_items_write ON order_items FOR ALL TO authenticated
    USING      ( EXISTS (SELECT 1 FROM orders o WHERE o.order_id = order_items.order_id
                          AND o.location_id IN (SELECT app.my_location_ids())) )
    WITH CHECK ( EXISTS (SELECT 1 FROM orders o WHERE o.order_id = order_items.order_id
                          AND o.location_id IN (SELECT app.my_location_ids())) );
GRANT SELECT, INSERT, UPDATE, DELETE ON order_items TO authenticated;

-- 10.2 via orders.location_id, read-only
DROP POLICY IF EXISTS order_status_history_select ON order_status_history;
CREATE POLICY order_status_history_select ON order_status_history FOR SELECT TO authenticated
    USING ( (SELECT app.is_platform_admin())
            OR EXISTS (SELECT 1 FROM orders o WHERE o.order_id = order_status_history.order_id
                        AND o.location_id IN (SELECT app.my_location_ids())) );
GRANT SELECT ON order_status_history TO authenticated;

DROP POLICY IF EXISTS payments_select ON payments;
CREATE POLICY payments_select ON payments FOR SELECT TO authenticated
    USING ( (SELECT app.is_platform_admin())
            OR EXISTS (SELECT 1 FROM orders o WHERE o.order_id = payments.order_id
                        AND o.location_id IN (SELECT app.my_location_ids())) );
GRANT SELECT ON payments TO authenticated;

DROP POLICY IF EXISTS order_feedback_select ON order_feedback;
CREATE POLICY order_feedback_select ON order_feedback FOR SELECT TO authenticated
    USING ( (SELECT app.is_platform_admin())
            OR EXISTS (SELECT 1 FROM orders o WHERE o.order_id = order_feedback.order_id
                        AND o.location_id IN (SELECT app.my_location_ids())) );
GRANT SELECT ON order_feedback TO authenticated;

-- 10.3 via menu_items.location_id, admin-write
DROP POLICY IF EXISTS menu_item_aliases_select ON menu_item_aliases;
CREATE POLICY menu_item_aliases_select ON menu_item_aliases FOR SELECT TO authenticated
    USING ( (SELECT app.is_platform_admin())
            OR EXISTS (SELECT 1 FROM menu_items mi WHERE mi.menu_item_id = menu_item_aliases.menu_item_id
                        AND mi.location_id IN (SELECT app.my_location_ids())) );
DROP POLICY IF EXISTS menu_item_aliases_write ON menu_item_aliases;
CREATE POLICY menu_item_aliases_write ON menu_item_aliases FOR ALL TO authenticated
    USING      ( EXISTS (SELECT 1 FROM menu_items mi WHERE mi.menu_item_id = menu_item_aliases.menu_item_id
                          AND mi.location_id IN (SELECT app.my_admin_location_ids())) )
    WITH CHECK ( EXISTS (SELECT 1 FROM menu_items mi WHERE mi.menu_item_id = menu_item_aliases.menu_item_id
                          AND mi.location_id IN (SELECT app.my_admin_location_ids())) );
GRANT SELECT, INSERT, UPDATE, DELETE ON menu_item_aliases TO authenticated;

-- 10.4 via navigation_maps.location_id, admin-write
DROP POLICY IF EXISTS map_waypoints_select ON map_waypoints;
CREATE POLICY map_waypoints_select ON map_waypoints FOR SELECT TO authenticated
    USING ( (SELECT app.is_platform_admin())
            OR EXISTS (SELECT 1 FROM navigation_maps nm WHERE nm.map_id = map_waypoints.map_id
                        AND nm.location_id IN (SELECT app.my_location_ids())) );
DROP POLICY IF EXISTS map_waypoints_write ON map_waypoints;
CREATE POLICY map_waypoints_write ON map_waypoints FOR ALL TO authenticated
    USING      ( EXISTS (SELECT 1 FROM navigation_maps nm WHERE nm.map_id = map_waypoints.map_id
                          AND nm.location_id IN (SELECT app.my_admin_location_ids())) )
    WITH CHECK ( EXISTS (SELECT 1 FROM navigation_maps nm WHERE nm.map_id = map_waypoints.map_id
                          AND nm.location_id IN (SELECT app.my_admin_location_ids())) );
GRANT SELECT, INSERT, UPDATE, DELETE ON map_waypoints TO authenticated;

-- 10.5 via service_points.location_id, ops-write
DROP POLICY IF EXISTS cutlery_requests_select ON cutlery_requests;
CREATE POLICY cutlery_requests_select ON cutlery_requests FOR SELECT TO authenticated
    USING ( (SELECT app.is_platform_admin())
            OR EXISTS (SELECT 1 FROM service_points sp WHERE sp.service_point_id = cutlery_requests.service_point_id
                        AND sp.location_id IN (SELECT app.my_location_ids())) );
DROP POLICY IF EXISTS cutlery_requests_write ON cutlery_requests;
CREATE POLICY cutlery_requests_write ON cutlery_requests FOR ALL TO authenticated
    USING      ( EXISTS (SELECT 1 FROM service_points sp WHERE sp.service_point_id = cutlery_requests.service_point_id
                          AND sp.location_id IN (SELECT app.my_location_ids())) )
    WITH CHECK ( EXISTS (SELECT 1 FROM service_points sp WHERE sp.service_point_id = cutlery_requests.service_point_id
                          AND sp.location_id IN (SELECT app.my_location_ids())) );
GRANT SELECT, INSERT, UPDATE, DELETE ON cutlery_requests TO authenticated;

DROP POLICY IF EXISTS staff_calls_select ON staff_calls;
CREATE POLICY staff_calls_select ON staff_calls FOR SELECT TO authenticated
    USING ( (SELECT app.is_platform_admin())
            OR EXISTS (SELECT 1 FROM service_points sp WHERE sp.service_point_id = staff_calls.service_point_id
                        AND sp.location_id IN (SELECT app.my_location_ids())) );
DROP POLICY IF EXISTS staff_calls_write ON staff_calls;
CREATE POLICY staff_calls_write ON staff_calls FOR ALL TO authenticated
    USING      ( EXISTS (SELECT 1 FROM service_points sp WHERE sp.service_point_id = staff_calls.service_point_id
                          AND sp.location_id IN (SELECT app.my_location_ids())) )
    WITH CHECK ( EXISTS (SELECT 1 FROM service_points sp WHERE sp.service_point_id = staff_calls.service_point_id
                          AND sp.location_id IN (SELECT app.my_location_ids())) );
GRANT SELECT, INSERT, UPDATE, DELETE ON staff_calls TO authenticated;

-- 10.6 two-hop: cutlery_request_items -> cutlery_requests -> service_points.location_id
DROP POLICY IF EXISTS cutlery_request_items_select ON cutlery_request_items;
CREATE POLICY cutlery_request_items_select ON cutlery_request_items FOR SELECT TO authenticated
    USING ( (SELECT app.is_platform_admin())
            OR EXISTS (
                SELECT 1 FROM cutlery_requests cr
                JOIN service_points sp ON sp.service_point_id = cr.service_point_id
                WHERE cr.request_id = cutlery_request_items.request_id
                  AND sp.location_id IN (SELECT app.my_location_ids())
            ) );
DROP POLICY IF EXISTS cutlery_request_items_write ON cutlery_request_items;
CREATE POLICY cutlery_request_items_write ON cutlery_request_items FOR ALL TO authenticated
    USING      ( EXISTS (
                SELECT 1 FROM cutlery_requests cr
                JOIN service_points sp ON sp.service_point_id = cr.service_point_id
                WHERE cr.request_id = cutlery_request_items.request_id
                  AND sp.location_id IN (SELECT app.my_location_ids())
            ) )
    WITH CHECK ( EXISTS (
                SELECT 1 FROM cutlery_requests cr
                JOIN service_points sp ON sp.service_point_id = cr.service_point_id
                WHERE cr.request_id = cutlery_request_items.request_id
                  AND sp.location_id IN (SELECT app.my_location_ids())
            ) );
GRANT SELECT, INSERT, UPDATE, DELETE ON cutlery_request_items TO authenticated;

-- -----------------------------------------------------------------------------
-- SECTION 11: customers — no location/client scoping exists on this table
-- at all (a single global guest table). Zero dashboard access in v1 rather
-- than a rushed policy — see the vendor-only block below (it's included
-- there, not given a client policy here).
-- -----------------------------------------------------------------------------
-- (intentionally no policy in this section; customers is handled in Section 12)

-- -----------------------------------------------------------------------------
-- SECTION 12: vendor-only tables. RLS on, but the ONLY policy granted to
-- `authenticated` is scoped to platform admins — every other logged-in user
-- gets zero rows. These are fleet/hardware-management, audit, or legacy
-- tables the dashboard MVP has no client-facing use for.
-- -----------------------------------------------------------------------------

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'users', 'user_location_access', 'robot_models', 'component_catalog',
        'component_instances', 'robot_component_history', 'firmware_versions',
        'software_update_log', 'robot_telemetry', 'robot_alerts', 'charging_sessions',
        'navigation_logs', 'voice_commands', 'voice_intents', 'maintenance_records',
        'audit_log', 'sonic_dialogue_sessions', 'sonic_dialogue_turns', 'customers'
    ]
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_admin_select', t);
        EXECUTE format(
            'CREATE POLICY %I ON %I FOR SELECT TO authenticated USING ( (SELECT app.is_platform_admin()) )',
            t || '_admin_select', t
        );
        EXECUTE format('GRANT SELECT ON %I TO authenticated', t);
    END LOOP;
END $$;

-- -----------------------------------------------------------------------------
-- SECTION 13: view grants (must come after visits/orders have their own
-- policies above, since security_invoker makes this view depend on them)
-- -----------------------------------------------------------------------------

GRANT SELECT ON visit_bill_totals TO authenticated;

COMMIT;

-- =============================================================================
-- Post-apply audit — both must return ZERO rows. Run these every time this
-- file changes, before trusting the result.
-- =============================================================================
-- -- any public table with RLS still off:
-- select relname from pg_class c join pg_namespace n on n.oid=c.relnamespace
--  where n.nspname='public' and c.relkind='r' and not c.relrowsecurity;
--
-- -- any table with RLS on but zero policies at all (PostgREST returns [],
-- -- not an error, for this case — the #1 "why is my dashboard blank" cause):
-- select relname from pg_class c join pg_namespace n on n.oid=c.relnamespace
--  where n.nspname='public' and c.relkind='r' and c.relrowsecurity
--    and not exists (select 1 from pg_policy p where p.polrelid=c.oid);
