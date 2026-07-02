"""
Microsoft Teams messaging adapter.

Implements the channel-agnostic MessagingInterface against Microsoft Graph,
including the monitor-integration surface (collect_pending /
post_welcome_if_needed / check_messaging_health) so processes/monitor.py can
run with MESSAGING_CHANNEL=teams.

CLI usage (mirrors slack/interface.py):
    python messaging/teams/interface.py say "Hello!"
    python messaging/teams/interface.py read
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, fields
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.config import load_agent_messages, save_agent_messages
from messaging.base import MessagingInterface
from messaging.message_utils import (
    classify_message_type,
    extract_file_attachments,
    is_bot_message,
)
from messaging.teams.exceptions import TeamsAPIError, TeamsConfigError
from messaging.teams.graph_api import (
    _ensure_ok,
    _message_body,
    _quote,
    make_graph_api_request,
    set_reaction,
    upload_bytes_to_channel,
)
from messaging.teams.utils import (
    _guess_audio_content_type,
    _guess_content_type,
    _is_audio_content_type,
    normalize_message,
)
from services.monitor_service import WELCOME_SIGNATURE

DEFAULT_CONFIG_PATH = Path.home() / ".agent_settings.json"
MCP_TOKEN_FILE = "/dev/shm/mcp-token"
TOKEN_TTL_SECONDS = 5 * 60


@dataclass
class TeamsConfig:
    access_token: Optional[str] = None
    team_id: Optional[str] = None
    channel_id: Optional[str] = None
    access_token_expires_at: Optional[int] = None

    @classmethod
    def load(cls, config_path: Path) -> TeamsConfig:
        if not config_path.exists():
            return cls()
        try:
            with open(config_path, "r") as f:
                data = json.load(f) or {}
        except Exception as e:
            raise TeamsConfigError(
                f"Failed to load Teams config from {config_path}: {e}"
            ) from e
        if not isinstance(data, dict):
            raise TeamsConfigError("Invalid Teams config format")
        if "teams" in data:
            return cls.from_settings(data)
        teams_keys = {
            "access_token",
            "team_id",
            "channel_id",
            "access_token_expires_at",
        }
        return cls(**{k: v for k, v in data.items() if k in teams_keys})

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> TeamsConfig:
        teams_settings = settings.get("teams", {})
        if not isinstance(teams_settings, dict):
            raise TeamsConfigError("Invalid Teams settings format")
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in teams_settings.items() if k in known})

    def to_settings(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "access_token": self.access_token,
            "team_id": self.team_id,
            "channel_id": self.channel_id,
            "access_token_expires_at": self.access_token_expires_at,
        }
        return {k: v for k, v in data.items() if v is not None}

    def save(self, filepath: Path) -> None:
        try:
            with open(filepath, "r") as f:
                settings = json.load(f) or {}
        except (OSError, json.JSONDecodeError):
            settings = {}
        if not isinstance(settings, dict):
            settings = {}
        settings["teams"] = self.to_settings()
        with open(filepath, "w") as f:
            json.dump(settings, f, indent=2)


class TeamsInterface(MessagingInterface):
    """Microsoft Teams adapter backed by Microsoft Graph."""

    def __init__(self, config_file: str | Path = DEFAULT_CONFIG_PATH):
        super().__init__()
        self.config_file = (
            Path(config_file).expanduser() if config_file else DEFAULT_CONFIG_PATH
        )
        self.config = TeamsConfig.load(self.config_file)
        self.team_id = self.config.team_id
        self.channel_id = self.config.channel_id
        # Ids of messages this process has posted via say(). This — not the Graph
        # identity — is how is_own_message() avoids self-replies: a delegated user
        # token shares one identity with the human operator, so identity matching
        # would drop the human's own prompts. Tracking what *we* sent works for
        # both delegated and application tokens.
        self._sent_message_ids: set[str] = set()

    @property
    def is_connected(self) -> bool:
        return bool(self.token and self.team_id and self.channel_id)

    @property
    def token(self) -> Optional[str]:
        return self.get_access_token()

    def _read_teams_access_token(self, filepath: str = MCP_TOKEN_FILE) -> Optional[str]:
        """Read the Microsoft Graph access token from the MSTeams= entry in the MCP token file. Returns None if the file or entry is missing/unparseable."""
        try:
            with open(filepath, "r") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("MSTeams="):
                        payload = json.loads(line[len("MSTeams=") :])
                        return payload.get("access_token") or None
        except Exception as e:
            print(f"Failed to read Teams access token from {filepath}: {e}")
        return None

    @lru_cache(maxsize=1)
    def _cached_token(
        self, time_bucket: int, filepath: str = MCP_TOKEN_FILE
    ) -> Optional[str]:
        # Time bucket is used to invalidate the cache every TOKEN_TTL_SECONDS seconds.
        # If this is not there, it would cache the token forever, as the filepath param would not change.
        return self._read_teams_access_token(filepath)

    def get_access_token(self, filepath: str = MCP_TOKEN_FILE) -> Optional[str]:
        """
        Return the Teams Graph token, cached for ~5 minutes.
        Reads /dev/shm/mcp-token at most once per TTL window.
        """
        # time is used to determine how many TTL windows have passed, so the cache is invalidated every TOKEN_TTL_SECONDS seconds.
        time_bucket = int(time.time() // TOKEN_TTL_SECONDS)
        return self._cached_token(time_bucket, filepath)

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
        """
        Send message in the configured Teams channel. 1:1 chat does not need to be supported.

        :param channel: Teams channel id; falls back to the configured channel.
        :param thread_ts: parent channel message id to reply to. If not provided,
            posts a new top-level message to the channel.
        """
        channel_id = channel or self.channel_id
        if thread_ts:
            path = (
                f"/teams/{_quote(self.team_id)}/channels/{_quote(channel_id)}"
                f"/messages/{_quote(thread_ts)}/replies"
            )
        else:
            path = (
                f"/teams/{_quote(self.team_id)}/channels/{_quote(channel_id)}/messages"
            )
        status, payload, _ = make_graph_api_request(
            "POST",
            path,
            token=self.token,
            body=_message_body(message, is_html=False),
        )
        result = _ensure_ok(status, payload)
        sent_id = result.get("id") if isinstance(result, dict) else None
        if sent_id:
            self._sent_message_ids.add(str(sent_id))
        return result

    def upload_file(
        self,
        file_path: str,
        channel: Optional[str] = None,
        title: Optional[str] = None,
        content_type: Optional[str] = None,
        comment: Optional[str] = None,
        thread_ts: Optional[str] = None,
        agent: Optional[str] = None,
        *,
        require_audio: bool = False,
    ) -> Dict[str, Any]:
        """Upload a local file to the channel Files folder and return the DriveItem.

        ``title``/``comment``/``thread_ts``/``agent`` are accepted for parity with
        other channel adapters but are currently ignored by the Teams implementation.

        :param require_audio: enforce that the file resolves to an audio/* content
            type, used by audio uploads that need special content-type handling.
        """
        path = Path(file_path).expanduser()
        if not path.exists():
            raise TeamsConfigError(f"file does not exist: {path}")
        if not path.is_file():
            raise TeamsConfigError(f"not a file: {path}")
        resolved_content_type = (
            _guess_audio_content_type(path.name, content_type)
            if require_audio
            else _guess_content_type(path.name, content_type)
        )
        if require_audio and not _is_audio_content_type(resolved_content_type):
            raise TeamsConfigError(
                f"audio upload requires an audio/* content type; got "
                f"{resolved_content_type} for {path.name}"
            )
        try:
            content = path.read_bytes()
        except OSError as e:
            raise TeamsConfigError(f"could not read file {path}: {e}") from e
        return upload_bytes_to_channel(
            path.name,
            content,
            team_id=self.team_id,
            channel_id=channel or self.channel_id,
            token=self.token,
            content_type=resolved_content_type,
        )

    def get_messages(
        self,
        channel: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Fetch recent messages from the channel, newest first."""
        channel_id = channel or self.channel_id
        top = max(1, min(int(limit), 50))
        status, payload, _ = make_graph_api_request(
            "GET",
            f"/teams/{_quote(self.team_id)}/channels/{_quote(channel_id)}/messages",
            token=self.token,
            query={"$top": top},
        )
        payload = _ensure_ok(status, payload)
        items = payload.get("value") if isinstance(payload, dict) else []
        if not isinstance(items, list):
            return []
        return [normalize_message(x) for x in items if isinstance(x, dict)]

    def get_history(
        self,
        channel: Optional[str] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """MessagingInterface entry point — delegates to get_messages."""
        return self.get_messages(channel=channel, limit=limit)

    def get_replies(
        self,
        parent_message_id: str,
        channel: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        channel_id = channel or self.channel_id
        top = max(1, min(int(limit), 50))
        status, payload, _ = make_graph_api_request(
            "GET",
            f"/teams/{_quote(self.team_id)}/channels/{_quote(channel_id)}/messages/{_quote(parent_message_id)}/replies",
            token=self.token,
            query={"$top": top},
        )
        payload = _ensure_ok(status, payload)
        items = payload.get("value") if isinstance(payload, dict) else []
        if not isinstance(items, list):
            return []
        return [normalize_message(x) for x in items if isinstance(x, dict)]

    def get_replies_or_empty(
        self,
        parent_message_id: str,
        channel: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        try:
            return self.get_replies(
                parent_message_id,
                channel=channel,
                limit=limit,
            )
        except Exception:
            return []

    def react(
        self,
        message_id: str,
        emoji: str = "🥷",
        channel: Optional[str] = None,
        *,
        reply_to_id: Optional[str] = None,
    ) -> bool:
        """Add an emoji reaction to a channel message or a threaded reply.

        Signature and return type match MessagingInterface.react (and Slack's)
        so a channel-agnostic caller can react identically. ``reply_to_id`` is
        Teams-specific (the parent message id when reacting to a thread reply)
        and is keyword-only so the positional args stay aligned with Slack.
        """
        if not self.is_connected:
            return False
        try:
            set_reaction(
                self.team_id,
                channel or self.channel_id,
                message_id,
                self.token,
                reaction_type=emoji,
                reply_to_id=reply_to_id,
            )
            return True
        except (TeamsConfigError, TeamsAPIError):
            return False

    def is_own_message(self, message: Dict[str, Any]) -> bool:
        """True if ``message`` was posted by this monitor process via say().

        Deliberately id-based, not identity-based: a delegated user token shares
        its Graph identity with the human operator, so matching on from_user_id
        would discard the human's own prompts. Self-posts (replies, welcome) are
        recorded in ``_sent_message_ids`` when say() returns, and prior-run posts
        are already filtered by the persisted ``seen_messages`` set.
        """
        return str(message.get("id") or "") in self._sent_message_ids

    def is_human_message(self, message: Dict[str, Any]) -> bool:
        """True if ``message`` was sent by a real human user (not an app)."""
        return bool(message.get("from_user_id")) and not is_bot_message(message)

    # ------------------------------------------------------------------
    # Monitor integration — ABC implementation
    # ------------------------------------------------------------------

    def _evaluate_pending(
        self,
        candidate: Dict[str, Any],
        *,
        parent_id: Optional[str],
        is_reply: bool,
        emoji: str,
        agent_mentions: List[str],
    ) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Evaluate candidate and return (pending_message, seen_key) if actionable.

        Performs filtering, reaction, classification, and pending message building.
        Returns:
            (pending_message_dict, seen_key) if the message should be queued.
                seen_key is the dedup key for replies ("<parent_id>:<cand_id>"),
                or None for top-level messages.
            (None, None) if the message should be skipped.
        """
        # Step 1: never respond to our own posts.
        if self.is_own_message(candidate):
            return None, None

        # Step 2: decide whether to respond and whether to ack.
        text = (candidate.get("text") or "").lower()
        is_mentioned = any(m.lower() in text for m in agent_mentions)
        # Replies: only when mentioned. Main channel: humans always, bots only
        # when mentioned — same policy as the Slack adapter.
        should_respond = (
            is_mentioned
            if is_reply
            else (not is_bot_message(candidate) or is_mentioned)
        )
        should_react = self.is_human_message(candidate) or (
            is_bot_message(candidate) and is_mentioned
        )

        if not should_respond:
            return None, None

        cand_id = candidate.get("id", "")

        # Step 3: ack (best-effort — a failed reaction never blocks the queue).
        if should_react and cand_id:
            try:
                self.react(cand_id, emoji, reply_to_id=parent_id)
            except Exception:
                pass

        # Step 4: classify and build pending message.
        attachments = extract_file_attachments(candidate)
        msg_type = classify_message_type(attachments, is_reply)
        # Reply target: a thread reply answers under its thread root (parent_id);
        # a top-level message answers under itself (cand_id) so the response is
        # threaded beneath the triggering post rather than added as a new
        # top-level channel message. Teams threads under whatever message id is
        # passed to say(thread_ts=...).
        reply_target = parent_id if is_reply else cand_id
        pending_message = {
            "user": candidate.get("from") or "Unknown",
            "text": candidate.get("text", ""),
            "timestamp": cand_id,
            "thread_ts": reply_target or None,
            "type": msg_type,
            "audio_files": attachments["audio_files"],
            "image_files": attachments["image_files"],
            "pdf_files": attachments["pdf_files"],
            "other_files": attachments["other_files"],
        }

        # Compute the dedup key for replies.
        seen_key = f"{parent_id}:{cand_id}" if is_reply and parent_id else None

        return pending_message, seen_key

    def collect_pending(
        self,
        msg: Dict[str, Any],
        agent_mentions: List[str],
        seen_messages: set,
        agent_data: dict,
        pending_messages: list,
    ) -> None:
        """Process one top-level channel message and its thread replies.

        The top-level message is answered once, de-duplicated via
        ``seen_messages``. Thread replies are scanned on *every* poll — including
        for an already-seen parent — because a new reply can land on an old
        message; they are de-duplicated separately via
        ``agent_data["seen_replies"]`` keyed as ``<parent>:<reply>``. Skipping the
        reply scan when the parent is seen would silently drop in-thread questions
        on any message older than the current cycle.

        Graph exposes no cheap reply-count signal on the channel messages list,
        so this issues one ``get_replies`` call per top-level message each poll.
        That cost is bounded by the monitor's history page size (50).
        """
        emoji = os.environ.get("NINJA_AGENT_EMOJI", "🥷").strip()

        msg_id = msg.get("id", "")
        if not msg_id:
            return
        seen_replies = set(agent_data.get("seen_replies", []))

        # Top-level message — answer only the first time we see it.
        if msg_id not in seen_messages:
            seen_messages.add(msg_id)
            pending_msg, _ = self._evaluate_pending(
                msg,
                parent_id=None,
                is_reply=False,
                emoji=emoji,
                agent_mentions=agent_mentions,
            )
            if pending_msg:
                pending_messages.append(pending_msg)

        # Thread replies — always scanned (Graph returns replies only, not the
        # parent), so new replies on a previously-seen parent are still caught.
        replies = self.get_replies_or_empty(msg_id)
        for reply in replies:
            reply_id = reply.get("id", "")
            if not reply_id:
                continue
            seen_key = f"{msg_id}:{reply_id}"
            if seen_key in seen_replies:
                continue
            pending_msg, reply_seen_key = self._evaluate_pending(
                reply,
                parent_id=msg_id,
                is_reply=True,
                emoji=emoji,
                agent_mentions=agent_mentions,
            )
            if pending_msg and reply_seen_key:
                pending_messages.append(pending_msg)
                seen_replies.add(reply_seen_key)
        agent_data["seen_replies"] = list(seen_replies)

    def post_welcome_if_needed(self, agent: dict, welcome_text: str) -> bool:
        """Post ``welcome_text`` once, if the channel has no prior human activity.

        Idempotency layers, mirroring the Slack adapter:
          1. Persisted ``welcomed`` flag in ``.agent_messages.json``.
          2. History sniff for any human message or the welcome signature.
        Never raises — failures are swallowed so monitor startup is unblocked.
        """
        agent_data = load_agent_messages()
        if agent_data.get("welcomed"):
            return False

        try:
            messages = self.get_history(limit=50)
        except Exception:
            return False

        for m in messages:
            if self.is_human_message(m) or WELCOME_SIGNATURE in (m.get("text") or ""):
                agent_data["welcomed"] = True
                save_agent_messages(agent_data)
                return False

        try:
            self.say(welcome_text)
            agent_data["welcomed"] = True
            save_agent_messages(agent_data)
            return True
        except Exception as e:
            print(f"⚠️ Welcome announcement skipped: {e}", file=sys.stderr)
            return False

    def check_messaging_health(self) -> Dict[str, Any]:
        """Validate Teams credentials by fetching the configured channel.

        Returns a status dict compatible with health_service. Never raises.
        """
        if not (self.token and self.team_id and self.channel_id):
            return {
                "service": "teams",
                "status": "missing",
                "message": "Missing Teams access_token / team_id / channel_id",
            }
        try:
            status, payload, _ = make_graph_api_request(
                "GET",
                f"/teams/{_quote(self.team_id)}/channels/{_quote(self.channel_id)}",
                token=self.token,
            )
            if 200 <= status < 300:
                name = payload.get("displayName") if isinstance(payload, dict) else None
                return {
                    "service": "teams",
                    "status": "ok",
                    "team": name or self.team_id,
                }
            if status in (401, 403):
                return {
                    "service": "teams",
                    "status": "invalid",
                    "message": f"Graph auth failed ({status})",
                }
            return {
                "service": "teams",
                "status": "error",
                "message": f"Graph returned {status}",
            }
        except Exception as e:
            return {"service": "teams", "status": "error", "message": str(e)}

    def get_unique_channel(self) -> str:
        raise NotImplementedError("Teams adapter is not yet implemented.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Teams messaging CLI")
    parser.add_argument(
        "-C",
        "--config-file",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"Path to config file (default: {DEFAULT_CONFIG_PATH})",
    )
    sub = parser.add_subparsers(dest="command")

    p_config = sub.add_parser("config", help="Show or set configuration")
    p_config.add_argument(
        "--clear",
        action="store_true",
        help="Clear Microsoft Teams configuration",
    )
    p_config.add_argument("--set-access-token", help="Set Microsoft Graph access token")
    p_config.add_argument("--set-team-id", help="Set Microsoft Teams team ID")
    p_config.add_argument("--set-channel-id", help="Set Microsoft Teams channel ID")
    p_config.add_argument(
        "--set-agent", help="Set the default agent the monitor runs as (e.g. ninja)"
    )

    p_config.set_defaults(func=cmd_config)

    p_say = sub.add_parser("say", help="Send a message to a Teams channel")
    p_say.add_argument("message", help="Text to send")
    p_say.add_argument(
        "-t",
        "--thread",
        help="parent channel message id to reply under (omit for a new top-level message)",
    )
    p_say.set_defaults(func=cmd_say)

    p_upload = sub.add_parser(
        "upload", help="Upload a local file to the Teams channel Files folder"
    )
    p_upload.add_argument("file", help="Local path to the file to upload")
    p_upload.add_argument("--content-type", help="override the detected content type")
    p_upload.add_argument(
        "--audio",
        action="store_true",
        help="require the file to resolve to an audio/* content type",
    )
    p_upload.set_defaults(func=cmd_upload)

    p_read = sub.add_parser("read", help="Read recent channel messages")
    p_read.add_argument("--limit", type=int, default=10, help="messages to fetch")
    p_read.set_defaults(func=cmd_read)

    p_react = sub.add_parser("react", help="Add an emoji reaction to a channel message")
    p_react.add_argument("message_id", help="ID of the message to react to")
    p_react.add_argument(
        "emoji",
        nargs="?",
        default="🥷",
        help="emoji name or character (default: 🥷)",
    )
    p_react.add_argument(
        "--reply-to",
        help="parent message id when reacting to a thread reply",
    )
    p_react.set_defaults(func=cmd_react)

    return parser


def cmd_say(args: argparse.Namespace) -> int:
    # Credentials come from the config file (~/.agent_settings.json), the same
    # source as read/react/upload — no env vars required. This is the command
    # the monitor tells Claude to run to post a reply, so it must work with only
    # the configured token/team/channel present.
    try:
        iface = TeamsInterface(config_file=args.config_file)
        result = iface.say(args.message, thread_ts=args.thread)
    except (TeamsConfigError, TeamsAPIError) as e:
        sys.stderr.write(f"say failed: {e}\n")
        return 2 if isinstance(e, TeamsConfigError) else 1
    print(f"Sending message: {args.message!r}")
    print("Response:", json.dumps(result, indent=2))
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    try:
        iface = TeamsInterface(config_file=args.config_file)
        uploaded = iface.upload_file(
            args.file,
            content_type=args.content_type,
            require_audio=args.audio,
        )
    except (TeamsConfigError, TeamsAPIError) as e:
        sys.stderr.write(f"upload failed: {e}\n")
        return 2 if isinstance(e, TeamsConfigError) else 1
    print(json.dumps(uploaded, indent=2))
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    try:
        iface = TeamsInterface(config_file=args.config_file)
        messages = iface.get_messages(limit=args.limit)
    except (TeamsConfigError, TeamsAPIError) as e:
        sys.stderr.write(f"read failed: {e}\n")
        return 2 if isinstance(e, TeamsConfigError) else 1
    if not messages:
        print("no messages")
        return 0
    for m in messages:
        text = (m.get("text") or "").replace("\n", " ")
        print(
            f"{m['id']}  {m.get('created') or '?'}  {m.get('from') or '?'}: {text[:80]}"
        )
    return 0


def cmd_react(args: argparse.Namespace) -> int:
    try:
        iface = TeamsInterface(config_file=args.config_file)
        ok = iface.react(args.message_id, args.emoji, reply_to_id=args.reply_to)
    except (TeamsConfigError, TeamsAPIError) as e:
        sys.stderr.write(f"react failed: {e}\n")
        return 2 if isinstance(e, TeamsConfigError) else 1
    print("reacted" if ok else "react failed")
    return 0 if ok else 1


def cmd_config(args: argparse.Namespace) -> int:
    config_file = Path(args.config_file).expanduser()
    config = TeamsConfig.load(config_file)
    if args.clear:
        config = TeamsConfig()
    if args.set_access_token:
        config.access_token = args.set_access_token
    if args.set_team_id:
        config.team_id = args.set_team_id
    if args.set_channel_id:
        config.channel_id = args.set_channel_id

    config.save(config_file)

    default_agent: Optional[str] = None
    if args.set_agent:
        # default_agent is a channel-agnostic, top-level setting the monitor reads
        # (core.config.load_agent_config); it lives beside the "teams" block, not
        # inside it. config.save() preserves it, so write it independently here.
        try:
            with open(config_file, "r") as f:
                settings = json.load(f) or {}
        except (OSError, json.JSONDecodeError):
            settings = {}
        if not isinstance(settings, dict):
            settings = {}
        settings["default_agent"] = args.set_agent
        with open(config_file, "w") as f:
            json.dump(settings, f, indent=2)
        default_agent = args.set_agent

    print("Microsoft Teams configuration:")
    print(f"  team_id:             {config.team_id or '-'}")
    print(f"  channel_id:          {config.channel_id or '-'}")
    token_preview = f"{config.access_token[:10]}..." if config.access_token else "-"
    print(f"  access_token:        {token_preview}")
    if default_agent:
        print(f"  default_agent:       {default_agent}")

    return 0


def main(argv: Optional[list[str]] = None) -> int:
    """
    creat
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
