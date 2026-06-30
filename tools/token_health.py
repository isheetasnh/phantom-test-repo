#!/usr/bin/env python3
"""
Token Health — Validate the active messaging channel and GitHub tokens.

Unlike health_check.py's config-level checks, this module validates the tokens
*currently in use* against their live APIs:
  - Messaging channel: delegated to the active adapter via the ABC
    (check_messaging_health() — works for Slack, WhatsApp, Teams, etc.)
  - GitHub: `gh auth status` (the session gh actually authenticated with)

Each check returns a small dict — {"service", "status", ...} — where status is
one of: "ok", "missing", "invalid", "error". Never raises.

Usage:
    python tools/token_health.py            # human-readable
    python tools/token_health.py --json     # JSON for scripting

Python API:
    from tools.token_health import check_messaging_token, check_github_token
"""

import json
import os
import subprocess
import sys

from messaging import get_messaging_interface

MCP_TOKEN_FILE = "/dev/shm/mcp-token"


def _parse_mcp_tokens(filepath: str = MCP_TOKEN_FILE) -> dict:
    """Parse /dev/shm/mcp-token into a dict of service → value.

    Each line is either ``KEY=value`` or ``KEY={"json": "object"}``.
    Returns an empty dict if the file is missing or unreadable.
    """
    tokens: dict = {}
    try:
        with open(filepath, "r") as f:
            content = f.read()
        for line in content.strip().split("\n"):
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if value.startswith("{"):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    pass
            tokens[key] = value
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"⚠️ Error parsing {filepath}: {e}", file=sys.stderr)
    return tokens


def check_messaging_token(filepath: str = MCP_TOKEN_FILE) -> dict:
    """Validate the active messaging channel credentials via the ABC.

    Delegates entirely to the adapter's ``check_messaging_health()`` — no
    channel-specific logic here. The active channel is resolved from the
    ``MESSAGING_CHANNEL`` env-var (default: slack).

    Returns a dict with at minimum:
        ``service``  — channel name (e.g. "slack", "whatsapp", "teams")
        ``status``   — "ok", "missing", "invalid", or "error"
    Never raises.
    """
    try:
        return get_messaging_interface().check_messaging_health()
    except Exception as e:
        channel = os.environ.get("MESSAGING_CHANNEL", "slack")
        return {"service": channel, "status": "error", "message": str(e)}


def check_github_token(filepath: str = MCP_TOKEN_FILE) -> dict:
    """Validate the GitHub token in the mcp-token file via `gh auth status`.

    `gh auth status` checks the session gh is actually logged in with.
    Returns service/status/message; never raises.
    """
    tokens = _parse_mcp_tokens(filepath)
    gh_data = tokens.get("Github", {})
    has_token = isinstance(gh_data, dict) and bool(gh_data.get("access_token"))

    if not has_token:
        return {
            "service": "github",
            "status": "missing",
            "message": "No GitHub token in mcp-token",
        }

    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return {"service": "github", "status": "error", "message": "gh not installed"}
    except subprocess.TimeoutExpired:
        return {
            "service": "github",
            "status": "error",
            "message": "gh auth status timed out",
        }
    except Exception as e:
        return {"service": "github", "status": "error", "message": str(e)}

    if result.returncode == 0:
        return {"service": "github", "status": "ok"}
    return {
        "service": "github",
        "status": "invalid",
        "message": (result.stderr or result.stdout).strip()[:200],
    }


def check_all(filepath: str = MCP_TOKEN_FILE) -> list[dict]:
    """Run all token checks and return their result dicts."""
    return [check_messaging_token(filepath), check_github_token(filepath)]


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Validate messaging channel and GitHub tokens"
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument(
        "--file",
        default=MCP_TOKEN_FILE,
        help=f"mcp-token path (default: {MCP_TOKEN_FILE})",
    )
    args = parser.parse_args()

    results = check_all(args.file)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        icons = {"ok": "✅", "missing": "⚪", "invalid": "❌", "error": "⚠️"}
        for r in results:
            icon = icons.get(r["status"], "❓")
            extra = r.get("message") or r.get("team", "")
            print(
                f"  {icon} {r['service']:8s} — {r['status']}"
                + (f" ({extra})" if extra else "")
            )

    # Exit non-zero if any token is invalid or missing.
    bad = any(r["status"] in ("invalid", "missing") for r in results)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
