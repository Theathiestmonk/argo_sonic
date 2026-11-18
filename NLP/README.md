# NLP – "Hi Sonic" voice agent (LangGraph)

Wake word → greet → listen → **one** LLM call for intent + all entities → navigation / ordering / movement / menu / general → back to listening.
Sarvam for speech (streaming STT + streaming TTS), OpenAI small model for understanding and answers.

```
python -m NLP                    # real: mic + speaker + ROS     (run from argo_sonic/, ROS sourced, Nav2 + safety shield up)
python -m NLP --sim              # real voice, simulated robot (NLP_MOCK_BLOCKED=forward,left to test "blocked")
python -m NLP --text-mode --sim  # type instead of speak; no mic/Sarvam/ROS (needs only OPENAI_API_KEY)

python -m pytest -p no:anyio NLP/tests          # 24 offline tests (scripted LLM/voice, real graph + knowledge)
python -m NLP.tests.live_voice_check            # Sarvam TTS→STT loopback + wake model + mic open (network)
python -m NLP.tests.ros_sim_check               # real RosRobot vs in-process ROS simulator (ROS sourced)
```
Keys come from `NLP/.env` or the existing `sonic/.env` (`OPENAI_API_KEY`, `SARVAM_API_KEY`). All knobs: `config.py` / `.env.example`.

## Graph (`graph.py`)
```
START → wake → greet → listen ─┬ silence → silence_prompt → listen   (2nd silence → sleep → wake)
                       ▲       └ understand → route → navigation | ordering | movement | menu | general | unclear | farewell(→wake)
                       └───────────────────────────── every action node returns to listen
menu --"yes, order it"--> ordering        (dish pre-filled; asks only what's missing)
```
`state.awaiting` records what the robot just asked ("order_qty", "nav_confirm", …) so a bare "two", "medium" or "yes" is routed to the right node and slot.

| Node | Behaviour |
|---|---|
| **navigation** | Destination must be a saved waypoint (`src/argo_mini/waypoints/<map>.json`, re-read on change). Given → "Going to X" and drives; missing → "Where do you want me to go?"; unknown → "I'm sorry, I don't know how to go to X. Do you want me to go to Y instead?" (closest match; digits must match exactly, so "table 9" never becomes "Table 5"). Aliases (base/dock → Docker) in `restaurant.json`. On arrival it stops and returns to listening; arriving at "Table N" makes N the sticky table for ordering. |
| **ordering** | Table (kept from navigation, else asked) → item → quantity → variation → "Added …, anything else?" → read-back → send. Anything already said is never re-asked. Free service items (water, towel, ketchup, cutlery…) are orderable too. Unavailable/unknown dishes are called out. Remove/cancel supported. |
| **movement** | forward/back = 1 m; left/right = turn 90° then 1 m (diff-drive, no strafing); turn left/right = 90° in place; spin = 360°. Blocked → "Sorry, I am blocked by something in that direction and cannot move." |
| **menu** | category → item list → description/price/offer → "Would you like to order it?" yes → ordering; no → "another item?". Specials of the day, famous dishes, veg/non-veg, and free-form questions ("something spicy with chicken") via a small lexical RAG over the menu + one streamed LLM answer. |
| **general** | name / weather (Open-Meteo, no key) / joke / restaurant facts (`data/restaurant.json`), then steers back: "Now, are you ready to order?" (a "yes" goes straight to ordering). |

## Latency design
- **Streaming speech**: LLM tokens → sentence chunker (first chunk may break at a comma) → Sarvam TTS WebSocket (raw PCM16, persistent + keep-alive) → player callback. Measured first audio **~200 ms** after text. Fixed phrases (greetings, prompts…) are pre-synthesised at boot and disk-cached (`.cache/tts`) → ~0 ms.
- **Streaming STT**: audio goes to Sarvam *while the user speaks*; local webrtcvad endpointing sends `flush` at end-of-speech; transcript ≈ 100 ms later. The STT socket is opened while the robot is still talking.
- **Speech never blocks work**: nodes only *queue* speech; "Going to Table 2" plays while Nav2 is already driving. `listen` is the single place that waits for the speaker to finish (half-duplex, no self-hearing).
- **LLM**: one compact-JSON call (~20 output tokens, reasoning off, prompt prefix static → cached). Model `gpt-5.4-nano` was the fastest measured here (`gpt-4.1-nano` is deprecated on this account); automatic fallback chain if a model disappears. This link has ≈640 ms bare TTFB to OpenAI, so ~1 s/call is the floor – hence:
- **Zero-LLM fast paths**: movement commands, yes/no, "that's all", bye, and replies to the robot's own slot questions (quantity, table, variation, waypoint, category, dish) resolve locally in <5 ms. Ordering/menu/navigation nodes themselves are deterministic (no LLM).
- Persistent in-process rclpy node (no 1–2 s `nav_bridge.py` subprocess per trip); menu refreshes in a background thread.

## Safety (movement)
All velocity goes to `/cmd_vel_smoothed` → **safety shield** → `/cmd_vel`; never bypassed. Before moving we check clearance from lidar (`/scan_corrected`), depth (`/safety_shield/depth_distance`) and ultrasonics in that direction (need `move + 0.5 m`); mid-move we stop if clearance < 0.45 m or progress stalls 1.5 s (shield zeroing us). No fresh sensor data ⇒ refuse ("my safety sensors are not responding") rather than fail-open.

## Assumptions to confirm on the robot
1. Lidar angle 0 = robot front, CCW positive (else set `NLP_LIDAR_YAW_OFFSET`). 2. `/us/*` are `sensor_msgs/Range`; `/safety_shield/depth_distance` is `Float32` metres. 3. Mic supports 16 kHz mono (USB PnP device 0 on this Jetson; set `NLP_MIC_DEVICE`). 4. Orders: confirmed orders go to the dashboard's POS order ledger (`backend/pos_api.py` → `POST /orders`, source `voice_assistant`, appear on the Orders page as #1001…), then Postgres (`restaurant_ops_schema.sql`; untested – the DB login in `sonic/.env` is rejected), then the legacy `src/argo_mini/orders/<map>.json`, so an order is never lost. 5. Menu: live `GET :8888/menu` first (currently empty here), else `src/argo_mini/menu/menu.json`; spice/size options live in `data/restaurant.json → variations`.
