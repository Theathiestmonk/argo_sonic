#!/bin/bash
# install-services.sh
# Run once on the Jetson to set up auto-start for the UI and launcher.
#
# Usage:
#   bash ~/dhruvil/argo_sonic/install-services.sh
#
# After this, on every boot:
#   - http://<jetson-ip>:3000  → Argo setup UI
#   - http://<jetson-ip>:8888  → Launcher API (called by UI "Start Argo" button)
#
# The UI "Start Argo" button calls the launcher which runs start_slam_explore.sh
# (rosbridge is included in that script so the UI connects automatically).

set -e

REPO="$(cd "$(dirname "$0")" && pwd)"
WHOAMI="$(whoami)"
PYTHON="$(which python3)"
NPM="$(which npm)"

echo ""
echo "================================================"
echo "  Argo Services Installer"
echo "  repo:   $REPO"
echo "  user:   $WHOAMI"
echo "  python: $PYTHON"
echo "================================================"
echo ""

# ── Step 1: Build the React frontend ──────────────────────────────────────────
echo "[install] Building frontend..."
cd "$REPO/frontend"
$NPM run build
echo "[install] Frontend built → $REPO/frontend/dist"

# ── Step 2: Create argo-launcher.service ──────────────────────────────────────
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

# ── Step 3: Create argo-ui.service ────────────────────────────────────────────
# Serves the built React app with Python's built-in HTTP server — no Node needed at runtime.
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

# ── Step 4: Enable and start both services ─────────────────────────────────────
echo "[install] Enabling services..."
sudo systemctl daemon-reload
sudo systemctl enable argo-launcher.service argo-ui.service
sudo systemctl restart argo-launcher.service argo-ui.service

echo ""
echo "================================================"
echo "  Done! Services are running."
echo ""
echo "  UI       →  http://$(hostname -I | awk '{print $1}'):3000"
echo "  Launcher →  http://$(hostname -I | awk '{print $1}'):8888"
echo ""
echo "  Check status:"
echo "    sudo systemctl status argo-ui"
echo "    sudo systemctl status argo-launcher"
echo ""
echo "  View logs:"
echo "    journalctl -u argo-ui -f"
echo "    journalctl -u argo-launcher -f"
echo "================================================"
echo ""
