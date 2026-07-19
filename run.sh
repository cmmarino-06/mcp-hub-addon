#!/bin/bash
set -euo pipefail

OPTIONS_FILE="/data/options.json"

HA_MCP_URL="$(jq -r '.ha_mcp_url' "$OPTIONS_FILE")"
UNIFI_MCP_URL="$(jq -r '.unifi_mcp_url' "$OPTIONS_FILE")"
UNIFI_MCP_TOKEN="$(jq -r '.unifi_mcp_token' "$OPTIONS_FILE")"
HUB_SECRET_PATH="$(jq -r '.hub_secret_path // ""' "$OPTIONS_FILE")"
LOG_LEVEL="$(jq -r '.log_level // "info"' "$OPTIONS_FILE" | tr '[:lower:]' '[:upper:]')"

if [ -z "$HUB_SECRET_PATH" ]; then
    HUB_SECRET_PATH="/private_$(openssl rand -hex 24)"
    echo "No hub_secret_path set — generated one. Set this in the add-on"
    echo "Configuration tab so it survives reinstalls, and use it as the"
    echo "path segment in the Cloudflare Tunnel + claude.ai connector URL:"
    echo "  $HUB_SECRET_PATH"
fi

export HA_MCP_URL UNIFI_MCP_URL UNIFI_MCP_TOKEN HUB_SECRET_PATH LOG_LEVEL
export PORT="9585"

exec gosu mcp python3 /app/app.py
