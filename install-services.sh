#!/bin/bash
# install-services.sh
# Run on the Jetson (safe to re-run after updates).
#
# Creates 3 systemd services that auto-start at boot:
#   argo-rosbridge  →  rosbridge WebSocket (port 9090)  — always on
#   argo-launcher   →  launcher HTTP API   (port 8888)  — always on
#   argo-ui         →  React frontend      (port 3000)  — always on
#
# User flow after this:
#   Open http://<jetson-ip>:3000 → UI loads, already connected to ROS
#   Click "Start Argo" → launcher starts SLAM + Nav2 + frontier_explorer

set -e

REPO="$(cd "$(dirname "$0")" && pwd)"
WHOAMI="$(whoami)"
PYTHON="$(which python3)"
NPM="$(which npm)"
ROS_SETUP="/opt/ros/humble/setup.bash"

echo ""
echo "================================================"
echo "  Argo Services Installer"
echo "  repo:   $REPO"
echo "  user:   $WHOAMI"
echo "  python: $PYTHON"
echo "================================================"
echo ""

# ── Step 1: Build the React frontend ─────────────────────────────────────────
echo "[install] Building frontend..."
cd "$REPO/frontend"
$NPM run build
echo "[install] Frontend built → $REPO/frontend/dist"
cd "$REPO"

# ── Step 2: install rosbridge if missing ─────────────────────────────────────
echo "[install] Checking rosbridge_suite..."
if ! /opt/ros/humble/bin/ros2 pkg list 2>/dev/null | grep -q rosbridge_server; then
    echo "[install] rosbridge not found — installing ros-humble-rosbridge-suite..."
    sudo apt-get install -y ros-humble-rosbridge-suite
else
    echo "[install] rosbridge_server found ✓"
fi

# Make the wrapper executable
chmod +x "$REPO/backend/start-rosbridge.sh"

# ── Step 3: argo-rosbridge.service ───────────────────────────────────────────
# Rosbridge must be always-on so the UI can connect the moment you open it.
# Uses a wrapper script so systemd gets the full ROS environment (PATH fix).
echo "[install] Creating argo-rosbridge.service..."
sudo tee /etc/systemd/system/argo-rosbridge.service > /dev/null <<EOF
[Unit]
Description=ROS2 Rosbridge WebSocket Server (port 9090)
After=network.target

[Service]
Type=simple
User=$WHOAMI
ExecStart=/bin/bash $REPO/backend/start-rosbridge.sh
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# ── Step 4: argo-launcher.service ────────────────────────────────────────────
echo "[install] Creating argo-launcher.service..."
sudo tee /etc/systemd/system/argo-launcher.service > /dev/null <<EOF
[Unit]
Description=Argo Launcher HTTP Server (port 8888)
After=network.target

[Service]
Type=simple
User=$WHOAMI
WorkingDirectory=$REPO
ExecStart=$PYTHON $REPO/backend/launcher.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# ── Step 5: argo-ui.service ──────────────────────────────────────────────────
echo "[install] Creating argo-ui.service..."
sudo tee /etc/systemd/system/argo-ui.service > /dev/null <<EOF
[Unit]
Description=Argo UI Frontend (port 3000)
After=network.target

[Service]
Type=simple
User=$WHOAMI
WorkingDirectory=$REPO/frontend/dist
ExecStart=$PYTHON -m http.server 3000
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# ── Step 6: Enable and (re)start all three services ──────────────────────────
echo "[install] Enabling and starting services..."
sudo systemctl daemon-reload
sudo systemctl enable  argo-rosbridge.service argo-launcher.service argo-ui.service
sudo systemctl restart argo-rosbridge.service argo-launcher.service argo-ui.service

echo ""
echo "================================================"
echo "  Done!"
echo ""
echo "  Open this URL on any device:"
echo "  → http://$(hostname -I | awk '{print $1}'):3000"
echo ""
echo "  Services:"
echo "    rosbridge  :9090  $(sudo systemctl is-active argo-rosbridge)"
echo "    launcher   :8888  $(sudo systemctl is-active argo-launcher)"
echo "    ui         :3000  $(sudo systemctl is-active argo-ui)"
echo ""
echo "  Logs:"
echo "    journalctl -u argo-rosbridge -f"
echo "    journalctl -u argo-launcher  -f"
echo "    journalctl -u argo-ui        -f"
echo "================================================"
echo ""
