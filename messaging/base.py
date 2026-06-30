"""
MessagingInterface — channel-agnostic ABC.

Each channel adapter (slack/, whatsapp/, teams/) must implement this interface.
Internal code should import only from this module, never from a specific channel.

The contract here is exactly what ``processes/monitor.py`` calls directly.
Everything else (react, get_replies, is_own_message, is_bot_message,
is_human_message, has_audio_attachment, upload_file, is_connected, …) is an
internal implementation detail of each adapter — not part of this contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class MessagingInterface(ABC):
    """Abstract base class for all messaging channel adapters."""

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    @abstractmethod
    def say(
        self,
        message: str,
        channel: Optional[str] = None,
        thread_ts: Optional[str] = None,
        username: Optional[str] = None,
        icon_emoji: Optional[str] = None,
        icon_url: Optional[str] = None,
        agent: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send a text message.

        Args:
            message:    Message body (Markdown supported where the channel allows).
            channel:    Override the default channel/conversation.
            thread_ts:  Thread / reply-to identifier (channel-specific format).
            username:   Display name override for this message.
            icon_emoji: Emoji avatar override.
            icon_url:   Image URL avatar override.
            agent:      Named agent whose configured identity to use.

        Returns:
            Channel API response dict.
        """

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    @abstractmethod
    def get_history(
        self,
        channel: Optional[str] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Retrieve recent messages from a channel.

        Args:
            channel: Override the default channel/conversation.
            limit:   Maximum number of messages to return.

        Returns:
            List of message dicts (structure is channel-specific).
        """

    # ------------------------------------------------------------------
    # Monitor integration
    # ------------------------------------------------------------------

    @abstractmethod
    def collect_pending(
        self,
        msg: Dict[str, Any],
        agent_mentions: List[str],
        seen_messages: set,
        agent_data: dict,
        pending_messages: list,
    ) -> None:
        """Process a single raw message from get_history().

        For each unseen, non-own message this method:
          1. Reacts with a channel-native ack when warranted.
          2. Appends the message to ``pending_messages`` if the agent should respond.
          3. Scans for unseen thread replies and appends those too.

        Mutates ``seen_messages``, ``agent_data``, and ``pending_messages`` in place.

        The ack emoji, thread reply schema, and message classification
        (own / bot / human / audio) are all internal to each adapter.

        Args:
            msg:              A single message dict from ``get_history()``.
            agent_mentions:   List of keyword strings that trigger a response
                              (e.g. ["ninja", "@ninja"]).
            seen_messages:    Set of already-processed message IDs. Updated in place.
            agent_data:       Persistent state dict (seen_replies, welcomed, …).
                              Updated in place.
            pending_messages: List to append actionable messages to. Updated in place.
        """

    @abstractmethod
    def post_welcome_if_needed(self, agent: dict, welcome_text: str) -> bool:
        """Post ``welcome_text`` if the channel has no prior human messages.

        Idempotent — checks both a persisted flag in ``agent_data`` and a
        signature string in channel history before posting. Should never raise;
        errors are swallowed so they do not block monitor startup.

        Args:
            agent:        Agent config dict (name, emoji, role, mentions, …).
            welcome_text: The fully-rendered welcome message to post.

        Returns:
            True if the message was actually posted this call, False otherwise.
        """

    # ------------------------------------------------------------------
    # Health service integration
    # ------------------------------------------------------------------

    @abstractmethod
    def check_messaging_health(self) -> Dict[str, Any]:
        """Validate the messaging channel credentials and connectivity.

        Called by ``processes/health_service.py`` on each health check cycle.
        Should never raise — all errors must be caught and returned as a status
        dict so the health service can continue with the remaining checks.

        Returns:
            Dict with at minimum:
                ``service``  — channel name string (e.g. "slack", "whatsapp")
                ``status``   — one of: "ok", "missing", "invalid", "error"
            Optional keys:
                ``message``  — human-readable error detail
                ``team``     — workspace / account name on success (if available)
        """
