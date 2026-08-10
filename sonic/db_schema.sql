-- =============================================================================
-- db_schema.sql — Sonic voice-agent add-on
--
-- This does NOT redefine tables/entities the master schema already owns
-- (locations, robots, tasks, map_waypoints, users, service_points, customers,
-- visits, menu_categories, menu_items, orders, order_items, payments,
-- voice_commands). It only adds what sonic_agent.py needs that the master
-- schema doesn't already model: fuzzy menu-alias matching, cutlery/staff-call
-- requests, and a full multi-turn conversation log.
--
-- Load order:
--   1. robot_fleet_schema.sql     (clients, locations, robots, tasks, ...)
--   2. restaurant_ops_schema.sql  (service_points, customers, visits, menu, orders, ...)
--   3. db_schema.sql              (this file)
--
-- Apply with:  psql -U <user> -d <database> -f db_schema.sql
--
-- -----------------------------------------------------------------------------
-- How sonic_agent.py's in-memory DialogueState maps onto this schema:
-- -----------------------------------------------------------------------------
--   table_no            -> service_points.label (looked up within the robot's
--                           location; service_points.point_type='dine_in_table')
--   current_order /
--   confirm_order -> yes -> a row in orders (order_source='voice_assistant',
--                           visit_id = the guest's open visit) + order_items rows
--   bill_total           -> SUM(orders.total_amount) for the visit; see the
--                           visit_bill_totals view below instead of a stored column
--   conversation_history  -> sonic_dialogue_turns, grouped by sonic_dialogue_sessions
--   navigation / cutlery   -> a row in tasks (task_type='custom' for navigate,
--                             'delivery' for cutlery), optionally cross-linked
--                             from cutlery_requests.task_id
--   call_people             -> staff_calls (no equivalent in the master schema —
--                              it isn't a robot movement job)
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- Enumerated types — only the vocabularies the master schema doesn't already
-- cover (menu categories, order status, etc. are master schema's free-text /
-- CHECK-constrained columns; not duplicated here)
-- -----------------------------------------------------------------------------

CREATE TYPE cutlery_item AS ENUM ('spoon', 'fork', 'glass', 'knife', 'napkin');

CREATE TYPE call_target AS ENUM ('manager', 'waiter', 'cleaner');

CREATE TYPE request_status AS ENUM ('pending', 'fulfilled', 'cancelled');

CREATE TYPE turn_role AS ENUM ('user', 'robot');

-- -----------------------------------------------------------------------------
-- menu_item_aliases — spoken variants / common mis-transcriptions of a dish
-- name (e.g. "veggie burgur" -> "Veg Burger"), used by find_menu_item()'s
-- fuzzy matching. The master schema's menu_items has no equivalent.
-- -----------------------------------------------------------------------------

CREATE TABLE menu_item_aliases (
    alias_id       BIGSERIAL PRIMARY KEY,
    menu_item_id   UUID NOT NULL REFERENCES menu_items (menu_item_id) ON DELETE CASCADE,
    alias          TEXT NOT NULL,
    UNIQUE (alias)
);

CREATE INDEX idx_menu_item_aliases_menu_item_id ON menu_item_aliases (menu_item_id);

-- -----------------------------------------------------------------------------
-- cutlery_requests / cutlery_request_items — structured log of "bring me a
-- spoon and fork" requests (cutlery_handler). Optionally linked to the actual
-- robot job once one is dispatched via the master schema's tasks table.
-- -----------------------------------------------------------------------------

CREATE TABLE cutlery_requests (
    request_id        UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_point_id  UUID NOT NULL REFERENCES service_points (service_point_id),
    visit_id          UUID REFERENCES visits (visit_id),
    task_id           UUID REFERENCES tasks (task_id),   -- the robot delivery job, once dispatched
    status            request_status NOT NULL DEFAULT 'pending',
    requested_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    fulfilled_at      TIMESTAMPTZ
);

CREATE TABLE cutlery_request_items (
    request_id  UUID NOT NULL REFERENCES cutlery_requests (request_id) ON DELETE CASCADE,
    item        cutlery_item NOT NULL,
    quantity    SMALLINT NOT NULL DEFAULT 1 CHECK (quantity > 0),
    PRIMARY KEY (request_id, item)
);

-- -----------------------------------------------------------------------------
-- staff_calls — "call the waiter/manager/cleaner" requests (call_people_handler).
-- Not a robot movement job, so it has no equivalent in the master schema's
-- tasks table.
-- -----------------------------------------------------------------------------

CREATE TABLE staff_calls (
    call_id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_point_id  UUID NOT NULL REFERENCES service_points (service_point_id),
    visit_id          UUID REFERENCES visits (visit_id),
    robot_id          UUID REFERENCES robots (robot_id),      -- which robot registered the call
    call_target       call_target NOT NULL,
    status            request_status NOT NULL DEFAULT 'pending',
    requested_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at       TIMESTAMPTZ,
    resolved_by_user_id UUID REFERENCES users (user_id)
);

CREATE INDEX idx_staff_calls_pending ON staff_calls (service_point_id) WHERE status = 'pending';

-- -----------------------------------------------------------------------------
-- sonic_dialogue_sessions / sonic_dialogue_turns — one session per wake-word
-- activation (run_dialogue_session), turns mirror DialogueState.conversation_history
-- plus the NLU output for each turn. Complements the master schema's
-- voice_commands (which is a flat one-row-per-utterance log with no session
-- grouping and no robot-turn/full-slots capture) — each user turn here can
-- optionally link back to its voice_commands row for cross-analytics.
-- -----------------------------------------------------------------------------

CREATE TABLE sonic_dialogue_sessions (
    session_id        UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    robot_id          UUID NOT NULL REFERENCES robots (robot_id),
    visit_id          UUID REFERENCES visits (visit_id),
    service_point_id  UUID REFERENCES service_points (service_point_id),
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at          TIMESTAMPTZ
);

CREATE TABLE sonic_dialogue_turns (
    turn_id           BIGSERIAL PRIMARY KEY,
    session_id        UUID NOT NULL REFERENCES sonic_dialogue_sessions (session_id) ON DELETE CASCADE,
    voice_command_id  UUID REFERENCES voice_commands (voice_command_id),   -- set for role='user' turns
    role              turn_role NOT NULL,
    text              TEXT NOT NULL,
    detected_intent   TEXT,     -- NLU "intent" field, when role = 'user'
    slots             JSONB,    -- NLU "slots" object, when role = 'user'
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_sonic_dialogue_turns_session_id ON sonic_dialogue_turns (session_id);

-- -----------------------------------------------------------------------------
-- visit_bill_totals — replaces a stored "bills" table: get_bill_handler's
-- running total is just the sum of an open visit's non-cancelled orders.
-- -----------------------------------------------------------------------------

CREATE VIEW visit_bill_totals AS
SELECT
    v.visit_id,
    v.location_id,
    v.service_point_id,
    v.visit_status,
    COALESCE(SUM(o.total_amount) FILTER (WHERE o.order_status NOT IN ('cancelled', 'refunded')), 0) AS bill_total
FROM visits v
LEFT JOIN orders o ON o.visit_id = v.visit_id
GROUP BY v.visit_id, v.location_id, v.service_point_id, v.visit_status;

COMMIT;

-- =============================================================================
-- Example seed data (optional — remove if you only want the schema)
-- =============================================================================
-- INSERT INTO service_points (location_id, point_type, label, seating_capacity)
-- VALUES
--     ('<location_id>', 'dine_in_table', 'Table 1', 4),
--     ('<location_id>', 'dine_in_table', 'Table 2', 4),
--     ('<location_id>', 'dine_in_table', 'Table 3', 2);
