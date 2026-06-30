#!/usr/bin/env python3
"""Transcribe a Slack audio/voice message to text.

Downloads the audio file using the Slack bot token, then sends it to the
LiteLLM transcription endpoint (Whisper). Prints the transcript to stdout.

Usage:
    python messaging/slack/transcribe.py <download_url>

Auth:
    Reads ``bot_token`` from ``~/.agent_settings.json``.
    The LiteLLM API key is read via ``clients.litellm_client.get_config()``.

Exit codes:
    0  — transcript printed to stdout
    1  — missing argument, download failure, or transcription failure
"""

import json
import sys
from pathlib import Path

import requests
from clients.litellm_client import api_url, get_config


def transcribe(download_url: str) -> str:
    """Download ``download_url`` with Slack auth and return the transcript.

    Args:
        download_url: The ``url_private_download`` value from the Slack file object.

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

    bot_token = settings.get("bot_token", "")
    if not bot_token:
        raise RuntimeError(
            f"No 'bot_token' found in {settings_path}. "
            "Run: python messaging/slack/interface.py config --set-channel <channel>"
        )

    # --- download audio ---
    audio_resp = requests.get(
        download_url,
        headers={"Authorization": f"Bearer {bot_token}"},
        timeout=60,
    )
    if not audio_resp.ok:
        raise RuntimeError(
            f"Audio download failed ({audio_resp.status_code}): {audio_resp.text[:200]}"
        )

    # --- transcribe ---
    transcription_resp = requests.post(
        api_url("/v1/audio/transcriptions"),
        headers={"Authorization": f"Bearer {cfg['api_key']}"},
        files={"file": ("audio.webm", audio_resp.content, "audio/webm")},
        data={"model": "whisper-1"},
        timeout=120,
    )
    if not transcription_resp.ok:
        raise RuntimeError(
            f"Transcription failed ({transcription_resp.status_code}): "
            f"{transcription_resp.text[:200]}"
        )

    text = transcription_resp.json().get("text", "")
    if not text:
        raise RuntimeError("Transcription returned empty text.")
    return text


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: transcribe.py <download_url>", file=sys.stderr)
        sys.exit(1)

    try:
        print(transcribe(sys.argv[1]))
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
