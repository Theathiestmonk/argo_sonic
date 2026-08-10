-- =============================================================================
-- restaurant_ops_schema.sql
--
-- Front-of-house restaurant operations: physical service points (tables /
-- rooms / seats), customers/guests, visits, menu, orders, payments, feedback.
--
-- Depends on robot_fleet_schema.sql (locations, robots, tasks, map_waypoints,
-- users) — load that first. This file's cross-schema foreign keys are wired
-- up directly below (not left commented-out) since both files ship together.
--
-- Apply with:  psql -U <user> -d <database> -f restaurant_ops_schema.sql
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- SECTION 10: PHYSICAL LAYOUT — TABLES / ROOMS WITHIN A LOCATION
-- -----------------------------------------------------------------------------

-- 10.1 Every seating point an order can originate from or be delivered to
-- (a restaurant table, a hotel room, a bar counter seat, etc.)
CREATE TABLE service_points (
    service_point_id    UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    location_id          UUID NOT NULL REFERENCES locations (location_id) ON DELETE CASCADE,
    point_type            VARCHAR(30) CHECK (point_type IN
                           ('dine_in_table', 'hotel_room', 'bar_seat', 'counter', 'home_address', 'other')),
    label                  VARCHAR(50) NOT NULL,    -- e.g. "Table 4", "Room 204", "Seat B3"
    seating_capacity        INTEGER,
    waypoint_id               UUID REFERENCES map_waypoints (waypoint_id),   -- so the robot knows where to go
    is_active                  BOOLEAN DEFAULT true,
    created_at                   TIMESTAMPTZ DEFAULT now()
);

-- -----------------------------------------------------------------------------
-- SECTION 11: CUSTOMERS / GUESTS
-- -----------------------------------------------------------------------------

-- 11.1 Customer profile (optional — guests can also order anonymously as a walk-in)
CREATE TABLE customers (
    customer_id       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    full_name           VARCHAR(150),
    phone                 VARCHAR(20),
    email                   VARCHAR(150),
    loyalty_points            INTEGER DEFAULT 0,
    created_at                  TIMESTAMPTZ DEFAULT now()
);

-- 11.2 A customer's visit/session at a location (groups multiple orders
-- together, e.g. a table that orders drinks, then food, then dessert
-- across one sitting)
CREATE TABLE visits (
    visit_id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    location_id            UUID NOT NULL REFERENCES locations (location_id),
    service_point_id         UUID REFERENCES service_points (service_point_id),
    customer_id                UUID REFERENCES customers (customer_id),   -- nullable for anonymous walk-ins
    guest_count                  INTEGER DEFAULT 1,
    checked_in_at                  TIMESTAMPTZ DEFAULT now(),
    checked_out_at                   TIMESTAMPTZ,
    visit_status                       VARCHAR(20) DEFAULT 'active' CHECK (visit_status IN
                                        ('active', 'closed', 'no_show'))
);

-- -----------------------------------------------------------------------------
-- SECTION 12: MENU
-- -----------------------------------------------------------------------------

CREATE TABLE menu_categories (
    category_id       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    location_id         UUID NOT NULL REFERENCES locations (location_id) ON DELETE CASCADE,
    name                  VARCHAR(100) NOT NULL,    -- e.g. "Starters", "Drinks", "Mains"
    display_order           INTEGER DEFAULT 0
);

CREATE TABLE menu_items (
    menu_item_id       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    category_id          UUID REFERENCES menu_categories (category_id),
    location_id             UUID NOT NULL REFERENCES locations (location_id) ON DELETE CASCADE,
    item_name                 VARCHAR(150) NOT NULL,
    description                 TEXT,
    price                         DECIMAL(10, 2) NOT NULL,
    is_available                    BOOLEAN DEFAULT true,
    prep_time_minutes                 INTEGER,
    image_url                           TEXT,    -- staff editor stores a base64 data: URI here, not a real URL
    -- Staff-editor-only display fields with no voice-agent meaning (stock
    -- count, discount%/flag, promo label, diet tag) — kept as opaque JSON
    -- instead of named columns so menu.html's item shape can evolve without
    -- schema migrations; sonic_agent.py never reads this column.
    extra                                JSONB DEFAULT '{}'::jsonb,
    created_at                            TIMESTAMPTZ DEFAULT now()
);

-- 12.1 One row per location: the staff editor's currency/tax settings that
-- accompany the menu (menu.html's "settings" object) plus a last-saved
-- timestamp, mirroring the old menu.json's top-level {settings, savedAt}.
CREATE TABLE menu_settings (
    location_id       UUID PRIMARY KEY REFERENCES locations (location_id) ON DELETE CASCADE,
    currency_code     VARCHAR(10) DEFAULT 'USD',
    tax_percent       DECIMAL(5, 2) DEFAULT 0,
    saved_at          TIMESTAMPTZ
);

-- -----------------------------------------------------------------------------
-- SECTION 13: ORDERS — the core "who ordered what, from where, and when"
-- -----------------------------------------------------------------------------

CREATE TABLE orders (
    order_id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    order_number              VARCHAR(30) UNIQUE NOT NULL,    -- human-friendly, e.g. "ORD-20260729-0042"
    location_id                 UUID NOT NULL REFERENCES locations (location_id),
    visit_id                      UUID REFERENCES visits (visit_id),
    service_point_id                UUID REFERENCES service_points (service_point_id),    -- WHERE it was ordered from
    customer_id                       UUID REFERENCES customers (customer_id),    -- WHO ordered (nullable if anonymous)
    taken_by_user_id                    UUID REFERENCES users (user_id),    -- staff who logged it, if manual
    order_source                          VARCHAR(20) CHECK (order_source IN
                                           ('dine_in', 'voice_assistant', 'staff_ui', 'mobile_app', 'phone', 'walk_in')),
    order_status                            VARCHAR(20) DEFAULT 'placed' CHECK (order_status IN
                                             ('placed', 'confirmed', 'preparing', 'ready', 'out_for_delivery',
                                              'delivered', 'cancelled', 'refunded')),
    subtotal_amount                           DECIMAL(10, 2) DEFAULT 0,
    tax_amount                                  DECIMAL(10, 2) DEFAULT 0,
    total_amount                                  DECIMAL(10, 2) DEFAULT 0,
    special_instructions                            TEXT,
    placed_at                                         TIMESTAMPTZ DEFAULT now(),    -- WHEN it was placed
    confirmed_at                                        TIMESTAMPTZ,
    ready_at                                              TIMESTAMPTZ,
    delivered_at                                            TIMESTAMPTZ,
    cancelled_at                                              TIMESTAMPTZ,
    delivery_task_id                                            UUID REFERENCES tasks (task_id),    -- the robot job that carries it
    delivery_robot_id                                             UUID REFERENCES robots (robot_id)   -- which robot delivered it
);

CREATE INDEX idx_orders_location_time ON orders (location_id, placed_at DESC);
CREATE INDEX idx_orders_service_point ON orders (service_point_id);
CREATE INDEX idx_orders_customer ON orders (customer_id);
CREATE INDEX idx_orders_visit ON orders (visit_id);

-- 13.1 Individual line items within an order
CREATE TABLE order_items (
    order_item_id     UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    order_id             UUID NOT NULL REFERENCES orders (order_id) ON DELETE CASCADE,
    menu_item_id            UUID NOT NULL REFERENCES menu_items (menu_item_id),
    quantity                  INTEGER NOT NULL DEFAULT 1,
    unit_price                  DECIMAL(10, 2) NOT NULL,    -- price at time of order (menu price may change later)
    line_total                    DECIMAL(10, 2) NOT NULL,
    special_request                  VARCHAR(200),    -- e.g. "no onions", "extra spicy"
    item_status                        VARCHAR(20) DEFAULT 'pending' CHECK (item_status IN
                                        ('pending', 'preparing', 'ready', 'delivered', 'cancelled'))
);

-- 13.2 Full timestamped history of order status changes (for analytics: how
-- long each stage took — placed -> confirmed -> preparing -> ready -> delivered)
CREATE TABLE order_status_history (
    history_id            BIGSERIAL PRIMARY KEY,
    order_id                UUID NOT NULL REFERENCES orders (order_id) ON DELETE CASCADE,
    status                     VARCHAR(20) NOT NULL,
    changed_by_user_id           UUID REFERENCES users (user_id),    -- NULL if changed automatically by system/robot
    changed_at                     TIMESTAMPTZ DEFAULT now(),
    note                              TEXT
);

-- -----------------------------------------------------------------------------
-- SECTION 14: PAYMENTS
-- -----------------------------------------------------------------------------

CREATE TABLE payments (
    payment_id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    order_id               UUID NOT NULL REFERENCES orders (order_id) ON DELETE CASCADE,
    amount                    DECIMAL(10, 2) NOT NULL,
    payment_method               VARCHAR(30) CHECK (payment_method IN
                                  ('cash', 'card', 'upi', 'wallet', 'room_charge', 'pay_at_counter')),
    payment_status                 VARCHAR(20) DEFAULT 'pending' CHECK (payment_status IN
                                    ('pending', 'completed', 'failed', 'refunded')),
    transaction_ref                   VARCHAR(150),
    paid_at                             TIMESTAMPTZ
);

-- -----------------------------------------------------------------------------
-- SECTION 15: FEEDBACK (ties order quality back to robot delivery)
-- -----------------------------------------------------------------------------

CREATE TABLE order_feedback (
    feedback_id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    order_id               UUID NOT NULL REFERENCES orders (order_id) ON DELETE CASCADE,
    rating                    INTEGER CHECK (rating BETWEEN 1 AND 5),
    delivery_rating             INTEGER CHECK (delivery_rating BETWEEN 1 AND 5),   -- specifically about robot delivery
    comment                       TEXT,
    submitted_at                    TIMESTAMPTZ DEFAULT now()
);

COMMIT;
