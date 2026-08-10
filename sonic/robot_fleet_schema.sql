-- =============================================================================
-- robot_fleet_schema.sql
--
-- Multi-tenant robot fleet platform: clients/owners/locations, robot models,
-- hardware components, firmware, telemetry, navigation/mapping, tasks, the
-- generic voice-command log, maintenance, and audit trail.
--
-- Load this FIRST. restaurant_ops_schema.sql and db_schema.sql both
-- reference tables defined here (locations, robots, tasks, map_waypoints,
-- users, voice_commands).
--
-- Apply with:  psql -U <user> -d <database> -f robot_fleet_schema.sql
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

BEGIN;

-- -----------------------------------------------------------------------------
-- SECTION 1: CLIENTS / OWNERS / LOCATIONS
-- -----------------------------------------------------------------------------

-- 1.1 Clients = the business/customer who purchased robot(s) from you
CREATE TABLE clients (
    client_id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    company_name       VARCHAR(150) NOT NULL,
    business_type      VARCHAR(50) CHECK (business_type IN
                        ('restaurant', 'hotel', 'bar', 'home', 'cafe', 'hospital', 'office', 'other')),
    contact_name       VARCHAR(100),
    contact_email      VARCHAR(150) UNIQUE,
    contact_phone      VARCHAR(20),
    billing_address    TEXT,
    subscription_plan  VARCHAR(50),    -- e.g. basic / pro / enterprise
    account_status     VARCHAR(20) DEFAULT 'active' CHECK (account_status IN
                        ('active', 'suspended', 'trial', 'cancelled')),
    created_at         TIMESTAMPTZ DEFAULT now(),
    updated_at         TIMESTAMPTZ DEFAULT now()
);

-- 1.2 Locations = physical site where robots are deployed (a client can have many)
CREATE TABLE locations (
    location_id        UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    client_id          UUID NOT NULL REFERENCES clients (client_id) ON DELETE CASCADE,
    location_name      VARCHAR(150) NOT NULL,     -- e.g. "Downtown Branch"
    location_type      VARCHAR(50),                -- restaurant / hotel-lobby / bar / home / etc.
    address_line       TEXT,
    city                VARCHAR(100),
    state               VARCHAR(100),
    country             VARCHAR(100),
    postal_code         VARCHAR(20),
    latitude            DECIMAL(9, 6),
    longitude           DECIMAL(9, 6),
    timezone            VARCHAR(50),
    floor_plan_map_id   UUID,    -- FK to navigation_maps, added after that table exists
    wifi_ssid           VARCHAR(100),   -- site network robot connects to
    -- Staff-facing kill-switch for voice-triggered navigation (sonic_agent.py's
    -- navigation_handler checks this before dispatching a "go to X" action).
    -- Real Nav2 wiring is deferred, but the gate needs to exist now so it's
    -- already in place once dispatch_robot_action() sends real goals.
    voice_nav_enabled   BOOLEAN DEFAULT true,
    created_at          TIMESTAMPTZ DEFAULT now()
);

-- 1.3 Users = people who log into the owner/staff UI to control robots
CREATE TABLE users (
    user_id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    client_id        UUID NOT NULL REFERENCES clients (client_id) ON DELETE CASCADE,
    full_name        VARCHAR(150),
    email            VARCHAR(150) UNIQUE NOT NULL,
    phone            VARCHAR(20),
    password_hash    TEXT NOT NULL,
    role             VARCHAR(30) DEFAULT 'staff' CHECK (role IN
                      ('owner', 'manager', 'staff', 'support_admin')),
    is_active        BOOLEAN DEFAULT true,
    last_login_at    TIMESTAMPTZ,
    created_at       TIMESTAMPTZ DEFAULT now()
);

-- 1.4 Which users can access which robots/locations (many-to-many, since a
-- manager might oversee multiple branches, or a robot might be shared)
CREATE TABLE user_location_access (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id             UUID NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    location_id         UUID NOT NULL REFERENCES locations (location_id) ON DELETE CASCADE,
    permission_level    VARCHAR(30) DEFAULT 'control' CHECK (permission_level IN
                         ('view_only', 'control', 'admin')),
    UNIQUE (user_id, location_id)
);

-- -----------------------------------------------------------------------------
-- SECTION 2: ROBOT MODELS, UNITS, AND HARDWARE COMPONENTS
-- -----------------------------------------------------------------------------

-- 2.1 Robot model/product line (you may release v1, v2, "Mini", "Pro" etc.)
CREATE TABLE robot_models (
    model_id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    model_name              VARCHAR(100) NOT NULL,     -- e.g. "CarrierBot X1"
    model_version           VARCHAR(30),                 -- e.g. "v1.2"
    description             TEXT,
    max_payload_kg          DECIMAL(6, 2),
    battery_capacity_wh     DECIMAL(8, 2),
    default_firmware_id     UUID,     -- FK to firmware_versions, set after that table exists
    released_at             DATE,
    is_active                BOOLEAN DEFAULT true
);

-- 2.2 Master catalog of component TYPES you use across all robots
-- (this is the "part catalog", not physical serialized units)
CREATE TABLE component_catalog (
    component_type_id    UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    category              VARCHAR(50) NOT NULL CHECK (category IN (
                           'compute_module', 'lidar', 'depth_camera', 'microcontroller',
                           'motor_driver', 'wheel_tire', 'battery', 'motor', 'power_board',
                           'speaker', 'microphone_array', 'display_screen', 'chassis',
                           'imu_sensor', 'ultrasonic_sensor', 'charging_dock', 'other'
                           )),
    part_name             VARCHAR(150) NOT NULL,    -- e.g. "Jetson Nano 4GB", "RPLIDAR A1", "ESP32-WROOM-32"
    manufacturer          VARCHAR(100),
    part_number           VARCHAR(100),
    unit_cost             DECIMAL(10, 2),
    datasheet_url         TEXT,
    specs_json            JSONB,    -- flexible specs: {"ram":"4GB","interfaces":["USB3","CSI"]}
    created_at            TIMESTAMPTZ DEFAULT now()
);

-- 2.3 Physical, serialized instances of a component (every actual chip/board/tire you own)
CREATE TABLE component_instances (
    component_instance_id    UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    component_type_id        UUID NOT NULL REFERENCES component_catalog (component_type_id),
    serial_number             VARCHAR(150) UNIQUE,
    manufacture_date          DATE,
    warranty_expiry           DATE,
    purchase_order_ref        VARCHAR(100),
    current_status            VARCHAR(30) DEFAULT 'in_stock' CHECK (current_status IN
                               ('in_stock', 'installed', 'faulty', 'retired', 'under_repair')),
    installed_in_robot_id     UUID,    -- FK to robots, nullable, set below
    installed_at              TIMESTAMPTZ,
    removed_at                TIMESTAMPTZ,
    created_at                TIMESTAMPTZ DEFAULT now()
);

-- 2.4 Robots = the actual deployed unit (this is the core "which robot is where" table)
CREATE TABLE robots (
    robot_id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    robot_uid               VARCHAR(50) UNIQUE NOT NULL,    -- human-friendly unique ID, e.g. "CB-X1-000842"
    model_id                 UUID NOT NULL REFERENCES robot_models (model_id),
    client_id                 UUID REFERENCES clients (client_id),
    location_id                UUID REFERENCES locations (location_id),
    serial_number              VARCHAR(150) UNIQUE NOT NULL,   -- chassis-level serial, separate from robot_uid
    manufactured_at             DATE,
    deployed_at                  TIMESTAMPTZ,
    firmware_version_id          UUID,    -- FK to firmware_versions
    current_status                VARCHAR(30) DEFAULT 'active' CHECK (current_status IN
                                   ('active', 'idle', 'charging', 'offline', 'maintenance', 'error', 'retired')),
    ip_address                     VARCHAR(45),
    mac_address                    VARCHAR(20),
    last_seen_at                   TIMESTAMPTZ,
    warranty_expiry                DATE,
    notes                           TEXT,
    created_at                      TIMESTAMPTZ DEFAULT now(),
    updated_at                      TIMESTAMPTZ DEFAULT now()
);

-- Now that robots exists, wire the earlier forward reference
ALTER TABLE component_instances
    ADD CONSTRAINT fk_component_installed_robot
    FOREIGN KEY (installed_in_robot_id) REFERENCES robots (robot_id);

-- 2.5 Bill-of-materials join: which component instances are CURRENTLY in which robot
-- (kept separate from component_instances.installed_in_robot_id so you retain
-- full swap history over time)
CREATE TABLE robot_component_history (
    id                        UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    robot_id                  UUID NOT NULL REFERENCES robots (robot_id) ON DELETE CASCADE,
    component_instance_id     UUID NOT NULL REFERENCES component_instances (component_instance_id),
    installed_at              TIMESTAMPTZ DEFAULT now(),
    removed_at                TIMESTAMPTZ,    -- NULL = currently installed
    reason_removed             VARCHAR(100)    -- e.g. "faulty", "upgrade", "scheduled swap"
);

-- -----------------------------------------------------------------------------
-- SECTION 3: FIRMWARE / SOFTWARE
-- -----------------------------------------------------------------------------

CREATE TABLE firmware_versions (
    firmware_version_id    UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    model_id                 UUID REFERENCES robot_models (model_id),
    version_label             VARCHAR(30) NOT NULL,    -- e.g. "2.4.1"
    release_notes             TEXT,
    build_artifact_url        TEXT,
    is_stable                 BOOLEAN DEFAULT true,
    released_at                TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE robots
    ADD CONSTRAINT fk_robot_firmware
    FOREIGN KEY (firmware_version_id) REFERENCES firmware_versions (firmware_version_id);

ALTER TABLE robot_models
    ADD CONSTRAINT fk_model_default_firmware
    FOREIGN KEY (default_firmware_id) REFERENCES firmware_versions (firmware_version_id);

-- Log of OTA update attempts per robot
CREATE TABLE software_update_log (
    update_id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    robot_id             UUID NOT NULL REFERENCES robots (robot_id) ON DELETE CASCADE,
    from_firmware_id     UUID REFERENCES firmware_versions (firmware_version_id),
    to_firmware_id       UUID REFERENCES firmware_versions (firmware_version_id),
    status                VARCHAR(20) CHECK (status IN ('started', 'success', 'failed', 'rolled_back')),
    started_at            TIMESTAMPTZ DEFAULT now(),
    finished_at           TIMESTAMPTZ,
    error_message         TEXT
);

-- -----------------------------------------------------------------------------
-- SECTION 4: TELEMETRY / ROBOT STATE (time-series data)
-- -----------------------------------------------------------------------------

-- 4.1 Periodic heartbeat/telemetry snapshot (e.g. every 5-30 sec per robot)
CREATE TABLE robot_telemetry (
    telemetry_id          BIGSERIAL PRIMARY KEY,
    robot_id               UUID NOT NULL REFERENCES robots (robot_id) ON DELETE CASCADE,
    recorded_at             TIMESTAMPTZ DEFAULT now(),
    battery_percent         DECIMAL(5, 2),
    battery_voltage         DECIMAL(6, 2),
    is_charging             BOOLEAN DEFAULT false,
    cpu_temp_c               DECIMAL(5, 2),
    cpu_usage_percent         DECIMAL(5, 2),
    ram_usage_percent         DECIMAL(5, 2),
    wifi_signal_dbm            INTEGER,
    pos_x                       DECIMAL(10, 3),    -- position in site map coordinate frame
    pos_y                       DECIMAL(10, 3),
    heading_deg                 DECIMAL(6, 2),
    linear_speed_mps            DECIMAL(6, 3),
    motor_status_json           JSONB,    -- per-motor-driver status, e.g. {"m1":"ok","m2":"ok"}
    lidar_status                 VARCHAR(20),
    depth_camera_status          VARCHAR(20),
    error_flags                  JSONB    -- array of active error codes at this moment
);

CREATE INDEX idx_telemetry_robot_time ON robot_telemetry (robot_id, recorded_at DESC);

-- 4.2 Alerts/errors raised by robots (subset of telemetry that needs attention)
CREATE TABLE robot_alerts (
    alert_id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    robot_id                 UUID NOT NULL REFERENCES robots (robot_id) ON DELETE CASCADE,
    alert_type                VARCHAR(50),    -- e.g. "low_battery","lidar_fail","stuck","collision_risk"
    severity                   VARCHAR(20) CHECK (severity IN ('info', 'warning', 'critical')),
    message                    TEXT,
    is_resolved                BOOLEAN DEFAULT false,
    raised_at                  TIMESTAMPTZ DEFAULT now(),
    resolved_at                 TIMESTAMPTZ,
    resolved_by_user_id          UUID REFERENCES users (user_id)
);

-- 4.3 Charging session log
CREATE TABLE charging_sessions (
    session_id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    robot_id              UUID NOT NULL REFERENCES robots (robot_id) ON DELETE CASCADE,
    dock_component_id      UUID REFERENCES component_instances (component_instance_id),
    started_at              TIMESTAMPTZ DEFAULT now(),
    ended_at                 TIMESTAMPTZ,
    start_battery_pct         DECIMAL(5, 2),
    end_battery_pct           DECIMAL(5, 2)
);

-- -----------------------------------------------------------------------------
-- SECTION 5: NAVIGATION / MAPPING
-- -----------------------------------------------------------------------------

-- 5.1 Site maps (SLAM-generated floor maps per location)
CREATE TABLE navigation_maps (
    map_id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    location_id               UUID NOT NULL REFERENCES locations (location_id) ON DELETE CASCADE,
    map_name                   VARCHAR(100),
    map_file_url                 TEXT,    -- pointer to stored occupancy grid / point cloud file
    resolution_m_per_px            DECIMAL(6, 4),
    origin_x                        DECIMAL(10, 3),
    origin_y                        DECIMAL(10, 3),
    version                          INTEGER DEFAULT 1,
    created_at                       TIMESTAMPTZ DEFAULT now(),
    is_active                         BOOLEAN DEFAULT true
);

ALTER TABLE locations
    ADD CONSTRAINT fk_location_map
    FOREIGN KEY (floor_plan_map_id) REFERENCES navigation_maps (map_id);

-- 5.2 Named waypoints on a map (e.g. "Kitchen", "Table 4", "Charging Dock", "Room 204")
CREATE TABLE map_waypoints (
    waypoint_id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    map_id                   UUID NOT NULL REFERENCES navigation_maps (map_id) ON DELETE CASCADE,
    label                      VARCHAR(100) NOT NULL,
    waypoint_type               VARCHAR(30) CHECK (waypoint_type IN
                                 ('table', 'kitchen', 'room', 'charging_dock', 'entrance', 'custom')),
    pos_x                        DECIMAL(10, 3),
    pos_y                         DECIMAL(10, 3),
    heading_deg                   DECIMAL(6, 2)
);

-- 5.3 Path/route history for auditing and analytics (optional but useful)
CREATE TABLE navigation_logs (
    nav_log_id             BIGSERIAL PRIMARY KEY,
    robot_id                 UUID NOT NULL REFERENCES robots (robot_id) ON DELETE CASCADE,
    task_id                    UUID,    -- FK to tasks, set after tasks table exists
    started_at                  TIMESTAMPTZ DEFAULT now(),
    ended_at                     TIMESTAMPTZ,
    from_waypoint_id              UUID REFERENCES map_waypoints (waypoint_id),
    to_waypoint_id                 UUID REFERENCES map_waypoints (waypoint_id),
    distance_m                       DECIMAL(8, 2),
    obstacles_avoided                 INTEGER DEFAULT 0,
    outcome                            VARCHAR(20) CHECK (outcome IN
                                        ('success', 'aborted', 'collision', 'stuck', 'rerouted'))
);

-- -----------------------------------------------------------------------------
-- SECTION 6: TASKS (the actual "carry this item" jobs)
-- -----------------------------------------------------------------------------

CREATE TABLE tasks (
    task_id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    robot_id                   UUID NOT NULL REFERENCES robots (robot_id) ON DELETE CASCADE,
    location_id                  UUID NOT NULL REFERENCES locations (location_id),
    created_by_user_id             UUID REFERENCES users (user_id),    -- staff who placed it via UI, nullable if voice-triggered
    task_type                        VARCHAR(30) DEFAULT 'delivery' CHECK (task_type IN
                                      ('delivery', 'return_to_dock', 'patrol', 'custom', 'pickup')),
    origin_waypoint_id                 UUID REFERENCES map_waypoints (waypoint_id),
    destination_waypoint_id              UUID REFERENCES map_waypoints (waypoint_id),
    payload_description                    VARCHAR(200),    -- e.g. "Order #58 - 2x Pasta, 1x Coke"
    priority                                 INTEGER DEFAULT 5,
    status                                     VARCHAR(20) DEFAULT 'queued' CHECK (status IN
                                                ('queued', 'in_progress', 'completed', 'failed', 'cancelled')),
    triggered_by                                 VARCHAR(20) CHECK (triggered_by IN ('voice', 'ui', 'api', 'schedule')),
    created_at                                     TIMESTAMPTZ DEFAULT now(),
    started_at                                       TIMESTAMPTZ,
    completed_at                                       TIMESTAMPTZ,
    failure_reason                                       TEXT
);

ALTER TABLE navigation_logs
    ADD CONSTRAINT fk_navlog_task
    FOREIGN KEY (task_id) REFERENCES tasks (task_id);

-- -----------------------------------------------------------------------------
-- SECTION 7: VOICE ASSISTANT (generic, fleet-wide log)
-- -----------------------------------------------------------------------------

-- 7.1 Raw voice command log (every utterance captured and processed)
CREATE TABLE voice_commands (
    voice_command_id      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    robot_id                UUID NOT NULL REFERENCES robots (robot_id) ON DELETE CASCADE,
    raw_transcript            TEXT,
    detected_language          VARCHAR(20),
    intent                       VARCHAR(50),    -- e.g. "go_to_table","stop","call_staff","status_check"
    intent_confidence              DECIMAL(4, 3),
    resulting_task_id                UUID REFERENCES tasks (task_id),
    wake_word_used                     VARCHAR(50),
    audio_clip_url                       TEXT,    -- optional stored audio for QA/training
    recorded_at                            TIMESTAMPTZ DEFAULT now()
);

-- 7.2 Supported intents catalog (keeps NLU intents centrally managed/versioned)
CREATE TABLE voice_intents (
    intent_id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    intent_name             VARCHAR(50) UNIQUE NOT NULL,
    description               TEXT,
    example_phrases            JSONB,    -- array of sample training phrases
    maps_to_task_type            VARCHAR(30)
);

-- -----------------------------------------------------------------------------
-- SECTION 8: MAINTENANCE / SERVICE
-- -----------------------------------------------------------------------------

CREATE TABLE maintenance_records (
    maintenance_id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    robot_id                          UUID NOT NULL REFERENCES robots (robot_id) ON DELETE CASCADE,
    performed_by                        VARCHAR(150),    -- technician name / team
    maintenance_type                      VARCHAR(30) CHECK (maintenance_type IN
                                           ('scheduled', 'repair', 'component_swap', 'inspection', 'recall')),
    description                             TEXT,
    related_component_instance_id             UUID REFERENCES component_instances (component_instance_id),
    cost                                        DECIMAL(10, 2),
    performed_at                                  TIMESTAMPTZ DEFAULT now(),
    next_due_at                                     TIMESTAMPTZ
);

-- -----------------------------------------------------------------------------
-- SECTION 9: AUDIT LOG (who did what through the owner UI)
-- -----------------------------------------------------------------------------

CREATE TABLE audit_log (
    audit_id         BIGSERIAL PRIMARY KEY,
    user_id            UUID REFERENCES users (user_id),
    robot_id             UUID REFERENCES robots (robot_id),
    action                 VARCHAR(100),    -- e.g. "manual_override","emergency_stop","firmware_push"
    details_json             JSONB,
    performed_at                TIMESTAMPTZ DEFAULT now()
);

COMMIT;
