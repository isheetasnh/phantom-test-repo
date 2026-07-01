#!/usr/bin/env bash
# install.sh — Setup script for Ninja browser automation agent
#
# Usage:
#   ./install.sh --messaging-channel slack --channel "#my-channel" --channel-id "C0AAAAMBR1R"
#   ./install.sh --messaging-channel teams --teams-id "TEAM_ID" --channel-id "CHANNEL_ID"
#   ./install.sh --messaging-channel whatsapp
#
# What this does:
#   1.   Installs Python dependencies (requirements.txt)
#   1.5. Installs pdx CLI
#   2.   Creates the logs directory + writes MESSAGING_CHANNEL to /etc/environment
#   3.   Runs channel-specific setup (install/<channel>.sh)
#   4.   Installs and enables systemd services
#   5.   Configures VNC (removes password)
#   6.   Waits for browser server to be ready
#
# Prerequisites (must be provided manually — not handled by this script):
#   - s3_config.json at repo root or /root/  (AWS credentials for S3 cache)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------

MESSAGING_CHANNEL=""
CHANNEL_ARGS=()   # forwarded to the channel-specific install script

SUPPORTED_CHANNELS=("slack" "whatsapp" "teams")

usage() {
    echo "Usage: $0 --messaging-channel <slack|whatsapp|teams> [channel-specific options]"
    echo ""
    echo "Options:"
    echo "  --messaging-channel CHANNEL   Messaging channel (required: slack|whatsapp|teams)"
    echo "  --channel CHANNEL             Slack channel name — passed to channel script (e.g. '#my-channel')"
    echo "  --channel-id CHANNEL_ID       Channel ID — passed to channel script (e.g. 'C0AAAAMBR1R')"
    echo "  --workspace-id WORKSPACE_ID   Slack workspace ID — passed to channel script (optional)"
    echo "  --teams-id TEAM_ID            Microsoft Teams team ID — passed to channel script"
    echo "  --help                        Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0 --messaging-channel slack --channel '#my-channel' --channel-id 'C0AAAAMBR1R'"
    echo "  $0 --messaging-channel teams --teams-id 'TEAM_ID' --channel-id 'CHANNEL_ID'"
    echo "  $0 --messaging-channel whatsapp"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --messaging-channel) MESSAGING_CHANNEL="$2"; shift 2 ;;
        --channel|--channel-id|--workspace-id|--teams-id)
            CHANNEL_ARGS+=("$1" "$2"); shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) echo "Unknown option: $1"; usage; exit 1 ;;
    esac
done

if [[ -z "$MESSAGING_CHANNEL" ]]; then
    echo "ERROR: --messaging-channel is required"
    usage
    exit 1
fi

if [[ ! " ${SUPPORTED_CHANNELS[*]} " =~ " ${MESSAGING_CHANNEL} " ]]; then
    echo "ERROR: unsupported channel '${MESSAGING_CHANNEL}'. Choose from: ${SUPPORTED_CHANNELS[*]}"
    exit 1
fi

echo "=== Ninja Browser Automation — Setup (channel: ${MESSAGING_CHANNEL}) ==="
echo ""

# ---------------------------------------------------------------------------
# Step 1: Python dependencies
# ---------------------------------------------------------------------------
echo "▶ Installing Python dependencies..."
pip install -q -r "$SCRIPT_DIR/requirements.txt"
echo "  ✓ Python packages installed"

# Ensure the ninja package is importable by adding its parent to PYTHONPATH
NINJA_PARENT="$(cd "$SCRIPT_DIR/.." && pwd)"
if ! grep -q "$NINJA_PARENT" /etc/environment 2>/dev/null; then
    echo "PYTHONPATH=\"${NINJA_PARENT}:\${PYTHONPATH:-}\"" >> /etc/environment
fi
export PYTHONPATH="${NINJA_PARENT}:${PYTHONPATH:-}"
echo "  ✓ PYTHONPATH configured (${NINJA_PARENT})"

# ---------------------------------------------------------------------------
# Step 1.5: Install pdx CLI (Pipedream LLM wrapper)
# ---------------------------------------------------------------------------
PDX_SRC="$SCRIPT_DIR/bin/pdx"
PDX_DST="/usr/local/bin/pdx"
if [[ -f "$PDX_SRC" ]]; then
    chmod +x "$PDX_SRC"
    ln -sf "$PDX_SRC" "$PDX_DST"
    echo "  ✓ pdx CLI installed → $PDX_DST"
else
    echo "  ⚠ bin/pdx not found — skipping pdx install"
fi

# ---------------------------------------------------------------------------
# Step 2: Log directory + MESSAGING_CHANNEL → /etc/environment
# ---------------------------------------------------------------------------
mkdir -p /workspace/logs
echo "  ✓ Log directory ready (/workspace/logs)"

# Write MESSAGING_CHANNEL so all systemd services inherit the correct adapter.
if grep -q "^MESSAGING_CHANNEL=" /etc/environment 2>/dev/null; then
    sed -i "s/^MESSAGING_CHANNEL=.*/MESSAGING_CHANNEL=${MESSAGING_CHANNEL}/" /etc/environment
else
    echo "MESSAGING_CHANNEL=${MESSAGING_CHANNEL}" >> /etc/environment
fi
export MESSAGING_CHANNEL
echo "  ✓ Messaging channel: ${MESSAGING_CHANNEL}"

# ---------------------------------------------------------------------------
# Step 3: Channel-specific setup
# ---------------------------------------------------------------------------
CHANNEL_INSTALL="$SCRIPT_DIR/install/${MESSAGING_CHANNEL}.sh"
if [[ ! -f "$CHANNEL_INSTALL" ]]; then
    echo "❌ No install script found for channel: ${MESSAGING_CHANNEL}"
    echo "   Expected: $CHANNEL_INSTALL"
    exit 1
fi

bash "$CHANNEL_INSTALL" "${CHANNEL_ARGS[@]}"

# ---------------------------------------------------------------------------
# Step 4: Systemd services
# ---------------------------------------------------------------------------
echo ""
echo "▶ Installing systemd services..."
cp "$SCRIPT_DIR/systemd/ninja-sync.service"         /etc/systemd/system/ninja-sync.service
cp "$SCRIPT_DIR/systemd/ninja.service"              /etc/systemd/system/ninja.service
cp "$SCRIPT_DIR/systemd/ninja-monitor.service"      /etc/systemd/system/ninja-monitor.service
cp "$SCRIPT_DIR/systemd/ninja-dashboard.service"    /etc/systemd/system/ninja-dashboard.service
cp "$SCRIPT_DIR/systemd/ninja-integrations.service" /etc/systemd/system/ninja-integrations.service
cp "$SCRIPT_DIR/systemd/ninja-health.service"       /etc/systemd/system/ninja-health.service

# systemd ignores /etc/environment, so inject MESSAGING_CHANNEL into the units
# that build a messaging interface (else they fall back to the "slack" default).
for svc in ninja-monitor ninja-health; do
    mkdir -p "/etc/systemd/system/${svc}.service.d"
    printf '[Service]\nEnvironment=MESSAGING_CHANNEL=%s\n' "$MESSAGING_CHANNEL" \
        > "/etc/systemd/system/${svc}.service.d/channel.conf"
done

systemctl daemon-reload
systemctl enable ninja-sync.service ninja.service ninja-monitor.service ninja-dashboard.service ninja-integrations.service ninja-health.service
systemctl start  ninja-sync.service ninja.service ninja-monitor.service ninja-dashboard.service ninja-integrations.service ninja-health.service
echo "  ✓ ninja-sync.service installed, enabled and started"
echo "  ✓ ninja.service installed and enabled (single work cycle, restarts on failure)"
echo "  ✓ ninja-monitor.service installed, enabled and started (continuous messaging channel watcher)"
echo "  ✓ ninja-dashboard.service installed, enabled and started (port 9000)"
echo "  ✓ ninja-integrations.service installed, enabled and started (port 9020)"
echo "  ✓ ninja-health.service installed, enabled and started (periodic credential and dependency health checks)"

# ---------------------------------------------------------------------------
# Step 5: VNC password-free configuration
# ---------------------------------------------------------------------------
echo ""
echo "▶ Configuring VNC (removing password requirement)..."

SUPERVISOR_CONF="/etc/supervisor/conf.d/supervisord.conf"

if [[ -f "$SUPERVISOR_CONF" ]]; then
    sed -i 's|x11vnc -display :99 -forever -shared -rfbauth /root/.vnc/passwd -rfbport 5901|x11vnc -display :99 -forever -shared -nopw -rfbport 5901|g' "$SUPERVISOR_CONF"
    supervisorctl reread
    supervisorctl update
    supervisorctl restart x11vnc
    echo "  ✓ VNC configured to run without password (-nopw)"
    echo "  ✓ x11vnc restarted with new config"
else
    echo "  ⚠ Supervisor config not found at $SUPERVISOR_CONF — skipping VNC patch"
fi

# ---------------------------------------------------------------------------
# Step 6: Wait for browser server to be ready
# ---------------------------------------------------------------------------
echo ""
echo "▶ Waiting for browser server to be ready on port 9222..."
BROWSER_TIMEOUT=60
BROWSER_READY=false
for i in $(seq 1 "$BROWSER_TIMEOUT"); do
    if curl -sf http://localhost:9222/json/version >/dev/null 2>&1; then
        BROWSER_READY=true
        echo "  ✓ Browser server ready (${i}s)"
        break
    fi
    sleep 1
done

if [[ "$BROWSER_READY" == "false" ]]; then
    echo "  ⚠ Browser not responding after ${BROWSER_TIMEOUT}s — attempting manual start..."
    python "$SCRIPT_DIR/../browser/browser_server.py" start || true
    sleep 5
    if curl -sf http://localhost:9222/json/version >/dev/null 2>&1; then
        echo "  ✓ Browser started successfully"
    else
        echo "  ⚠ Browser could not be started — health check may still fail"
    fi
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "=== Setup complete ==="
echo ""
echo "Useful commands:"
echo "  systemctl status <service_name>             # Check service status"
echo "  journalctl -u <service_name> -f             # Follow service logs"
echo "  Dashboard: http://localhost:9000"
