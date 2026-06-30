"""
core.config — shared config cache with SIGHUP-triggered refresh.

Usage
-----
Register a loader once per module:

    from core.config import config_cached

    @config_cached("slack")
    def _load_slack_config() -> dict:
        with open(Path.home() / ".agent_settings.json") as f:
            return json.load(f)

    # Call it anywhere — result is cached until refresh_config() is called.
    cfg = _load_slack_config()

Trigger a full refresh (e.g. from a test):

    from core.config import refresh_config
    refresh_config()          # invalidate all caches
    refresh_config("slack")   # invalidate only the "slack" cache

SIGHUP wiring
-------------
Call install_sighup_handler() once at process startup to make SIGHUP
automatically invalidate all caches:

    from core.config import install_sighup_handler
    install_sighup_handler()
"""

from __future__ import annotations

import functools
import json
import logging
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional, TypeVar

_logger = logging.getLogger(__name__)

_SENTINEL = object()
_registry: Dict[str, dict] = {}  # {"name": {"fn": callable, "value": Any}}
_lock = threading.Lock()
_sighup_installed = False

F = TypeVar("F", bound=Callable[..., Any])


def config_cached(name: str) -> Callable[[F], F]:
    """
    Decorator that caches the return value of a zero-argument loader function.

    The result is kept in memory until ``refresh_config(name)`` or
    ``refresh_config()`` is called.

    Args:
        name: Unique key for this config entry (used by refresh_config).

    Example:
        @config_cached("slack")
        def load_slack_config() -> dict:
            ...
    """

    def decorator(fn: F) -> F:
        with _lock:
            _registry[name] = {"fn": fn, "value": _SENTINEL}

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with _lock:
                entry = _registry.get(name)
                if entry is None:
                    return fn(*args, **kwargs)
                if entry["value"] is _SENTINEL:
                    entry["value"] = entry["fn"]()
                    _logger.debug("config_cached[%s]: loaded", name)
                return entry["value"]

        return wrapper  # type: ignore[return-value]

    return decorator


def refresh_config(name: Optional[str] = None) -> None:
    """
    Invalidate one or all cached config entries.

    On the next call to the decorated loader the value will be re-loaded
    from its source.

    Args:
        name: If given, invalidate only this entry. If None, invalidate all.
    """
    with _lock:
        if name is not None:
            entry = _registry.get(name)
            if entry is not None:
                entry["value"] = _SENTINEL
                _logger.debug("config_cached[%s]: invalidated", name)
            else:
                _logger.warning("refresh_config: unknown key %r", name)
        else:
            for key, entry in _registry.items():
                entry["value"] = _SENTINEL
            _logger.debug("config_cached: all entries invalidated (%d)", len(_registry))


def install_sighup_handler() -> None:
    """
    Install a SIGHUP handler that calls refresh_config() on receipt.

    Safe to call multiple times — only the first call installs the handler.
    """
    global _sighup_installed
    if _sighup_installed:
        return

    def _handler(signum: int, frame: Any) -> None:
        _logger.info("SIGHUP received — refreshing all config caches")
        refresh_config()

    signal.signal(signal.SIGHUP, _handler)
    _sighup_installed = True
    _logger.debug("install_sighup_handler: installed")


# ---------------------------------------------------------------------------
# Agent config
# ---------------------------------------------------------------------------

_AGENT_CONFIG_PATH = Path.home() / ".agent_settings.json"


@config_cached("agent_config")
def load_agent_config() -> dict:
    """Load agent configuration from ~/.agent_settings.json.

    Result is cached until refresh_config() or a SIGHUP is received.
    Returns an empty dict if the file is missing or unreadable.
    """
    try:
        if _AGENT_CONFIG_PATH.exists():
            return json.loads(_AGENT_CONFIG_PATH.read_text())
    except Exception as e:
        print(f"⚠️ Warning: Could not read config: {e}", file=sys.stderr)
    return {}


# ---------------------------------------------------------------------------
# Monitor state I/O
# ---------------------------------------------------------------------------
# These helpers persist the monitor's seen-message dedup state and agent
# thread-tracking state to disk. They are NOT cached (state changes every
# poll cycle) — but they live here alongside load_agent_config() so all
# file-based state I/O is in one place.


def _get_repo_root() -> Path:
    """Return the repo root (src/ninja/) relative to this file."""
    return Path(__file__).parent.parent


def load_seen_messages() -> set:
    """Load previously seen message IDs from .seen_messages.json."""
    path = _get_repo_root() / ".seen_messages.json"
    try:
        if path.exists():
            data = json.loads(path.read_text())
            return set(data.get("seen", []))
    except Exception:
        pass
    return set()


def save_seen_messages(seen: set) -> None:
    """Persist seen message IDs (keeps last 100 to bound file size)."""
    path = _get_repo_root() / ".seen_messages.json"
    try:
        recent = sorted(seen)[-100:]
        path.write_text(json.dumps({"seen": recent}))
    except Exception as e:
        print(f"⚠️ Warning: Could not save seen messages: {e}", file=sys.stderr)


def load_agent_messages() -> dict:
    """Load agent thread-tracking state from .agent_messages.json."""
    path = _get_repo_root() / ".agent_messages.json"
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return {"messages": [], "seen_replies": []}


def save_agent_messages(data: dict) -> None:
    """Persist agent thread-tracking state (bounded to prevent unbounded growth)."""
    path = _get_repo_root() / ".agent_messages.json"
    try:
        data["messages"] = data.get("messages", [])[-20:]
        data["seen_replies"] = data.get("seen_replies", [])[-100:]
        path.write_text(json.dumps(data))
    except Exception as e:
        print(f"⚠️ Warning: Could not save agent messages: {e}", file=sys.stderr)
