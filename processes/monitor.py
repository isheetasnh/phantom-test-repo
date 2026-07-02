#!/usr/bin/env python3
"""
Agent Monitor - Watches the messaging channel for mentions and triggers agent responses.

Polls the configured channel on a fixed interval, batches all new messages
(mentions, thread replies, cron jobs) and dispatches them to Claude in one
prompt per cycle.

Lifecycle responsibilities only — business logic lives in services/monitor_service.py,
config / state I/O lives in core/config.py.

Usage:
    python monitor.py              # Run with configured agent
    python monitor.py --agent ninja # Run as specific agent
"""

import os
import signal
import subprocess
import sys
import time
from functools import cache
from pathlib import Path
from typing import Optional

from agents_config import AGENTS
from clients.posthog_client import capture

# core — config cache, SIGHUP, state I/O
from core.config import (
    install_sighup_handler,
    load_agent_config,
    load_agent_messages,
    load_seen_messages,
    save_agent_messages,
    save_seen_messages,
)

# Messaging interface — channel-agnostic ABC via factory
from messaging import MessagingInterface, get_messaging_interface

# Issue-driven loop — see agent-docs/LOOP.md
from processes.orchestrator import (
    ORCHESTRATOR_SERVICE,
    count_open_issues,
    is_orchestrator_running,
)

# Cron scheduler — see agent-docs/CRON.md and tools/cron.py
from services.cron_service import claim_cron, get_due_cron_messages

# Monitor business logic
from services.monitor_service import build_welcome_message, run_batched_response

# ---------------------------------------------------------------------------
# Messaging singleton
# ---------------------------------------------------------------------------


@cache
def _get_messaging() -> MessagingInterface:
    """Get a persistent MessagingInterface instance (created once, cached)."""
    return get_messaging_interface()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

POLL_INTERVAL = 60  # base seconds
POLL_JITTER = 5  # random jitter seconds
MAX_RUNTIME = 24 * 60 * 60  # 24 hours in seconds

BACKOFF_INITIAL = 60  # Initial backoff: 1 minute
BACKOFF_MAX = 600  # Max backoff: 10 minutes
BACKOFF_MULTIPLIER = 2

# Liveness heartbeat — overwritten with the current unix timestamp on every poll
# tick. processes/health_service.py reads it to surface monitor liveness to PostHog.
# Lives in /tmp (sandbox-local), mirroring the orchestrator's heartbeat file.
MONITOR_HEARTBEAT_FILE = Path("/tmp/ninja_monitor_heartbeat")


# ---------------------------------------------------------------------------
# Rate limit handler (tightly coupled to poll loop — stays here)
# ---------------------------------------------------------------------------


class RateLimitHandler:
    """Handles exponential backoff for rate limiting."""

    def __init__(self):
        self.current_backoff = 0
        self.consecutive_rate_limits = 0
        self.last_rate_limit_time = 0

    def on_rate_limit(self):
        self.consecutive_rate_limits += 1
        self.last_rate_limit_time = time.time()
        if self.current_backoff == 0:
            self.current_backoff = BACKOFF_INITIAL
        else:
            self.current_backoff = min(
                self.current_backoff * BACKOFF_MULTIPLIER, BACKOFF_MAX
            )
        print(
            f"⚠️ Rate limited! Backing off for {self.current_backoff}s "
            f"(attempt #{self.consecutive_rate_limits})",
            flush=True,
        )
        return self.current_backoff

    def on_success(self):
        if self.consecutive_rate_limits > 0:
            print(
                f"✅ Rate limit cleared after {self.consecutive_rate_limits} retries",
                flush=True,
            )
        self.current_backoff = 0
        self.consecutive_rate_limits = 0

    def is_backing_off(self) -> bool:
        if self.current_backoff == 0:
            return False
        return (time.time() - self.last_rate_limit_time) < self.current_backoff

    def get_remaining_backoff(self) -> float:
        if not self.is_backing_off():
            return 0
        return max(0, self.current_backoff - (time.time() - self.last_rate_limit_time))


rate_limiter = RateLimitHandler()


# ---------------------------------------------------------------------------
# Orchestrator + heartbeat
# ---------------------------------------------------------------------------


def write_monitor_heartbeat() -> None:
    """Overwrite MONITOR_HEARTBEAT_FILE with the current unix timestamp.

    Called on every poll tick so health_service.py can detect a stalled monitor.
    Best-effort — never raises.
    """
    try:
        MONITOR_HEARTBEAT_FILE.write_text(str(int(time.time())))
    except OSError:
        pass


def maybe_launch_orchestrator() -> bool:
    """Launch the orchestrator if there is open work and it isn't already running.

    Always launches via systemd (ninja.service). See agent-docs/LOOP.md.
    """
    if is_orchestrator_running():
        return False
    open_issues = count_open_issues()
    if open_issues <= 0:
        return False

    print(
        f"🚀 {open_issues} open issue(s) and orchestrator idle — launching",
        flush=True,
    )
    try:
        result = subprocess.run(
            ["systemctl", "start", ORCHESTRATOR_SERVICE],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            print(f"✅ Started {ORCHESTRATOR_SERVICE} via systemd", flush=True)
            return True
        print(
            f"⚠️ systemctl start {ORCHESTRATOR_SERVICE} failed "
            f"({result.returncode}): {result.stderr.strip()}",
            flush=True,
        )
        return False
    except (OSError, subprocess.SubprocessError) as e:
        print(f"⚠️ Could not launch {ORCHESTRATOR_SERVICE}: {e}", flush=True)
        return False


def maybe_emit_heartbeat(
    agent_id: str, start_time: float, last_heartbeat: float
) -> float:
    """Emit a monitor-alive heartbeat metric at most once per minute.

    Also refreshes the /tmp liveness file on every call (cheap, not rate-limited)
    so health_service.py tracks the poll loop even between PostHog emissions.
    """
    # Refresh liveness file every tick regardless of rate-limit window
    write_monitor_heartbeat()

    now = time.time()
    if now - last_heartbeat < 60:
        return last_heartbeat
    capture("ninja monitor heartbeat", {"uptime_seconds": int(now - start_time)})
    print(f"💗 Emitted heartbeat for {agent_id}", flush=True)
    return now


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    import argparse
    import random

    # Wire SIGHUP → refresh all config caches (config hot-reload without restart)
    install_sighup_handler()

    parser = argparse.ArgumentParser(
        description="Agent Monitor - Watch the messaging channel for mentions"
    )
    parser.add_argument("--agent", "-a", help="Agent to run as (default: from config)")
    parser.add_argument(
        "--interval",
        "-i",
        type=int,
        default=POLL_INTERVAL,
        help="Poll interval in seconds",
    )
    args = parser.parse_args()

    config = load_agent_config()
    agent_id = args.agent or config.get("default_agent", "").lower()

    if not agent_id or agent_id not in AGENTS:
        print("❌ No valid agent configured!", file=sys.stderr)
        print(f"Available agents: {', '.join(AGENTS.keys())}", file=sys.stderr)
        print("Set 'default_agent' in ~/.agent_settings.json", file=sys.stderr)
        sys.exit(1)

    agent = AGENTS[agent_id]
    channel = os.environ.get("MESSAGING_CHANNEL", "slack")

    print(
        f"""
╔══════════════════════════════════════════════════════════════╗
║  {agent['emoji']} {agent['name']} Monitor - Watching for mentions
╠══════════════════════════════════════════════════════════════╣
║  Agent:    {agent['name']} ({agent['role']})
║  Channel:  {channel}
║  Polling:  Every {args.interval}s (+{POLL_JITTER}s jitter)
║  Runtime:  max {MAX_RUNTIME // 60} minutes
║  Mentions: {', '.join(agent['mentions'])}
╚══════════════════════════════════════════════════════════════╝
""",
        flush=True,
    )

    iface = _get_messaging()
    seen_messages = load_seen_messages()
    agent_data = load_agent_messages()
    start_time = time.time()
    last_heartbeat = 0.0

    iface.post_welcome_if_needed(agent, build_welcome_message(agent))

    print(f"📡 Starting monitor loop (max {MAX_RUNTIME // 60} minutes)...", flush=True)

    try:
        while True:
            last_heartbeat = maybe_emit_heartbeat(agent_id, start_time, last_heartbeat)

            if time.time() - start_time >= MAX_RUNTIME:
                print(
                    f"\n⏰ Max runtime ({MAX_RUNTIME // 60} minutes) reached. Stopping.",
                    flush=True,
                )
                break

            if rate_limiter.is_backing_off():
                remaining = rate_limiter.get_remaining_backoff()
                print(
                    f"⏳ Rate limit backoff: {remaining:.0f}s remaining...", flush=True
                )
                time.sleep(min(remaining, 30))
                continue

            # --- collect messages ---
            try:
                raw_messages = iface.get_history(limit=50)
                rate_limiter.on_success()
            except Exception as e:
                err = str(e).lower()
                if "ratelimit" in err or "rate" in err:
                    backoff_time = rate_limiter.on_rate_limit()
                    time.sleep(min(backoff_time, 30))
                else:
                    print(f"⚠️ Error reading messages: {e}", file=sys.stderr)
                continue

            print(f"📨 Got {len(raw_messages)} messages", flush=True)

            pending_messages: list = []

            for msg in raw_messages:
                iface.collect_pending(
                    msg,
                    agent.get("mentions", []),
                    seen_messages,
                    agent_data,
                    pending_messages,
                )

            # --- inject due cron jobs ---
            for job in get_due_cron_messages(time.time()):
                if claim_cron(job["id"]):
                    pending_messages.append(
                        {
                            "user": "cron",
                            "text": job["prompt"],
                            "timestamp": f"cron:{job['id']}:{int(time.time())}",
                            "thread_ts": job.get("thread_ts"),
                            "type": "cron",
                            "cron_id": job["id"],
                        }
                    )
                    print(
                        f"  ⏰ Cron job '{job['id']}' is due — queued for batch",
                        flush=True,
                    )

            # --- dispatch ---
            if pending_messages:
                capture(
                    "ninja batch processing started",
                    {"message_count": len(pending_messages)},
                )
                print(
                    f"\n📋 Processing {len(pending_messages)} pending message(s)...",
                    flush=True,
                )
                run_batched_response(agent, pending_messages, iface.say)

            maybe_launch_orchestrator()

            save_seen_messages(seen_messages)
            save_agent_messages(agent_data)

            jitter = random.uniform(0, POLL_JITTER)
            sleep_time = args.interval + jitter
            if rate_limiter.consecutive_rate_limits > 0:
                sleep_time += BACKOFF_INITIAL / 2
                print(
                    f"💤 Extended sleep due to recent rate limits: {sleep_time:.0f}s",
                    flush=True,
                )
            time.sleep(sleep_time)

    except KeyboardInterrupt:
        print("\n\n👋 Monitor stopped")
        save_seen_messages(seen_messages)
        save_agent_messages(agent_data)


if __name__ == "__main__":
    main()
