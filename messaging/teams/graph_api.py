import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from messaging.teams.exceptions import TeamsAPIError, TeamsConfigError
from messaging.teams.utils import (
    _drive_id_from_item,
    _guess_content_type,
    _safe_upload_name,
    _str_or_none,
    format_teams_message,
    normalize_reaction_type,
)

GRAPH_TRANSIENT_STATUSES = frozenset({0, 429, 502, 503, 504})
GRAPH_MAX_RETRIES = int(os.environ.get("MICROSOFT_GRAPH_MAX_RETRIES", "3"))
GRAPH_RETRY_BACKOFF = float(os.environ.get("MICROSOFT_GRAPH_RETRY_BACKOFF", "1.5"))
GRAPH_BASE_URL = os.environ.get(
    "MICROSOFT_GRAPH_BASE_URL", "https://graph.microsoft.com/v1.0"
)


def _should_retry_graph(method: str, status: int, attempt: int) -> bool:
    """Decide whether a transient Graph failure is worth retrying.

    Idempotent reads (GET/HEAD) are retried for any transient status. For
    non-idempotent verbs we only retry on 429, which means the request was
    throttled and never processed, so a retry cannot double-apply the write.
    """
    if attempt >= GRAPH_MAX_RETRIES:
        return False
    if status not in GRAPH_TRANSIENT_STATUSES:
        return False
    if method.upper() in ("GET", "HEAD"):
        return True
    return status == 429


def _graph_retry_delay(headers: dict[str, str], attempt: int) -> float:
    """Backoff for the next retry, honoring a Retry-After header when present."""
    retry_after = headers.get("Retry-After") or headers.get("retry-after")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            pass
    return GRAPH_RETRY_BACKOFF * (2**attempt)


def _decode_json(raw: bytes) -> Any:
    if not raw:
        return {}
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _ensure_ok(status: int, payload: Any) -> Any:
    if 200 <= status < 300:
        return payload
    raise TeamsAPIError(status, payload)


def _message_body(message: str, *, is_html: bool = False) -> dict[str, Any]:
    return {
        "body": {
            "contentType": "html",
            "content": format_teams_message(message, is_html=is_html),
        }
    }


def _quote(value: Optional[str]) -> str:
    if not value:
        raise TeamsConfigError("missing Teams destination value")
    return urllib.parse.quote(value.strip(), safe="")


def _graph_url(path_or_url: str, query: Optional[dict[str, Any]] = None) -> str:
    """Resolve a Graph path (or absolute URL) and append an optional query."""
    if path_or_url.startswith("https://"):
        url = path_or_url
    else:
        url = f"{GRAPH_BASE_URL.rstrip('/')}/{path_or_url.lstrip('/')}"
    if query:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urllib.parse.urlencode(query)}"
    return url


def _graph_request(
    method: str,
    path_or_url: str,
    *,
    token: str,
    data: Optional[bytes] = None,
    content_type: Optional[str] = None,
    query: Optional[dict[str, Any]] = None,
    timeout: float = 20.0,
) -> tuple[int, bytes, dict[str, str]]:
    """Shared core: build URL, send, retry on transient failures, return raw."""
    url = _graph_url(path_or_url, query)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = content_type

    attempt = 0
    while True:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as e:
            status, raw, resp_headers = e.code, e.read(), dict(e.headers or {})
        except urllib.error.URLError as e:
            status, raw, resp_headers = (
                0,
                json.dumps(
                    {"error": "connection_failed", "detail": str(e.reason)}
                ).encode("utf-8"),
                {},
            )

        if not _should_retry_graph(method, status, attempt):
            return status, raw, resp_headers

        delay = _graph_retry_delay(resp_headers, attempt)
        print(
            f"Microsoft Graph {method} {path_or_url} returned {status}; "
            f"retrying in {delay:.1f}s "
            f"(attempt {attempt + 1}/{GRAPH_MAX_RETRIES})",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(delay)
        attempt += 1


def make_graph_api_request(
    method: str,
    path_or_url: str,
    *,
    token: str,
    body: Optional[dict[str, Any]] = None,
    query: Optional[dict[str, Any]] = None,
    timeout: float = 20.0,
) -> tuple[int, Any, dict[str, str]]:
    """Send an optional JSON body and decode a JSON response."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    content_type = "application/json" if body is not None else None
    status, raw, headers = _graph_request(
        method,
        path_or_url,
        token=token,
        data=data,
        content_type=content_type,
        query=query,
        timeout=timeout,
    )
    return status, _decode_json(raw), headers


def make_graph_api_bytes_request(
    method: str,
    path_or_url: str,
    *,
    token: str,
    data: bytes,
    content_type: str = "application/octet-stream",
    timeout: float = 60.0,
) -> tuple[int, Any, dict[str, str]]:
    """Send a raw byte body (e.g. PUT file content) and decode a JSON response."""
    status, raw, headers = _graph_request(
        method,
        path_or_url,
        token=token,
        data=data,
        content_type=content_type or "application/octet-stream",
        timeout=timeout,
    )
    return status, _decode_json(raw), headers


def upload_bytes_to_channel(
    filename: str,
    content: bytes,
    *,
    team_id: str,
    channel_id: str,
    token: str,
    content_type: Optional[str] = None,
) -> dict[str, Any]:
    folder = get_channel_files_folder(team_id, channel_id, token)
    drive_id = _drive_id_from_item(folder)
    folder_id = _str_or_none(folder.get("id")) if isinstance(folder, dict) else None
    if not (drive_id and folder_id):
        raise TeamsAPIError(200, folder)

    upload_name = _safe_upload_name(filename)
    upload_path = (
        f"/drives/{_quote(str(drive_id))}/items/{_quote(str(folder_id))}:/"
        f"{urllib.parse.quote(upload_name, safe='')}:/content"
    )
    status, payload, _ = make_graph_api_bytes_request(
        "PUT",
        upload_path,
        token=token,
        data=content,
        content_type=_guess_content_type(upload_name, content_type),
    )
    return _ensure_ok(status, payload)


def get_channel_files_folder(team_id, channel_id, token) -> dict[str, Any]:
    status, payload, _ = make_graph_api_request(
        "GET",
        f"/teams/{_quote(team_id)}/channels/{_quote(channel_id)}/filesFolder",
        token=token,
    )
    return _ensure_ok(status, payload)


def set_reaction(
    team_id: str,
    channel_id: str,
    message_id: str,
    token: str,
    *,
    reaction_type: str = "✅",
    reply_to_id: Optional[str] = None,
) -> dict[str, Any]:
    """Add an emoji reaction to a channel message (or a threaded reply)."""
    if reply_to_id and reply_to_id != message_id:
        path = (
            f"/teams/{_quote(team_id)}/channels/{_quote(channel_id)}"
            f"/messages/{_quote(reply_to_id)}/replies/{_quote(message_id)}/setReaction"
        )
    else:
        path = (
            f"/teams/{_quote(team_id)}/channels/{_quote(channel_id)}"
            f"/messages/{_quote(message_id)}/setReaction"
        )
    reaction = normalize_reaction_type(reaction_type)
    status, payload, _ = make_graph_api_request(
        "POST",
        path,
        token=token,
        body={"reactionType": reaction},
    )
    _ensure_ok(status, payload)
    return {
        "ok": True,
        "message_id": message_id,
        "reply_to_id": reply_to_id,
        "reaction_type": reaction,
    }
