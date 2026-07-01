#!/usr/bin/env bash
# install/teams.sh — Microsoft Teams-specific installation steps.
#
# Called by install.sh after the common steps complete.
# Writes the Teams destination (team + channel) and the Microsoft Graph access
# token into the agent config so the monitor and interface can authenticate.
#
# Credentials:
#   --teams-id / --channel-id   destination identifiers (passed as install args)
#   MICROSOFT_GRAPH_ACCESS_TOKEN  Graph bearer token, exported by the installer
#                                 after reading the `MSTeams=` line from
#                                 /dev/shm/mcp-token (never passed as a CLI arg)
#
# Usage:
#   bash install/teams.sh --teams-id "TEAM_ID" --channel-id "CHANNEL_ID"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NINJA_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------

TEAMS_ID=""
CHANNEL_ID=""
TEAMS_AGENT="ninja"  # always ninja — only one agent in this repo

usage() {
    echo "Usage: $0 --teams-id TEAM_ID --channel-id CHANNEL_ID"
    echo ""
    echo "Options:"
    echo "  --teams-id TEAM_ID        Microsoft Teams team ID (required)"
    echo "  --channel-id CHANNEL_ID   Microsoft Teams channel ID (required)"
    echo "  --help                    Show this help message"
    echo ""
    echo "Requires the MICROSOFT_GRAPH_ACCESS_TOKEN environment variable."
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --teams-id)   TEAMS_ID="$2"; shift 2 ;;
        --channel-id) CHANNEL_ID="$2"; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) echo "Unknown option: $1"; usage; exit 1 ;;
    esac
done

if [[ -z "$TEAMS_ID" || -z "$CHANNEL_ID" ]]; then
    echo "ERROR: --teams-id and --channel-id are required for Microsoft Teams"
    usage
    exit 1
fi

if [[ -z "${MICROSOFT_GRAPH_ACCESS_TOKEN:-}" ]]; then
    echo "ERROR: MICROSOFT_GRAPH_ACCESS_TOKEN is not set."
    echo "       The installer must export it from the 'MSTeams=' entry in"
    echo "       /dev/shm/mcp-token before invoking this script."
    exit 1
fi

# ---------------------------------------------------------------------------
# Configure Microsoft Teams credentials
# ---------------------------------------------------------------------------
echo ""
echo "▶ Configuring Microsoft Teams channel..."

python "$NINJA_DIR/messaging/teams/interface.py" config \
    --set-team-id "$TEAMS_ID" \
    --set-channel-id "$CHANNEL_ID" \
    --set-access-token "$MICROSOFT_GRAPH_ACCESS_TOKEN" \
    --set-agent "$TEAMS_AGENT"

echo "  ✓ Teams team ID set to:    $TEAMS_ID"
echo "  ✓ Teams channel ID set to: $CHANNEL_ID"
echo "  ✓ Microsoft Graph access token stored (${#MICROSOFT_GRAPH_ACCESS_TOKEN} chars)"
echo "  ✓ Teams agent set to:    $TEAMS_AGENT"
