#!/bin/bash
# Thin passthrough wrapper around argo_sonic_nav.py — lets you launch the
# NTFields nav stack from anywhere without typing the full python3 path.
# All args are forwarded as-is, e.g.:
#   ./sh/run_argo_sonic_nav.sh                          # interactive map selector
#   ./sh/run_argo_sonic_nav.sh --map office_map         # skip selector
#   ./sh/run_argo_sonic_nav.sh --map office_map --no-cam --no-rviz

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
exec python3 "$SCRIPT_DIR/argo_sonic_nav.py" "$@"
