"""Shared message processing utilities across all messaging adapters."""

from typing import Any, Dict


def is_bot_message(message: Dict[str, Any]) -> bool:
    """True if message was posted by an application (bot), not a user.

    Works for both Teams (from_application_id) and Slack (bot_profile).
    """
    # Teams check
    if message.get("from_application_id"):
        return True
    # Slack check
    if message.get("bot_profile"):
        return True
    return False


def extract_file_attachments(message: Dict[str, Any]) -> Dict[str, list]:
    """Categorize a normalized message's attachments by type.

    Returns a dict with keys: audio_files, image_files, pdf_files, other_files.
    Each entry exposes name, mimetype, size, and url.
    """
    audio_files, image_files, pdf_files, other_files = [], [], [], []
    for f in message.get("files") or []:
        content_type = (f.get("content_type") or "").lower()
        entry = {
            "name": f.get("name") or "unknown",
            "mimetype": content_type,
            "size": f.get("size") or 0,
            "url": f.get("content_url") or f.get("web_url") or "",
        }
        if content_type.startswith("audio/"):
            audio_files.append(entry)
        elif content_type.startswith("image/"):
            image_files.append(entry)
        elif content_type == "application/pdf":
            pdf_files.append(entry)
        elif entry["name"] != "unknown" or entry["url"]:
            other_files.append(entry)
    return {
        "audio_files": audio_files,
        "image_files": image_files,
        "pdf_files": pdf_files,
        "other_files": other_files,
    }


def classify_message_type(attachments: Dict[str, list], is_reply: bool) -> str:
    """Derive a message type from attachments + position (attachment wins)."""
    if attachments["audio_files"]:
        return "audio_message"
    if (
        attachments["image_files"]
        or attachments["pdf_files"]
        or attachments["other_files"]
    ):
        return "file_message"
    return "thread_reply" if is_reply else "mention"
