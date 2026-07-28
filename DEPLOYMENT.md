# Argo Sonic — Deployment & Operations Guide

How to deploy the UI + backend to the robot (Jetson), what each piece does,
and where to look when something isn't working.

## 1. Architecture at a glance

```
Browser (React app, frontend/dist/)
   │
   ├── ws://<host>:9090  →  argo-rosbridge (rosbridge_websocket)  →  ROS2 graph
   │                         always-on systemd service
   │
   └── http://<host>:8888 →  argo-launcher (backend/launcher.py)
                              always-on systemd service, zero ROS deps —
                              starts/stops shell scripts as subprocesses,
                              serves maps/waypoints as plain files over HTTP
```

- **`argo-rosbridge`** — always on. Bridges the browser's WebSocket connection to the ROS2 graph (topics, services, actions). Independent of whichever robot "stack" is running.
- **`argo-launcher`** (`backend/launcher.py`) — always on, port 8888. A tiny stdlib-only HTTP server with no ROS imports; it just launches/kills one of three shell scripts as a subprocess and serves the `maps/`/`waypoints/` directories as JSON/files. See §5 for its full API.
- **`argo-ui`** — always on, port 3000. Just `python3 -m http.server 3000` serving the prebuilt `frontend/dist/`. Not a dev server — you must `npm run build` after every change.

## 2. First-time setup (already done, for reference)

```bash
cd ~/my_project/argo_sonic          # or wherever it's cloned
sudo bash sh/install-services.sh
```
This builds the frontend, installs `rosbridge_server` if missing, and creates/enables the three systemd services above.

**`set -e` at the top means the script stops dead at the first failed step** — no partial "well, 2 out of 3 services got installed" state, so seeing it stop early doesn't mean anything after the failure point is in effect yet. Re-run it after fixing whatever failed. Two errors you may hit on a Jetson specifically:

- **Step 1 (frontend build) fails with `Cannot find module '@rollup/rollup-linux-arm64-gnu'`** — `frontend/node_modules`/`package-lock.json` were most likely installed on a different-architecture machine (e.g. an x86 dev laptop) and then copied/pulled onto the Jetson. This is a known npm optional-dependencies bug (npm/cli#4828) where the wrong platform's native binary gets resolved. Fix by installing fresh **on the Jetson itself**:
  ```bash
  cd ~/my_project/argo_sonic/frontend
  rm -rf node_modules package-lock.json
  npm install
  npm run build   # confirm it succeeds standalone before re-running the installer
  ```

- **Step 2 (installing `rosbridge_server`) fails with `Could not get lock /var/lib/dpkg/lock-frontend`** — some other process is holding the apt/dpkg lock, almost always `packagekitd` (GNOME Software's background update-checker, which ships on Ubuntu desktop images and runs on its own schedule — nothing you did triggered it). Since the Jetson here is a robot, not a desktop anyone uses GNOME Software on, the clean permanent fix is to stop it from running at all:
  ```bash
  sudo systemctl stop packagekit
  sudo systemctl mask packagekit   # mask (not just stop) so it can't restart itself later
  ```
  (`sudo unmask packagekit` reverses it, if the GUI is ever needed again.) Never delete the lock file or kill the holding process forcibly — that can corrupt dpkg's state mid-transaction.

## 3. Deploying an update

```bash
cd ~/my_project/argo_sonic
git pull
cd frontend && npm run build && cd ..
chmod +x sh/*.sh                    # git clone doesn't always preserve +x
sudo systemctl restart argo-launcher argo-ui
# argo-rosbridge only needs a restart if sh/start-rosbridge.sh itself changed
```

Confirm all three are up:
```bash
sudo systemctl status argo-rosbridge argo-launcher argo-ui --no-pager
curl -s http://localhost:8888/status; echo
curl -s http://localhost:8888/maps; echo
```
Then open `http://<jetson-ip>:3000`.

## 4. The three stack modes

The UI never runs `ros2 launch`/`ros2 run` directly — everything goes through
`argo-launcher`'s `POST /start`, which runs one of these three **UI-dedicated**
shell scripts (kept separate from the hand-run originals in `sh/` so a
browser click can never disturb an SSH session using the same script name):

| Mode | Script | What it launches | When |
|---|---|---|---|
| `manual` | `sh/start_slam_ui.sh` | SLAM only (robot_state_publisher, serial_bridge, rplidar, scan_relay, slam_toolbox mapping) — **no Nav2** | Building a new map, driving manually |
| `auto` | `sh/start_slam_explore_ui.sh` | SLAM + full Nav2 + frontier_explorer | Building a new map, autonomous exploration |
| `navigate` | `sh/start_argo_nav_ui.sh --map <path>` | slam_toolbox **localization** + full Nav2 + depth safety shield (+ camera) on a **previously saved** map | Normal operation — sending the robot to tables/places |

`start_ntfields_nav_ui.sh` also exists (NTFields social shield/trainer/
navigator instead of the depth safety shield) but is **not currently wired
up** — it references executables (`ntfields_social_shield`, `ntfields_trainer`,
`ntfields_navigator`) that only exist in a separate, diverged workspace
(`~/argo_mini_ws`), not this repo's own `install/`. This repo's own NTFields
source (`ntfields_planner_node`/`ntfields_speed`/etc.) is a different,
non-interchangeable implementation from the same fork point. Which one is
actually meant to be current needs confirming before `NAV_SCRIPT` points at
it — see the script's own header comment for the full story. Both nav
scripts write to the same `/tmp/argo_nav_progress` file (see §5's
`GET /nav_progress`), so switching `NAV_SCRIPT` is a one-line change either way.

All three UI-launched scripts always run with `--no-rviz` when launched by
the UI (rviz2 has no `DISPLAY` on a headless robot and would otherwise crash
immediately, desyncing `/status` from reality — see the comment in
`launcher.py`).

**Important:** `navigate` mode's `/status` reporting `"running": true` only
means *the wrapper process launched* — `start_argo_nav_ui.sh` itself
takes **90+ seconds** (camera wait, costmap wait, several `lifecycle set
configure/activate` steps) before Nav2 can actually accept a goal. The
frontend (`DashboardHome.jsx`) separately confirms real readiness by polling
`/rosapi/topics` over rosbridge for `/navigate_to_pose/_action/status` before
unlocking "Confirm" — don't rely on `/status` alone to mean "ready for goals."

Switching maps while `navigate` mode is already running automatically stops
the old process group and starts fresh with the new map — the launcher will
never leave you silently navigating against the wrong map's coordinate frame.

## 5. `argo-launcher` API reference

All endpoints are CORS-open (`Access-Control-Allow-Origin: *`).

| Method | Path | Description |
|---|---|---|
| GET | `/status` | `{running, pid, mode, map}` — current subprocess state |
| GET | `/config` | `{maps_dir}` — absolute path to `src/argo_mini/maps` on this checkout |
| GET | `/maps` | `{maps: [str]}` — names from `*.yaml` in `maps/` |
| GET | `/maps/<name>/meta` | `{resolution, origin}` parsed from `<name>.yaml` |
| GET | `/maps/<name>/preview` | raw `<name>.pgm` bytes |
| GET | `/waypoints/<map>` | JSON content of `waypoints/<map>.json` (`{}` if none yet) |
| POST | `/waypoints/<map>` | body = full waypoints dict; overwrites the file |
| POST | `/start` | body `{"mode": "manual"\|"auto"\|"navigate", "map": str}` (map required for `navigate`) |
| POST | `/stop` | kills the current process group |

## 6. Checking logs

**`argo-launcher`** (start/stop, maps, waypoints, mode/map switches):
```bash
journalctl -u argo-launcher -f
```
Look for lines like `[launcher] stack started mode=navigate map=office_map pid=...`.

**`argo-rosbridge`**:
```bash
journalctl -u argo-rosbridge -f
```

**`argo-ui`** — rarely useful, it's just a static file server:
```bash
journalctl -u argo-ui -f
```

**⚠️ The actual ROS nodes (SLAM, Nav2, serial_bridge, camera, etc.) do NOT
appear in any of the above.** `launcher.py` deliberately discards their
stdout/stderr (`subprocess.DEVNULL`) so a slow/chatty node can't wedge the
tiny single-threaded HTTP server. To see what they're actually doing:

**These run as `root`** (the systemd services run as root — confirmed via
`sh/install-services.sh`'s own printed `user: root` line), so their ROS logs
land in `/root/.ros/log/`, *not* `~/.ros/log/` (that's only where logs go if
you run a script by hand as your own user — see below). Needs `sudo` to read:
```bash
sudo ls -t /root/.ros/log/ | head -1                          # most recent run
sudo ls /root/.ros/log/                                       # flat per-node log files here
                                                               # (only ros2 launch creates the
                                                               # per-run-folder structure; ros2 run
                                                               # writes flat <node>_<pid>_<ts>.log files)
sudo tail -n 60 /root/.ros/log/bt_navigator_<pid>_*.log       # match the pid from `ps -ef`, PIDs get reused
```

Or, for live debugging, run the script by hand instead of through the UI —
you'll see everything in real time (as your own user, so `~/.ros/log/` this
time), and it stays independent of the `argo-launcher` service:
```bash
bash sh/start_argo_nav_ui.sh --map ~/my_project/argo_sonic/src/argo_mini/maps/office_map
```

**Frontend (browser-side)**: DevTools (F12) → Console tab. This is where
React errors, the rosbridge WebSocket connect/close events, and the actual
cause behind a toast message show up.

## 7. Smoke tests

**Backend only, no browser:**
```bash
curl -s http://localhost:8888/status; echo
curl -s -X POST http://localhost:8888/start -H "Content-Type: application/json" -d '{"mode":"navigate","map":"office_map"}'
sleep 5
ps -ef | grep -E "slam_toolbox|bt_navigator|behavior_server" | grep -v grep
curl -s http://localhost:8888/status; echo
curl -s -X POST http://localhost:8888/stop
```
Expect the three processes to show up and `/status` to report
`"running": true, "mode": "navigate", "map": "office_map"`.

**Confirm the right script ran, not the wrong stack:**
```bash
ps -ef | grep -E "start_slam_ui|start_slam_explore_ui|start_argo_nav_ui" | grep -v grep
```
For `navigate` mode you should see `slam_toolbox`, `bt_navigator`,
`behavior_server`, `planner_server`, `controller_server`,
`velocity_smoother`, `depth_safety_shield` — and **not** `frontier_explorer`
(that only belongs to `auto` mode).

## 8. Known gaps (intentionally out of scope so far)

- ROS node stdout/stderr isn't captured anywhere except `~/.ros/log/` — see §6.
- `waypoint_manager.py` is still a separate, manually-run SSH/REPL tool (`s`/`c`/`g`/`x`/`p`/`l`/`q` commands) — the UI's "Send Argo here" buttons publish directly to `/goal_pose` and don't use it.
- The camera/depth safety shield in `navigate` mode defaults to **on** (matching the original `start_argo_nav.sh`); pass `--no-cam` by hand if the HP60C camera or its SDK isn't present on a given deployment.
- `frontend/public/order.html` / `menu.html` (the food-ordering/menu-catalog feature) are untouched, separate systems — not covered by anything in this doc.
