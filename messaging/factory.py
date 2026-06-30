"""
Messaging factory — picks one channel adapter from config.

Lazy-imports the selected channel so unused adapters are never loaded.
The active channel is resolved from the MESSAGING_CHANNEL environment
variable (default: "slack").

Usage:
    from messaging.factory import get_messaging_interface

    iface = get_messaging_interface()   # returns MessagingInterface
    iface.say("Hello!")
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from messaging.base import MessagingInterface

_CHANNEL_ENV = "MESSAGING_CHANNEL"
_DEFAULT_CHANNEL = "slack"

_SUPPORTED_CHANNELS = ("slack", "whatsapp", "teams")


def get_messaging_interface(channel: str | None = None) -> "MessagingInterface":
    """
    Return an initialised MessagingInterface for the active channel.

    Args:
        channel: Override the channel name. Falls back to the
                 MESSAGING_CHANNEL env-var, then "slack".

    Returns:
        A concrete MessagingInterface instance.

    Raises:
        ValueError: If the requested channel is not supported.
    """
    resolved = channel or os.environ.get(_CHANNEL_ENV, _DEFAULT_CHANNEL)

    if resolved not in _SUPPORTED_CHANNELS:
        raise ValueError(
            f"Unsupported messaging channel: {resolved!r}. "
            f"Choose from: {', '.join(_SUPPORTED_CHANNELS)}"
        )

    if resolved == "slack":
        from messaging.slack.interface import SlackInterface

        return SlackInterface()

    if resolved == "whatsapp":
        from messaging.whatsapp.interface import WhatsAppInterface

        return WhatsAppInterface()

    if resolved == "teams":
        from messaging.teams.interface import TeamsInterface

        return TeamsInterface()

    # Unreachable — kept for type-checker satisfaction
    raise ValueError(f"Unhandled channel: {resolved!r}")
