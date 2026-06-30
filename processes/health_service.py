#!/usr/bin/env python3
"""
Health Service — Periodically validates external-service credentials.

This service runs independently of the monitor. It wakes up every 15 minutes
and validates:
  - the active messaging channel credentials (Slack/WhatsApp/Teams via ABC),
  - the GitHub token in /dev/shm/mcp-token (gh auth status),
  - the LiteLLM gateway connection (authenticated GET /v1/models — validates
    the key and connectivity without consuming tokens),
  - the Pipedream Connect credentials (minimal catalog call),
  - the browser VPN (Psiphon tunnel liveness + IP egress check),
  - the monitor process heartbeat (stale file detection).

Each check emits a PostHog metric **only when it fails** (``error=1``); a
healthy check emits nothing. Every emission includes the sandbox ID, so we can
alert on an expired credential or a broken gateway without coupling the check to
the monitor's message-polling loop.

Usage:
    python processes/health_service.py                 # run forever, 15-min interval
    python processes/health_service.py --interval 600  # custom interval in seconds
    python processes/health_service.py --once          # run a single check and exit
"""

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from browser.browser_server import PSIPHON_HOST, PSIPHON_PORT, PSIPHON_PROXY
from clients.litellm_client import api_url, get_config, get_headers
from clients.posthog_client import capture
from messaging import get_messaging_interface
from processes.monitor import MONITOR_HEARTBEAT_FILE
from tools.token_health import check_github_token
from utils.pipedream import PipedreamClient

# How often to check, in seconds.
CHECK_INTERVAL = 15 * 60  # 15 minutes

# Lightweight IP-echo endpoint used to confirm browser traffic routes through
# the Psiphon tunnel and that the egress IP differs from the sandbox's direct IP.
IP_ECHO_URL = "https://api.ipify.org?format=json"

# A heartbeat file older than this (seconds) means the monitor has stalled.
MONITOR_STALE_AFTER = 5 * 60  # 5 minutes

# Liveness heartbeat for this service: overwritten with the current unix
# timestamp at the end of every check cycle. Mirrors MONITOR_HEARTBEAT_FILE so
# an external watcher can detect a stalled health service. Lives in /tmp.
HEALTH_HEARTBEAT_FILE = Path("/tmp/ninja_health_heartbeat")


def write_health_heartbeat() -> None:
    """Overwrite HEALTH_HEARTBEAT_FILE with the current unix timestamp.

    Called at the end of every check cycle so supervisord/monitoring can
    detect a stalled health service. Best-effort — never raises.
    """
    try:
        HEALTH_HEARTBEAT_FILE.write_text(str(int(time.time())))
    except OSError:
        pass


def _emit_error(event: str, status: str, **extra) -> None:
    """Emit a health metric with ``error=1`` — only called when a check fails."""
    capture(event, {"error": 1, "status": status, **extra})


def check_messaging_health() -> int:
    """Validate the active messaging channel credentials via the ABC.

    Resolves the active channel from MESSAGING_CHANNEL env-var (default: slack)
    and delegates to the adapter's check_messaging_health() implementation.
    Returns 1 on error, 0 when credentials are valid.
    """
    channel = os.environ.get("MESSAGING_CHANNEL", "slack")
    try:
        result = get_messaging_interface().check_messaging_health()
    except Exception as e:
        result = {"service": channel, "status": "error", "message": str(e)}

    if result["status"] == "ok":
        print(f"🔑 {channel} token OK", flush=True)
        return 0

    _emit_error(
        f"ninja {channel} health",
        result["status"],
        message=result.get("message", ""),
    )
    print(
        f"🔑 {channel} token ERROR (status={result['status']}"
        f"{', ' + result['message'] if result.get('message') else ''})",
        flush=True,
    )
    return 1


def check_github_health() -> int:
    """Emit ``ninja github health`` (error=1) only if the GitHub token is bad.

    Returns 1 on error, 0 when the token is valid. A missing token counts as an
    error (logged as "Github token not found").
    """
    result = check_github_token()
    if result["status"] == "ok":
        print("🔑 GitHub token OK", flush=True)
        return 0

    if result["status"] == "missing":
        print("🔑 Github token not found", flush=True)
    else:
        print(
            f"🔑 GitHub token ERROR (status={result['status']}"
            f"{', ' + result['message'] if result.get('message') else ''})",
            flush=True,
        )
    _emit_error(
        "ninja github health", result["status"], message=result.get("message", "")
    )
    return 1


def check_litellm_health() -> int:
    """Emit ``ninja litellm health`` (error=1) only if the gateway probe fails.

    Issues an authenticated ``GET /v1/models`` against the gateway. This both
    validates the API key (401/403 on a bad/expired key) and confirms
    connectivity, without invoking a model or consuming any tokens. Returns 1 on
    error, 0 on success.
    """
    cfg = get_config()
    api_key = cfg.get("api_key")
    base_url = cfg.get("base_url")

    if not api_key or not base_url:
        print(
            "🤖 LiteLLM not configured (missing api_key/base_url in settings.json)",
            flush=True,
        )
        _emit_error("ninja litellm health", "missing")
        return 1

    req = urllib.request.Request(
        api_url("/v1/models"),
        headers=get_headers(),
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = "ok" if resp.status == 200 else f"http_{resp.status}"
    except urllib.error.HTTPError as e:
        status = f"http_{e.code}"
    except Exception as e:
        status = str(e)

    if status == "ok":
        print("🤖 LiteLLM OK", flush=True)
        return 0
    _emit_error("ninja litellm health", status)
    print(f"🤖 LiteLLM ERROR (status={status})", flush=True)
    return 1


def check_pipedream_health() -> int:
    """Emit ``ninja pipedream health`` (error=1) only if the OAuth probe fails.

    Instantiates PipedreamClient and makes a minimal catalog call to force the
    OAuth exchange. Returns 1 on error, 0 on success.
    """
    try:
        client = PipedreamClient()
    except RuntimeError:
        print("🔌 Pipedream credentials not found", flush=True)
        _emit_error("ninja pipedream health", "missing")
        return 1
    except Exception as e:
        print(f"🔌 Pipedream not available: {e}", flush=True)
        _emit_error("ninja pipedream health", "error")
        return 1

    try:
        client.list_apps(limit=1)
    except Exception as e:
        _emit_error("ninja pipedream health", str(e)[:120])
        print(f"🔌 Pipedream ERROR ({str(e)[:120]})", flush=True)
        return 1

    print("🔌 Pipedream OK", flush=True)
    return 0


def _fetch_egress_ip(proxy: str | None) -> str | None:
    """Return the public egress IP via IP_ECHO_URL, optionally through proxy."""
    handler = (
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        if proxy
        else urllib.request.ProxyHandler({})
    )
    opener = urllib.request.build_opener(handler)
    try:
        with opener.open(IP_ECHO_URL, timeout=15) as resp:
            return json.loads(resp.read().decode()).get("ip")
    except Exception:
        return None


def check_vpn_health() -> int:
    """Emit ``ninja vpn health`` (error=1) only if the browser VPN is broken.

    Layered probe: port open → traffic routes → egress IP differs from direct IP.
    Returns 1 on error, 0 on success.
    """
    try:
        with socket.create_connection((PSIPHON_HOST, PSIPHON_PORT), timeout=5):
            pass
    except OSError as e:
        _emit_error("ninja vpn health", "proxy_down", message=str(e)[:120])
        print(f"🛡️ VPN ERROR (proxy {PSIPHON_PROXY} not listening: {e})", flush=True)
        return 1

    proxied_ip = _fetch_egress_ip(PSIPHON_PROXY)
    if not proxied_ip:
        _emit_error("ninja vpn health", "no_route")
        print("🛡️ VPN ERROR (proxy up but no route to internet)", flush=True)
        return 1

    direct_ip = _fetch_egress_ip(None)
    if direct_ip and direct_ip == proxied_ip:
        _emit_error("ninja vpn health", "not_tunneled")
        print("🛡️ VPN ERROR (egress IP == direct IP; not tunneling)", flush=True)
        return 1

    print("🛡️ VPN OK", flush=True)
    return 0


def check_monitor_health() -> int:
    """Emit ``ninja monitor health`` (error=1) only if the monitor heartbeat is stale.

    The monitor overwrites MONITOR_HEARTBEAT_FILE with a unix timestamp on every
    poll tick. A missing or stale file means the monitor has stalled.
    Returns 1 on error, 0 when fresh.
    """
    try:
        with open(MONITOR_HEARTBEAT_FILE) as f:
            last_run_ts = int(f.read().strip())
    except (OSError, ValueError) as e:
        _emit_error("ninja monitor health", "missing", message=str(e)[:120])
        print(
            f"📡 Monitor heartbeat missing ({MONITOR_HEARTBEAT_FILE}: {e})",
            flush=True,
        )
        return 1

    age_seconds = int(time.time()) - last_run_ts
    if age_seconds > MONITOR_STALE_AFTER:
        _emit_error("ninja monitor health", "stale", age_seconds=age_seconds)
        print(
            f"📡 Monitor heartbeat STALE "
            f"(age={age_seconds}s > {MONITOR_STALE_AFTER}s)",
            flush=True,
        )
        return 1

    print(f"📡 Monitor heartbeat OK (age={age_seconds}s)", flush=True)
    return 0


def main():
    channel = os.environ.get("MESSAGING_CHANNEL", "slack")

    parser = argparse.ArgumentParser(
        description="Health Service - periodically validate service credentials"
    )
    parser.add_argument(
        "--interval",
        "-i",
        type=int,
        default=CHECK_INTERVAL,
        help=f"Check interval in seconds (default: {CHECK_INTERVAL})",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single check and exit (don't loop)",
    )
    args = parser.parse_args()

    if args.once:
        check_messaging_health()
        check_github_health()
        check_litellm_health()
        check_pipedream_health()
        check_vpn_health()
        check_monitor_health()
        write_health_heartbeat()
        return

    print(
        f"🏥 Health service started — checking {channel}, GitHub, LiteLLM, "
        f"Pipedream, VPN and monitor every {args.interval // 60} min",
        flush=True,
    )

    checks = (
        ("messaging", check_messaging_health),
        ("github", check_github_health),
        ("litellm", check_litellm_health),
        ("pipedream", check_pipedream_health),
        ("vpn", check_vpn_health),
        ("monitor", check_monitor_health),
    )

    try:
        while True:
            for name, check in checks:
                try:
                    check()
                except Exception as e:
                    print(f"⚠️ {name} health check crashed: {e}", flush=True)
            write_health_heartbeat()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n👋 Health service stopped", flush=True)


if __name__ == "__main__":
    main()
