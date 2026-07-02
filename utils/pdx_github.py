"""
utils/pdx_github.py — Pipedream GitHub Registry Client
=======================================================

Reads action metadata directly from the PipedreamHQ/pipedream GitHub
repository — no gateway involved, no auth required.

Used by ``pdx actions``, ``pdx describe``, and ``pdx tools`` to enumerate
and describe Pipedream component actions and emit OpenAI-style tool schemas.

Results are cached in-process for 1 hour to avoid hammering the GitHub API.

Public API
----------
    list_actions_for_app(app_slug)
        Return a list of action summaries for an app from the GitHub registry.

    describe_action(action_key)
        Return the full JSON-schema-ish props dict for a specific action key.

    action_to_openai_tool(schema)
        Convert an action schema dict to an OpenAI function-calling tool entry.

    APP_SLUG_TO_GH
        Dict mapping Pipedream app slugs to their GitHub component folder names.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# GitHub endpoints
# ---------------------------------------------------------------------------

_GH_API = "https://api.github.com/repos/PipedreamHQ/pipedream/contents/components"
_GH_RAW = "https://raw.githubusercontent.com/PipedreamHQ/pipedream/master/components"

# ---------------------------------------------------------------------------
# App slug → GitHub component folder map
# ---------------------------------------------------------------------------

APP_SLUG_TO_GH: Dict[str, str] = {
    "slack_v2": "slack_v2",
    "slack_bot": "slack_bot",
    "github": "github",
    "gitlab": "gitlab",
    "google_sheets": "google_sheets",
    "google_drive": "google_drive",
    "google_calendar": "google_calendar",
    "gmail": "gmail",
    "notion": "notion",
    "hubspot": "hubspot",
    "salesforce_rest_api": "salesforce_rest_api",
    "openai": "openai",
    "anthropic": "anthropic",
    "telegram_bot_api": "telegram_bot_api",
    "linear_app": "linear_app",
    "jira": "jira",
    "zendesk": "zendesk",
    "discord_bot": "discord_bot",
    "discord": "discord",
    "stripe": "stripe",
    "twilio": "twilio",
    "airtable_oauth": "airtable_oauth",
    "dropbox": "dropbox",
    "asana": "asana",
    "trello": "trello",
    "monday": "monday",
    "mysql": "mysql",
    "postgresql": "postgresql",
    "mongodb": "mongodb",
    "aws": "aws",
    "sendgrid": "sendgrid",
    "zoom": "zoom",
    "microsoft_teams": "microsoft_teams",
    "outlook": "outlook",
    "calendly": "calendly",
    "typeform": "typeform",
    "google_forms": "google_forms",
    "supabase": "supabase",
    "pinecone": "pinecone",
    "shopify_developer_app": "shopify_developer_app",
}

# ---------------------------------------------------------------------------
# In-process cache (TTL = 1 hour)
# ---------------------------------------------------------------------------

_cache: Dict[str, Any] = {}
_CACHE_TTL = 3600  # seconds

# ---------------------------------------------------------------------------
# Low-level GitHub HTTP helpers
# ---------------------------------------------------------------------------


def _gh_get(url: str, timeout: int = 8) -> Any:
    """GET a GitHub API URL and return the parsed JSON response."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "pdx/1.0", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _gh_raw(url: str, timeout: int = 5) -> str:
    """GET a raw GitHub content URL and return the decoded text."""
    req = urllib.request.Request(url, headers={"User-Agent": "pdx/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")


def _slug_to_title(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.replace("-", " ").split())


# ---------------------------------------------------------------------------
# .mjs parser — extracts action metadata and prop schema from component source
# ---------------------------------------------------------------------------

_RE_NAME = re.compile(r'\bname:\s*["`\'](.*?)["`\']', re.DOTALL)
_RE_DESC = re.compile(r'\bdescription:\s*["`\'](.*?)["`\']', re.DOTALL)
_RE_VER = re.compile(r'\bversion:\s*["`\']([\d.]+)["`\']')
_RE_KEY = re.compile(r'\bkey:\s*["`\'](.*?)["`\']')


def _parse_action_meta(mjs: str) -> Dict[str, str]:
    n = _RE_NAME.search(mjs)
    d = _RE_DESC.search(mjs)
    v = _RE_VER.search(mjs)
    k = _RE_KEY.search(mjs)
    return {
        "key": k.group(1) if k else "",
        "name": n.group(1) if n else "",
        "description": (d.group(1) if d else "").replace("\\n", " ").strip(),
        "version": v.group(1) if v else "",
    }


def _extract_props_block(mjs: str) -> Optional[str]:
    """Return the raw text inside the top-level `props: { ... }` block."""
    i = mjs.find("props:")
    if i < 0:
        return None
    start = mjs.find("{", i)
    if start < 0:
        return None
    depth = 0
    for j in range(start, len(mjs)):
        c = mjs[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return mjs[start + 1 : j]
    return None


_PROP_TYPE_MAP = {
    "string": "string",
    "string[]": "array",
    "integer": "integer",
    "integer[]": "array",
    "boolean": "boolean",
    "object": "object",
    "any": "string",
    "app": "string",
    "$.interface.http": "string",
    "$.service.db": "object",
}


def _parse_props(props_block: str) -> Dict[str, Dict[str, Any]]:
    props: Dict[str, Dict[str, Any]] = {}
    depth = 0
    i = 0
    n = len(props_block)
    entries: List[str] = []
    entry_start = 0
    while i < n:
        c = props_block[i]
        if c in "{[(":
            depth += 1
        elif c in "}])":
            depth -= 1
        elif c == "," and depth == 0:
            entries.append(props_block[entry_start:i])
            entry_start = i + 1
        i += 1
    tail = props_block[entry_start:].strip()
    if tail:
        entries.append(tail)

    for raw in entries:
        raw = raw.strip().rstrip(",").strip()
        if not raw:
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*\{(.*)\}\s*$", raw, re.DOTALL)
        if m:
            pname = m.group(1)
            body = m.group(2)
            t_match = re.search(r'\btype:\s*["`\'](.*?)["`\']', body)
            label_m = re.search(r'\blabel:\s*["`\'](.*?)["`\']', body, re.DOTALL)
            desc_m = re.search(r'\bdescription:\s*["`\'](.*?)["`\']', body, re.DOTALL)
            opt_m = re.search(r"\boptional:\s*(true|false)", body)
            default_m = re.search(
                r'\bdefault:\s*("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|\d+|true|false)',
                body,
            )
            is_ref = "propDefinition" in body
            raw_type = t_match.group(1) if t_match else "string"
            mapped = _PROP_TYPE_MAP.get(raw_type, "string")
            entry: Dict[str, Any] = {"type": mapped, "raw_type": raw_type}
            if label_m:
                entry["label"] = label_m.group(1)
            if desc_m:
                entry["description"] = desc_m.group(1).replace("\\n", " ").strip()[:300]
            if opt_m:
                entry["required"] = opt_m.group(1) == "false"
            else:
                entry["required"] = not is_ref
            if default_m:
                try:
                    entry["default"] = json.loads(default_m.group(1).replace("'", '"'))
                except Exception:
                    entry["default"] = default_m.group(1).strip("\"'")
            if is_ref:
                entry["propDefinition"] = True
            props[pname] = entry
            continue
        m2 = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)$", raw)
        if m2:
            props[m2.group(1)] = {
                "type": "string",
                "raw_type": "app",
                "appReference": True,
                "required": True,
                "description": f"Connected {m2.group(1)} account (auto-filled by Pipedream).",
            }
    return props


def _action_schema_from_mjs(
    app_slug: str, action_slug: str, mjs: str
) -> Dict[str, Any]:
    meta = _parse_action_meta(mjs)
    props_block = _extract_props_block(mjs) or ""
    props = _parse_props(props_block)
    public_props = {k: v for k, v in props.items() if not v.get("appReference")}
    return {
        "ok": True,
        "app_slug": app_slug,
        "action_slug": action_slug,
        "key": meta["key"] or f"{app_slug}-{action_slug}",
        "name": meta["name"] or _slug_to_title(action_slug),
        "description": meta["description"],
        "version": meta["version"],
        "props": public_props,
        "app_refs": [k for k, v in props.items() if v.get("appReference")],
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_actions_for_app(app_slug: str) -> List[Dict[str, Any]]:
    """
    Return a list of action summaries for ``app_slug`` from the GitHub registry.

    Results are cached for ``_CACHE_TTL`` seconds.

    Parameters
    ----------
    app_slug:
        Pipedream app slug (e.g. ``"github"``, ``"gmail"``).

    Returns
    -------
    list[dict]
        Each item has keys: ``key``, ``slug``, ``app_slug``, ``name``,
        ``description``, ``version``.

    Raises
    ------
    RuntimeError
        If the GitHub registry for ``app_slug`` cannot be fetched.
    """
    ck = ("actions", app_slug)
    hit = _cache.get(ck)
    if hit and time.time() - hit["ts"] < _CACHE_TTL:
        return hit["data"]

    folder = APP_SLUG_TO_GH.get(app_slug, app_slug)
    url = f"{_GH_API}/{folder}/actions"
    try:
        dirs = _gh_get(url)
    except Exception as e:
        raise RuntimeError(f"No actions registry found for '{app_slug}' ({e})")

    out: List[Dict[str, Any]] = []
    for d in dirs:
        if d.get("type") != "dir" or d["name"].startswith("common"):
            continue
        slug = d["name"]
        try:
            mjs = _gh_raw(f"{_GH_RAW}/{folder}/actions/{slug}/{slug}.mjs")
        except Exception:
            mjs = ""
        meta = _parse_action_meta(mjs) if mjs else {}
        out.append(
            {
                "key": meta.get("key") or f"{folder}-{slug}",
                "slug": slug,
                "app_slug": app_slug,
                "name": meta.get("name") or _slug_to_title(slug),
                "description": (meta.get("description") or "")[:200],
                "version": meta.get("version", ""),
            }
        )

    _cache[ck] = {"data": out, "ts": time.time()}
    return out


def describe_action(action_key: str) -> Dict[str, Any]:
    """
    Return the full JSON-schema-ish props dict for ``action_key``.

    The key format is ``<app_slug>-<action_slug>`` (e.g. ``"github-create-issue"``).
    Multiple app/action prefix splits are tried (longest first) to resolve the
    correct GitHub component path.

    Results are cached for ``_CACHE_TTL`` seconds.

    Parameters
    ----------
    action_key:
        Pipedream component key (e.g. ``"github-create-issue"``).

    Returns
    -------
    dict
        Full schema with keys: ``ok``, ``app_slug``, ``action_slug``, ``key``,
        ``name``, ``description``, ``version``, ``props``, ``app_refs``.

    Raises
    ------
    RuntimeError
        If the action cannot be resolved from GitHub.
    """
    ck = ("describe", action_key)
    hit = _cache.get(ck)
    if hit and time.time() - hit["ts"] < _CACHE_TTL:
        return hit["data"]

    parts = action_key.split("-")
    candidates: List[tuple] = []
    for i in range(len(parts) - 1, 0, -1):
        app = "-".join(parts[:i])
        act = "-".join(parts[i:])
        candidates.append((app, act))
        app_u = "_".join(parts[:i])
        if app_u != app:
            candidates.append((app_u, act))

    last_err = None
    for app_slug, action_slug in candidates:
        folder = APP_SLUG_TO_GH.get(app_slug, app_slug)
        url = f"{_GH_RAW}/{folder}/actions/{action_slug}/{action_slug}.mjs"
        try:
            mjs = _gh_raw(url)
            schema = _action_schema_from_mjs(app_slug, action_slug, mjs)
            _cache[ck] = {"data": schema, "ts": time.time()}
            return schema
        except Exception as e:
            last_err = e
            continue

    raise RuntimeError(
        f"Could not resolve action '{action_key}' "
        f"(tried {len(candidates)} app/action splits). Last error: {last_err}"
    )


def _props_to_json_schema(props: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    properties: Dict[str, Any] = {}
    required: List[str] = []
    for name, p in props.items():
        entry: Dict[str, Any] = {"type": p.get("type", "string")}
        if p.get("description"):
            entry["description"] = p["description"]
        elif p.get("label"):
            entry["description"] = p["label"]
        if "default" in p:
            entry["default"] = p["default"]
        if p.get("type") == "array":
            entry["items"] = {"type": "string"}
        properties[name] = entry
        if p.get("required"):
            required.append(name)
    schema: Dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def action_to_openai_tool(schema: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert an action schema dict to an OpenAI function-calling tool entry.

    Parameters
    ----------
    schema:
        Full action schema as returned by ``describe_action()``.

    Returns
    -------
    dict
        OpenAI-style ``{"type": "function", "function": {...}}`` entry suitable
        for passing directly to ``tools=[...]`` in an LLM API call.
    """
    fn_name = schema["key"].replace(".", "_")[:64]
    desc = schema.get("description") or schema.get("name") or schema["key"]
    return {
        "type": "function",
        "function": {
            "name": fn_name,
            "description": desc[:1000],
            "parameters": _props_to_json_schema(schema.get("props", {})),
        },
    }
