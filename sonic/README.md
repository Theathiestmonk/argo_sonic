# Sonic — restaurant voice agent (LangGraph + Postgres)

Real-time voice NLP agent for the cafe robot. Wake word ("Hi Sonic") → greet
→ listen → OpenAI LLM NLU (intent classification + slot extraction,
with mid-conversation intent switching) → dialogue handler (menu / call_people /
take_order / navigation / cutlery / about / normal_conv / get_bill) → Sarvam
TTS → back to idle.

Runtime is a real LangGraph `StateGraph` (see `build_graph()` in
`sonic_agent.py`) — each step of the flowchart in `graph.py` is an actual
graph node. Multi-turn "ask the guest something and wait" uses LangGraph's
`interrupt()`/`Command(resume=...)` mechanism plus a `MemorySaver`
checkpointer.

Menu, orders, dialogue sessions, cutlery/staff-call requests, and bills all
persist to Postgres (see `robot_fleet_schema.sql`, `restaurant_ops_schema.sql`,
`db_schema.sql`) — this replaced the old file-based menu.json/orders.json
system entirely; `backend/launcher.py`'s `/menu` and `/orders/*` endpoints
now read/write the same database.

---

## Setup

```bash
cd ..    # repo root
python3 -m venv venv && venv/bin/pip install -r requirements.txt
```

Create `sonic/.env` (gitignored — copy `.env.example`):
```
OPENAI_API_KEY=sk-...     # platform.openai.com
SARVAM_API_KEY=sk_...     # dashboard.sarvam.ai — not needed for --text-mode
DATABASE_URL=postgresql://user:password@host:5432/dbname
ROBOT_UID=SONIC-001
```

`main_agent.py`'s LLM defaults to OpenAI's gpt-4o-mini (see `LLM_URL`/
`LLM_MODEL` in `.env.example` to point elsewhere). A local Ollama model on
the Jetson was tried and rejected — the robot's own GPU-based path planner
(`ntfields_planner_node.py`) needs that memory more than chat latency does.

`backend/launcher.py` loads this same `.env` file, so `DATABASE_URL`/
`ROBOT_UID` only need to be set once here.

### Database

```bash
python run_migrations.py   # applies robot_fleet -> restaurant_ops -> db_schema
                            # -> auth_rls_schema, in order
python seed_db.py          # seeds a demo client/location/robot/5 tables, and
                            # the live menu from src/argo_mini/menu/menu.json
                            # (the same file staff edit via menu.html)
```

`run_migrations.py`'s first three files only target a brand-new empty
database (drop it first to start over); `auth_rls_schema.sql` (the 4th) and
`seed_db.py` are both idempotent and safe to re-run any time.

`Hi_Sonic.onnx` (the trained wake-word model) is already in this directory.

### Remote dashboard access (multi-tenant — dashboard/)

`sonic_agent.py`/`backend/launcher.py` always connect via `DATABASE_URL`
directly (trusted, bypasses RLS — see `auth_rls_schema.sql`'s header) and
need no auth setup. The separate `dashboard/` app is what real customers log
into remotely, and that needs a Supabase Auth user linked to a client:

```bash
# 1. Create the user in Supabase: dashboard -> Authentication -> Users -> Add user
# 2. Link them to a client (or grant fleet-wide access):
python grant_dashboard_access.py --email owner@cafe.com --client "Sonic Demo Restaurant" --role owner
python grant_dashboard_access.py --email me@yourcompany.com --platform-admin
```

See `dashboard/README.md` for running that app.

## Run

```bash
python sonic_agent.py               # real mic/speaker/wake-word loop
python sonic_agent.py --text-mode   # typed input / printed output — no mic,
                                     # no wake word, no Sarvam calls; exercises
                                     # the dialogue logic directly
```

Say **"Hi Sonic"**, wait for it to greet you, then talk naturally.

### Staff-dispatched sessions (backend/launcher.py)

When a staff member clicks a table's action button in the dashboard,
`backend/launcher.py`'s `POST /voice/start` spawns this script with
`TABLE_NO`/`SONIC_ACTION_HINT`/`SONIC_MAP_NAME` environment variables set —
the agent skips conversational table discovery and runs exactly one round
trip before exiting, instead of sitting in the wake-word loop:

- **order / bill / room_service**: the Kitchen→Table trip and the
  Table→Kitchen return are graph nodes (`n_navigate_to_table`,
  `n_return_to_kitchen` — see "Graph-owned navigation" below), run around
  the normal conversational session.
- **deliver**: at the Kitchen, ask staff to load the order and wait up to
  30s for a spoken confirmation (`run_delivery_session()`) — only then
  navigate to the table, announce the delivery, and return. No confirmation
  within the window aborts the trip rather than delivering an empty robot.
  This one isn't part of the intent_classify graph (it's not a
  conversation), so it still manages its own travel directly.

Running the script directly (no `TABLE_NO` set) gives the general always-on
wake-word loop, unaffected by any of this.

## Real navigation (`navigate_and_wait()` / `sonic/nav_bridge.py`)

`sonic_agent.py` itself stays a plain, non-ROS process — `nav_bridge.py` is
the one piece that talks to Nav2 directly (`/navigate_to_pose`), invoked as
a subprocess with the ROS environment sourced, matching how
`backend/launcher.py`'s `/estop` handler shells out to `ros2`. It blocks
until Nav2 reports a real result (arrived/failed/timeout) — nothing in this
agent claims to have arrived somewhere on a timer.

Waypoints come from `src/argo_mini/waypoints/<SONIC_MAP_NAME>.json`
(default `office_map`), matched by name (`"Kitchen"`, `"Table 3"`,
`"Docker"`) — the same names `seed_db.py`'s seeded tables and
`service_points.label` already use, so no separate id-mapping is needed.

Test the bridge in isolation before trusting it inside a session:
```bash
source /opt/ros/humble/setup.bash && source ../install/setup.bash
python3 nav_bridge.py --map office_map --destination Kitchen --timeout 60
```

## Graph-owned navigation

`state.location` is the single value that tracks where the robot currently
is or is headed — a table number (e.g. `"4"`) while serving a table, or the
literal string `"Kitchen"` once it's home. It's shared by **every** trigger
path: a staff dashboard click, a staff/guest voice command, all funnel
through the same `ask_table` → navigate → return sequence, so there's no
separate "dispatched" vs. "voice" navigation logic to keep in sync.

- **`take_order` and `get_bill` both route through `n_ask_table` first**
  (`INTENT_TO_NODE` points both at `"ask_table"`, not straight at their real
  node). For a dashboard-dispatched session, `run_graph_session()` pre-seeds
  `state.active_intent` (via `DISPATCH_ACTION_TO_INTENT`, keyed off
  `SONIC_ACTION_HINT`) and `state.location`/`location_locked` before the
  graph runs — `n_ask_table` sees `location` is already a real table and
  bypasses instantly, routing on to `new_order_start`/`get_bill_node` via
  `ASK_TABLE_CONTINUATION`. `n_intent_classify`'s pre-seeded-intent fast path
  (`state.first_turn and FORCED_LOCATION and state.active_intent`) skips
  greeting/NLU entirely for these sessions but still passes through
  `ask_table` on the way, for consistency. For a **voice-triggered** request
  with no table named ("Hi Sonic, take order") — `location` isn't set yet
  — `n_ask_table` asks "Which table should I come to?", using the same
  NLU/timeout/intent-switch handling as every other `ask_*` node; the answer
  is captured by `interpret()`'s ambient `table_no` slot capture, same
  mechanism that already opportunistically grabs a table number from *any*
  utterance ("take order for table 4" gets it in the same breath, no
  separate question needed).
- **Navigation happens inside the intent's own node, not before it.**
  `_ensure_at_table(state)` is called at the top of `n_new_order_start` and
  `n_get_bill`: by this point `location` is guaranteed to be a real table
  (thanks to `ask_table`), so if the robot hasn't physically arrived yet
  (`state.table_reached` is False), it makes the real Nav2 trip there via
  `navigate_and_wait()` — for a dispatched **or** voice-triggered session
  alike, no special-casing. Speaks a plain greeting on arrival ("Hi there!
  I'm here to take your order." / "...with your bill.") — not phrased as a
  question, so it doesn't compete with the actual first question
  (`n_ask_item`'s "What item would you like?") for the guest's answer. If
  the trip fails, it puts the state into going-idle and returns False so the
  caller bails out immediately (`return asdict(state)`) — the graph never
  starts a conversation the robot isn't physically at the table for. Once
  arrived, `state.table_reached` is set True so a resumed/looping session
  doesn't re-navigate.
- **`n_return_to_kitchen`** — the graph's terminal step. Every path that
  ends the session (`n_respond`'s `went_idle` handling) routes here instead
  of straight to `END`. If this visit ever resolved a table
  (`state.location` is a real table), it makes the real Table→Kitchen trip
  exactly once regardless of *why* the session ended (order completed,
  guest said bye, or a genuine silence timeout) — and resets
  `state.location` back to `"Kitchen"`, clearing `location_locked` too, so a
  *later, different* visit can't inherit a stale table number and silently
  navigate to the wrong place. A no-op for sessions that never touched a
  table (menu/cutlery/normal_conv — nothing to return from).

## Tuning

In `sonic_agent.py`:
```python
NAV_TIMEOUT_S          # default 90 — max seconds to wait for a Nav2 result
                        # per leg before giving up
WAKE_WORD_THRESHOLD    # 0.0–1.0, default 0.5 — lower if "Hi Sonic" isn't
                        # detected reliably, raise if it triggers on noise
SILENCE_ONSET_TIMEOUT_S, TRAILING_SILENCE_MS, SPEECH_RMS_THRESHOLD
                        # mic/VAD tuning — recalibrate against your room
PICKUP_CONFIRM_WINDOW_S # default 30 — delivery's kitchen pickup-confirmation window
```

Set `SONIC_TRACE=0` to silence the per-node execution trace printed to the
terminal by default.

## Voice (Sarvam TTS)

`SARVAM_SPEAKER = "ishita"` (female, bulbul:v3) at `SARVAM_TTS_PACE = 0.9`
(slightly slower than normal, clearer over a speaker in a noisy room).
`shubh` is bulbul:v3's male default; `varun` exists but Sarvam's own docs
flag it as a "villain/suspense" voice — not a fit here.

`speak()` tries Sarvam's real streaming API first (`sarvam_tts_stream()` —
one WebSocket connection per utterance, audio arrives in chunks as it's
generated) and transparently falls back to the non-streaming REST call
(`sarvam_tts()`) if the `websockets` package isn't installed or anything
about the streaming connection fails — this fallback hasn't been exercised
against the live API from a dev sandbox, so treat early failures there as
expected until verified against real hardware. Needs `websockets>=12.0`
(`pip install -r requirements.txt`).

## Staff switch: navigation kill switch

`locations.voice_nav_enabled` (Postgres) gates **all** autonomous
navigation now — a guest's spoken "take me to the kitchen" request AND
every staff-dispatched round trip (`navigate_and_wait()`). One switch stops
all robot movement (e.g. staff spot a spill on the floor). Toggle it from
the dashboard (`TablesPanel.jsx`) or directly:
```bash
curl -X POST http://localhost:8888/voice/nav_enabled -d '{"enabled": false}'
```

## Porting to Jetson

`wake_word.py`-equivalent code (`load_wake_word_model()`/`wait_for_wake_word()`
in `sonic_agent.py`) uses cross-platform libraries (`sounddevice`,
`openwakeword`) that work on Jetson as-is. Install audio deps via apt first:
```bash
sudo apt install portaudio19-dev python3-pyaudio
pip3 install sounddevice openwakeword onnxruntime psycopg2-binary langgraph --break-system-packages
```
