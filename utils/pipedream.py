"""
PipedreamClient — Pipedream Connect Integration Client
======================================================

HTTP client for the integrations gateway, which routes requests to
Pipedream Connect. Accessed via LiteLLM.

The gateway acts as a proxy between Phantom and Pipedream Connect —
all tool calls and direct API requests go through it, which then
forwards them to Pipedream's infrastructure for credential resolution
and upstream API execution.

Entry points:

    chat_with_tools(messages, model, ...)
        POST /v1/chat/completions with the ninja_integrations_gateway_user MCP server.
        LiteLLM fetches the tool list, passes it to the LLM, executes any
        tool calls, and returns the final response — all in one request.

    get_connection_link()
        GET the Pipedream Connect OAuth connection link for the user.
        No LLM involved. Returns a short-lived OAuth connection URL.

    check_health()
        GET /ninja/integrations-gateway/health — no auth required.
        Returns the parsed JSON response from the gateway health endpoint.

    list_accounts()
        GET /ninja/integrations-gateway/accounts — list connected apps for the user.

    list_apps(q, limit)
        GET /ninja/integrations-gateway/apps — browse the Pipedream app catalog.

    run_action(action_key, props)
        POST /ninja/integrations-gateway/actions/run — execute a Pipedream action.

    create_connect_token(app_slug)
        GET /ninja/integrations-gateway/connect-token — mint a short-lived Connect token.

Configuration is read automatically:
    - NINJA_USER_ID env var (or ~/.agent_settings.json ninja_user_id)
      → x-ninja-user-id header
    - ~/.agent_settings.json default_team_id + default_channel_id
      → x-ninja-integration-channel-id header
    - /root/.claude/settings.json (or local settings.json)
      → api_key and base_url (via clients.litellm_client.get_config)
    - /dev/shm/sandbox_metadata.json thread_id
      → x-ninja-conversation-id header (auto-populated, no override)

Usage::

    from utils.pipedream import PipedreamClient

    pdx = PipedreamClient()
    response = pdx.chat_with_tools(
        messages=[{"role": "user", "content": "What's on my calendar today?"}],
    )
    print(response)

    link = pdx.get_connection_link()
    print(link)

    accounts = pdx.list_accounts()
    print(accounts)
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from clients.litellm_client import get_config
from constants import (
    HEADER_NINJA_CONVERSATION_ID,
    HEADER_NINJA_EVENT_ID,
    HEADER_NINJA_SANDBOX_ID,
)
from core.config import config_cached
from messaging import get_messaging_interface

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MCP_USER_SERVER = "ninja_integrations_gateway_user"
_AGENT_SETTINGS_PATH = Path.home() / ".agent_settings.json"
_SANDBOX_METADATA_PATH = Path("/dev/shm/sandbox_metadata.json")
_PH_METADATA_PATH = Path("/dev/shm/ph_metadata.json")
_CONNECTION_LINK_PATH = "/ninja/integrations-gateway/get-integ-connection-ui-link"
_HEALTH_PATH = "/ninja/integrations-gateway/health"
_HTTP_PROXY_PATH = "/ninja/integrations-gateway/http"
_ACCOUNTS_PATH = "/ninja/integrations-gateway/accounts"
_APPS_PATH = "/ninja/integrations-gateway/apps"
_RUN_ACTION_PATH = "/ninja/integrations-gateway/actions/run"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@config_cached("pdx_agent_settings")
def _load_agent_settings() -> dict:
    """Return ~/.agent_settings.json as a dict, or {} on any error. Cached."""
    try:
        with open(_SANDBOX_METADATA_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


@config_cached("pdx_sandbox_metadata")
def _load_sandbox_metadata() -> dict:
    """Return thread_id from /dev/shm/sandbox_metadata.json, or {} if absent. Cached."""
    try:
        with open(_SANDBOX_METADATA_PATH) as f:
            data = json.load(f)
        thread_id = data.get("thread_id", "")
        return {"thread_id": thread_id} if thread_id else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


@config_cached("pdx_ph_metadata")
def _load_ph_metadata() -> dict:
    """Return sandbox_id from /dev/shm/ph_metadata.json, or {} if absent. Cached."""
    try:
        with open(_PH_METADATA_PATH) as f:
            data = json.load(f)
        sandbox_id = data.get("sandbox_id", "")
        return {"sandbox_id": sandbox_id} if sandbox_id else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _get_ninja_user_id() -> str:
    """
    Return the Ninja user UUID for x-ninja-user-id.

    Reads from NINJA_USER_ID env var first, then falls back to
    ninja_user_id in the cached ~/.agent_settings.json.

    Raises ValueError if neither is set.
    """
    value = os.environ.get("NINJA_USER_ID") or _load_agent_settings().get("user_id")
    if not value:
        raise ValueError(
            "NINJA_USER_ID is not set. "
            "Set it as an environment variable or as 'ninja_user_id' in ~/.agent_settings.json."
        )
    return value


def _get_unique_channel() -> str:
    """
    Derive x-ninja-integration-channel-id via the active messaging adapter.

    Delegates to the adapter's ``get_unique_channel()`` method so each
    channel type can derive a stable workspace-scoped identifier from
    its own identity fields.

    Raises ValueError if the adapter cannot resolve the identifier.
    """
    return get_messaging_interface().get_unique_channel()


def _http_post(url: str, headers: dict, body: dict) -> dict:
    """
    POST JSON body to url with headers.

    Returns the parsed JSON response dict.
    Raises PipedreamError for HTTP 4xx/5xx responses.
    """
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode(errors="replace")
        raise PipedreamError(exc.code, body_text) from exc


def _http_get(url: str) -> dict:
    """
    GET url with no auth headers.

    Returns the parsed JSON response dict.
    Raises PipedreamError for HTTP 4xx/5xx responses.
    """
    try:
        with urllib.request.urlopen(url) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode(errors="replace")
        raise PipedreamError(exc.code, body_text) from exc


def _http_get_authed(url: str, headers: dict) -> dict:
    """
    GET url with auth headers.

    Returns the parsed JSON response dict.
    Raises PipedreamError for HTTP 4xx/5xx responses.
    """
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode(errors="replace")
        raise PipedreamError(exc.code, body_text) from exc


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PipedreamError(Exception):
    """Raised when the gateway returns an error response."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"HTTP {status_code}: {message}")


# ---------------------------------------------------------------------------
# PipedreamClient
# ---------------------------------------------------------------------------


class PipedreamClient:
    """
    Client for the Pipedream Connect integrations gateway via LiteLLM.
    """

    def __init__(self) -> None:
        cfg = get_config()
        self._api_key = cfg["api_key"]
        self._base_url = cfg["base_url"].rstrip("/")

    # -- internal ------------------------------------------------------------

    def _base_headers(
        self,
        *,
        event_id: str | None = None,
    ) -> dict[str, str]:
        """Build the required + optional request headers."""
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "x-ninja-user-id": _get_ninja_user_id(),
            "x-ninja-integration-channel-id": _get_unique_channel(),
            "x-ninja-feature": "phantom",
        }
        thread_id = _load_sandbox_metadata().get("thread_id")
        if thread_id:
            headers[HEADER_NINJA_CONVERSATION_ID] = thread_id
        sandbox_id = _load_ph_metadata().get("sandbox_id")
        if sandbox_id:
            headers[HEADER_NINJA_SANDBOX_ID] = sandbox_id
        if event_id:
            headers[HEADER_NINJA_EVENT_ID] = event_id
        return headers

    # -- public API ----------------------------------------------------------

    def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        *,
        event_id: str | None = None,
    ) -> str:
        """
        Send a chat completion request with ninja_integrations_gateway_user tools enabled.

        LiteLLM handles the full tool-call loop internally:
        1. Fetches the tool list from the Pipedream Connect gateway
        2. Passes tools to the LLM
        3. Executes any tool calls and folds results back
        4. Returns the final LLM response

        Parameters
        ----------
        messages:
            OpenAI-format messages list, e.g.
            [{"role": "user", "content": "What's on my calendar?"}]
        model:
            LiteLLM model alias. Defaults to ANTHROPIC_MODEL from settings
            (via clients.litellm_client.get_config).
        event_id:
            Optional traceability header logged by the gateway.
            x-ninja-conversation-id and x-ninja-sandbox-id are auto-populated
            from sandbox metadata.

        Returns
        -------
        str
            The final LLM response text (choices[0].message.content).

        Raises
        ------
        PipedreamError
            On HTTP 400 (missing headers), 403 (bad API key), 502 (gateway down).
        ValueError
            If NINJA_USER_ID or unique_channel cannot be resolved.
        """
        url = f"{self._base_url}/v1/chat/completions"
        resolved_model = model if model is not None else get_config()["default_model"]
        headers = self._base_headers(event_id=event_id)
        body: dict[str, Any] = {
            "model": resolved_model,
            "messages": messages,
            "tools": [
                {
                    "type": "mcp",
                    "server_url": f"litellm_proxy/mcp/{_MCP_USER_SERVER}",
                    "server_label": _MCP_USER_SERVER,
                    "require_approval": "never",
                    "headers": {
                        "x-ninja-user-id": headers["x-ninja-user-id"],
                        "x-ninja-integration-channel-id": headers[
                            "x-ninja-integration-channel-id"
                        ],
                    },
                }
            ],
        }
        response = _http_post(url, headers, body)
        return response["choices"][0]["message"]["content"]

    def get_connection_link(
        self,
        *,
        event_id: str | None = None,
    ) -> str:
        """
        Get a short-lived Pipedream Connect OAuth connection link for the user.

        No LLM involved. User identity comes from the verified headers.
        The link expires after 30 minutes. Post it to the user in chat.

        Returns
        -------
        str
            The connection URL, e.g.
            "https://integrations-gateway.beta.myninja.ai/connections?..."

        Raises
        ------
        PipedreamError
            On HTTP 400 (missing x-ninja-integration-channel-id),
            403 (API key has no ninja_user_id), or 502 (gateway down).
        ValueError
            If NINJA_USER_ID or unique_channel cannot be resolved.
        """
        url = f"{self._base_url}{_CONNECTION_LINK_PATH}"
        headers = self._base_headers(event_id=event_id)
        response = _http_post(url, headers, body={})
        try:
            return response["link"]
        except KeyError as exc:
            raise PipedreamError(
                0,
                f"Unexpected get_connection_link response — no 'link' key: {response!r}",
            ) from exc

    def http_request(
        self,
        app_slug: str,
        method: str,
        url: str,
        *,
        json_body: dict | None = None,
        raw_body: str | None = None,
        extra_headers: dict[str, str] | None = None,
        query: dict[str, str] | None = None,
        event_id: str | None = None,
    ) -> dict:
        """
        Make a raw authenticated HTTP request through the Pipedream Connect proxy.

        The gateway uses x-ninja-user-id + x-ninja-integration-channel-id to resolve
        the user's Pipedream credentials for app_slug and proxies the request upstream.

        Parameters
        ----------
        app_slug:
            The integration app slug (e.g. ``"github"``, ``"gmail"``).
        method:
            HTTP method (``"GET"``, ``"POST"``, ``"PUT"``, ``"PATCH"``, ``"DELETE"``).
        url:
            Upstream URL (e.g. ``"https://api.github.com/user"``).
        json_body:
            Optional JSON-serialisable dict to send as the request body.
            Mutually exclusive with ``raw_body``.
        raw_body:
            Optional raw string body. Mutually exclusive with ``json_body``.
        extra_headers:
            Optional dict of additional headers to forward upstream.
        query:
            Optional dict of query-string parameters to append to the URL.
        event_id:
            Optional traceability header logged by the gateway.
            x-ninja-conversation-id and x-ninja-sandbox-id are auto-populated
            from sandbox metadata.

        Returns
        -------
        dict
            The full response dict from the gateway
            (expected keys: ``status``, ``headers``, ``body``).

        Raises
        ------
        PipedreamError
            On HTTP 400 (missing headers), 403 (bad API key), 502 (gateway down).
        ValueError
            If NINJA_USER_ID or unique_channel cannot be resolved.
        """
        endpoint = f"{self._base_url}{_HTTP_PROXY_PATH}"
        headers = self._base_headers(event_id=event_id)
        body: dict[str, Any] = {
            "app_slug": app_slug,
            "method": method.upper(),
            "url": url,
        }
        if json_body is not None:
            body["json"] = json_body
        elif raw_body is not None:
            body["data"] = raw_body
        if extra_headers:
            body["headers"] = extra_headers
        if query:
            body["query"] = query

        return _http_post(endpoint, headers, body)

    def list_accounts(self) -> list[dict[str, Any]]:
        """
        List connected Pipedream accounts for the user.

        Returns all integrations the user has onboarded, grouped by app slug
        on the gateway side.

        Returns
        -------
        list[dict]
            List of account dicts with keys: ``id``, ``app_slug``, ``app_name``,
            ``healthy``, ``created_at``, ``updated_at``.

        Raises
        ------
        PipedreamError
            On HTTP 4xx/5xx from the gateway.
        ValueError
            If NINJA_USER_ID or unique_channel cannot be resolved.
        """
        url = f"{self._base_url}{_ACCOUNTS_PATH}"
        headers = self._base_headers()
        response = _http_get_authed(url, headers)
        return response.get("accounts", [])

    def list_apps(
        self,
        *,
        q: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """
        Browse the Pipedream app catalog.

        Parameters
        ----------
        q:
            Optional search query (e.g. ``"github"``, ``"google sheets"``).
        limit:
            Maximum number of results to return (default 50).

        Returns
        -------
        list[dict]
            List of app dicts with keys: ``name_slug``, ``name``, ``description``,
            ``auth_type``, ``categories``.

        Raises
        ------
        PipedreamError
            On HTTP 4xx/5xx from the gateway.
        ValueError
            If NINJA_USER_ID or unique_channel cannot be resolved.
        """
        params: dict[str, str] = {"limit": str(limit)}
        if q:
            params["q"] = q
        query_string = urllib.parse.urlencode(params)
        url = f"{self._base_url}{_APPS_PATH}?{query_string}"
        headers = self._base_headers()
        response = _http_get_authed(url, headers)
        return response.get("apps", [])

    def run_action(
        self,
        action_key: str,
        *,
        props: dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Execute a Pipedream action on behalf of the user.

        Parameters
        ----------
        action_key:
            The component key, e.g. ``"github-create-issue"``.
        props:
            Dict of prop name → value to pass to the action.
        event_id:
            Optional traceability header logged by the gateway.

        Returns
        -------
        dict
            The action run result from the gateway (key: ``result``).

        Raises
        ------
        PipedreamError
            On HTTP 4xx/5xx from the gateway.
        ValueError
            If NINJA_USER_ID or unique_channel cannot be resolved.
        """
        url = f"{self._base_url}{_RUN_ACTION_PATH}"
        headers = self._base_headers(event_id=event_id)
        body: dict[str, Any] = {"action_key": action_key, "props": props or {}}
        return _http_post(url, headers, body)

    def check_health(self) -> dict:
        """
        GET /ninja/integrations-gateway/health — no auth required.

        Returns the parsed JSON response from the Pipedream Connect
        gateway health endpoint.
        """
        url = f"{self._base_url}{_HEALTH_PATH}"
        return _http_get(url)
