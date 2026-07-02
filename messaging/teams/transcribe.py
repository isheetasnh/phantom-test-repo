#!/usr/bin/env python3
"""Transcribe a Microsoft Teams audio/voice message to text.

Downloads the audio file using the Microsoft Graph access token, then sends it
to the LiteLLM transcription endpoint via the OpenAI client. Prints the transcript.

Usage:
    python messaging/teams/transcribe.py <download_url>

Auth:
    Reads ``teams.access_token`` from ``~/.agent_settings.json`` (the Graph token
    persisted by install/teams.sh). The LiteLLM API key is read via
    ``clients.litellm_client.get_config()``.

Exit codes:
    0  — transcript printed to stdout
    1  — missing argument, download failure, or transcription failure
"""

import asyncio
import json
import mimetypes
import sys
from pathlib import Path

import requests
from clients.litellm_client import get_config, resolve_model
from messaging.teams.exceptions import TeamsAPIError, TeamsConfigError
from messaging.teams.graph_api import resolve_share_download_url
from openai import AsyncOpenAI, OpenAIError


async def transcribe(download_url: str) -> str:
    """Download ``download_url`` with Graph auth and return the transcript.

    Args:
        download_url: The file download URL from the Teams message attachment
            (``contentUrl`` / ``@microsoft.graph.downloadUrl``).

    Returns:
        Transcript text string.

    Raises:
        RuntimeError: If the download or transcription request fails.
    """
    # --- auth ---
    cfg = get_config()
    settings_path = Path.home() / ".agent_settings.json"
    try:
        settings = json.loads(settings_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read {settings_path}: {exc}") from exc

    access_token = (settings.get("teams") or {}).get("access_token", "")
    if not access_token:
        raise RuntimeError(
            f"No 'teams.access_token' found in {settings_path}. "
            "Run: python messaging/teams/interface.py config --set-access-token <token>"
        )

    # --- download audio ---
    # Teams files live in SharePoint, which a Graph token can't fetch directly
    # (wrong audience → 401). Resolve the attachment URL to a pre-authenticated
    # @microsoft.graph.downloadUrl via /shares; that link needs no auth header.
    try:
        resolved = resolve_share_download_url(download_url, access_token)
    except (TeamsAPIError, TeamsConfigError) as exc:
        raise RuntimeError(f"Could not resolve Teams share URL: {exc}") from exc

    audio_resp = requests.get(resolved, timeout=60)
    if not audio_resp.ok:
        raise RuntimeError(
            f"Audio download failed ({audio_resp.status_code}): {audio_resp.text[:200]}"
        )

    # Name the upload with an extension the transcription model accepts, from the
    # download response's content type (Teams voice notes are typically audio/mp4).
    content_type = (audio_resp.headers.get("Content-Type") or "audio/mp4").split(";")[
        0
    ].strip() or "audio/mp4"
    ext = mimetypes.guess_extension(content_type) or ".m4a"

    # --- transcribe ---
    client = AsyncOpenAI(
        api_key=cfg["api_key"], base_url=cfg["base_url"].rstrip("/") + "/v1"
    )
    transcript = await client.audio.transcriptions.create(
        model=resolve_model("ninja-transcribe"),
        file=(f"audio{ext}", audio_resp.content, content_type),
        extra_headers={
            "Authorization": f"Bearer {cfg['api_key']}",
        },
    )

    text = transcript.text or ""
    if not text:
        raise RuntimeError("Transcription returned empty text.")
    return text


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: transcribe.py <download_url>", file=sys.stderr)
        sys.exit(1)

    try:
        print(asyncio.run(transcribe(sys.argv[1])))
    except (RuntimeError, requests.RequestException, OpenAIError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
