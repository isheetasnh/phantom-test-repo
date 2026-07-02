"""
services.monitor_service — business logic for the monitor process.

This module contains everything the monitor *does* (prompt building,
Claude dispatch, credit-error classification, welcome copy) separated
from everything it *is* (poll loop, rate-limit backoff, SIGHUP wiring).

``processes/monitor.py`` imports from here; nothing else should need to.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import httpx
from clients.litellm_client import api_url, get_config, get_headers
from clients.posthog_client import capture
from clients.super_ninja_client import get_super_ninja_url

# REPO_ROOT is the src/ninja/ package root — used to locate claude-wrapper.sh
_REPO_ROOT = Path(__file__).parent.parent

_TITLE_SYSTEM_PROMPT = "You are a helpful assistant that generates extremely concise titles (2-4 words maximum) for tasks based on the user's message. Respond with only the title, no other text or punctuation."
_TITLE_USER_PROMPT = "Generate an extremely brief title (2-4 words only) for a task that starts with this message:\n{prompt}"

LITELLM_SPENDING_UPDATE_INTERVAL_SECONDS = 5
TASK_LOG_FILE = Path("/workspace/ninja/src/ninja/.task_log.jsonl")

# ---------------------------------------------------------------------------
# Credit / subprocess error classification
# ---------------------------------------------------------------------------


class RunOutOfCreditsException(Exception):
    """Raised when Claude's output signals the account has no remaining credits."""


def is_customer_key_error(output: str) -> bool:
    """Return True if Claude's subprocess output indicates out-of-credits."""
    lower = output.lower()
    return ("400" in lower and "budget has been exceeded" in lower) or (
        "402" in lower and "customer keys are not active" in lower
    )


def get_current_spend() -> float:
    """Return the cumulative key spend from the gateway /key/info endpoint."""
    try:
        cfg = get_config()
        resp = httpx.get(
            api_url("/key/info"),
            params={"key": cfg["api_key"]},
            headers={"Authorization": f"Bearer {cfg['api_key']}"},
            timeout=10.0,
        )
        return float(resp.json()["info"]["spend"])
    except Exception as e:
        print(f"⚠️ Could not get spend: {e}", file=sys.stderr)
        return 0.0


def _write_task_log(texts: list[str], started_at: float, cost: float) -> None:
    """Find the JSONL written by Claude during this invocation, extract last prompt UUID, write task log."""
    try:
        claude_projects = Path.home() / ".claude" / "projects"
        newest_file, newest_mtime = None, 0.0
        for f in claude_projects.rglob("*.jsonl"):
            mtime = f.stat().st_mtime
            if mtime >= started_at and mtime > newest_mtime:
                newest_file, newest_mtime = f, mtime

        prompt_uuid = None
        if newest_file:
            with open(newest_file, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        if entry.get("type") == "user" and isinstance(
                            entry.get("message", {}).get("content"), str
                        ):
                            prompt_uuid = entry.get(
                                "uuid"
                            )  # keep updating → last one wins
                    except json.JSONDecodeError:
                        pass

        if prompt_uuid:
            combined = " | ".join(texts) if texts else ""
            title = generate_task_title(combined) if combined else None
            if not title:
                title = (
                    (combined[:50] + "…")
                    if len(combined) > 50
                    else combined or "User prompt"
                )
            entry: dict = {
                "id": prompt_uuid,
                "texts": texts,
                "cost": cost,
                "title": title,
            }
            with open(_REPO_ROOT / ".task_log.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            print(
                f"📝 Task log written: uuid={prompt_uuid} title={title!r} cost=${cost:.6f}",
                flush=True,
            )
    except Exception as e:
        print(f"⚠️ Could not write task log: {e}", file=sys.stderr)


def generate_task_title(prompt: str) -> str | None:
    """Call LiteLLM to generate a 2-4 word title for the given prompt."""
    try:
        resp = httpx.post(
            api_url("/v1/chat/completions"),
            headers=get_headers(),
            json={
                "model": "claude-haiku-4-5-20251001",
                "messages": [
                    {"role": "system", "content": _TITLE_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": _TITLE_USER_PROMPT.format(prompt=prompt),
                    },
                ],
                "max_tokens": 20,
                "temperature": 0.7,
            },
            timeout=10.0,
        )
        title = (
            resp.json()["choices"][0]["message"]["content"].strip().strip("'\" \n\t")
        )
        return title or None
    except Exception as e:
        print(f"⚠️ Could not generate task title: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Welcome message
# ---------------------------------------------------------------------------

# Distinctive opening phrase used as an idempotency anchor by the adapter's
# post_welcome_if_needed() implementation. Must match the first user-visible
# sentence of build_welcome_message(). Do not change without updating the
# adapter and the test suite.
WELCOME_SIGNATURE = "Hi, I'm Ninja \u2014 your"


def build_welcome_message(agent: dict) -> str:
    """Build the first-run welcome announcement text (channel-agnostic Markdown).

    The first user-visible sentence MUST contain ``WELCOME_SIGNATURE`` — it
    doubles as an invisible idempotency anchor when reading back channel history.
    """
    emoji = agent.get("emoji", "\U0001f47b")
    name = agent.get("name", "Ninja")
    role = agent.get("role", "Browser Automation Agent")
    return (
        f"{emoji} **Hi, I'm {name} \u2014 your {role}.**\n"
        "Think of me as a virtual employee on your team. Brief me in "
        "any language \u2014 by message, voice note, or file \u2014 and I'll "
        "get the work done. No clicking, no copy-pasting, no API keys "
        "for you to manage.\n"
        "\n"
        "**\U0001f4bc What you can ask me to do**\n"
        '- **Research & reports** \u2014 "Pull the top 10 competitors for '
        'X, summarise their pricing, give me a one-pager."\n'
        '- **Lead gen & outreach** \u2014 "Find 50 founders of seed-stage '
        'fintechs in NYC and draft a personalised intro email to each."\n'
        '- **Recruiting** \u2014 "Source 20 senior backend engineers from '
        'LinkedIn matching this JD and message them on my behalf."\n'
        '- **Operations & data entry** \u2014 "Update these 30 Salesforce '
        'records from this spreadsheet," or "file my expense reports '
        'from these receipts."\n'
        '- **Travel & bookings** \u2014 "Book the cheapest direct flight '
        'from SFO to JFK next Friday and add it to my calendar."\n'
        '- **Creative work** \u2014 "Generate a flat-style logo for a '
        'coffee app called Brewly," or edit a photo, design a banner, '
        "or mock up a landing page hero image.\n"
        "- **Reports posted right here** \u2014 I deliver results back to "
        "this channel as a message, file, image, or thread you can act on.\n"
        "\n"
        "**\U0001f9f0 How I get things done \u2014 two complementary tools**\n"
        "- \U0001f30d **Browser** \u2014 I drive a real Chromium browser the "
        "way a human would: navigate any website, fill forms, click "
        "through flows, scrape data, log in to dashboards, download "
        "files. *Best for:* anything without an API \u2014 internal admin "
        "tools, niche SaaS, web search, news, social profiles, "
        "research across many sites.\n"
        "- \U0001f50c **Integrations (3,000+ apps)** \u2014 direct, "
        "authenticated access to Slack, Gmail, Google Calendar, "
        "GitHub, Jira, Linear, Notion, Salesforce, HubSpot, Stripe, "
        "Airtable, Asana, LinkedIn, X/Twitter, AWS, and ~3,000 more. "
        "*Best for:* anything with a stable API \u2014 fast, reliable, "
        "rate-limit-friendly, and works in the background even while "
        "you're offline.\n"
        "I pick the right tool for each step automatically; you "
        "don't have to choose.\n"
        "\n"
        "**\U0001f4ac How to brief me**\n"
        "- Just type a message in this channel \u2014 I reply to every "
        "human message.\n"
        "- Include the word `ninja` anywhere in your message to be "
        "explicit, or to ping me from a thread.\n"
        "- Send a *voice note* in any language and I'll transcribe and "
        "act on it.\n"
        "- Drop a *screenshot, PDF, spreadsheet, or any file* with "
        "your request and I'll use it as context.\n"
        "\n"
        "**\U0001f441 Watch me work**\n"
        "- [**Live Browser**](0.0.0.0:6080/vnc.html?autoconnect=true) "
        "\u2014 watch my Chromium session in real time. Take over with "
        "mouse/keyboard if I get stuck.\n"
        "- [**Activity Dashboard**](0.0.0.0:9000) \u2014 live identity, "
        "logs, reasoning trace, and per-task cost.\n"
        "- [**Connect Apps**](0.0.0.0:9020) \u2014 connect new apps "
        "(one-click OAuth) so I can use them. Anything you connect "
        "here becomes a tool I can call."
    )


# ---------------------------------------------------------------------------
# Prompt / CLI command helpers
# ---------------------------------------------------------------------------


def say_cmd(thread_ts: Optional[str] = None) -> str:
    """Return the channel-appropriate CLI say command for Claude's prompt.

    Reads ``MESSAGING_CHANNEL`` env-var (default: ``slack``) so the command
    embedded in Claude's prompt always matches the active adapter.
    """
    channel = os.environ.get("MESSAGING_CHANNEL", "slack")
    base = f"python messaging/{channel}/interface.py say"
    if thread_ts:
        return f'{base} "message" -t {thread_ts}'
    return f'{base} "message"'


# ---------------------------------------------------------------------------
# Claude batch dispatch
# ---------------------------------------------------------------------------


def run_batched_response(
    agent: dict,
    pending_messages: list,
    messaging_say_fn,
) -> bool:
    """Send all pending messages to Claude in a single prompt.

    Args:
        agent:            Agent configuration dict.
        pending_messages: List of message dicts (user, text, timestamp,
                          thread_ts, type, audio_files).
        messaging_say_fn: Callable used to post the out-of-credits notification
                          (typically ``_get_messaging().say``). Passed in to
                          avoid a circular import with monitor.py.

    Returns:
        True if Claude successfully processed the messages.
    """
    if not pending_messages:
        return True

    agent_name = agent["name"]
    agent_role = agent["role"]
    agent_emoji = agent["emoji"]

    messages_text = ""
    for i, msg in enumerate(pending_messages, 1):
        msg_type = msg.get("type", "mention")

        if msg_type == "cron":
            reply_hint = say_cmd(msg.get("thread_ts"))
            messages_text += (
                f"\n--- Message {i} (cron — scheduled job) ---\n"
                f"Cron ID: {msg.get('cron_id', 'unknown')}\n"
                f"Prompt: {msg.get('text', '')}\n"
                f"Post the result with: {reply_hint}\n"
                "(See agent-docs/CRON.md. Execute the prompt; do not ask for confirmation.)\n"
            )
            continue

        thread_info = (
            f'\n   Thread: {msg["thread_ts"]} (reply with: {say_cmd(msg["thread_ts"])})'
            if msg.get("thread_ts")
            else f"\n   Channel: main (reply with: {say_cmd()})"
        )

        attachment_info = ""
        if msg.get("audio_files"):
            attachment_info += (
                "\n   🎤 AUDIO/VOICE MESSAGE — Must transcribe before responding!"
            )
            for af in msg["audio_files"]:
                attachment_info += (
                    f"\n   Audio file: {af.get('name', 'audio')} "
                    f"({af.get('mimetype', 'audio/*')})"
                    f"\n   Download URL: {af.get('url', 'N/A')}"
                )
        if msg.get("image_files"):
            attachment_info += "\n   🖼️  IMAGE ATTACHMENT(S):"
            for img in msg["image_files"]:
                size_kb = img.get("size", 0) // 1024
                attachment_info += (
                    f"\n   Image: {img.get('name', 'image')} "
                    f"({img.get('mimetype', 'image/*')}, {size_kb} KB)"
                    f"\n   Download URL: {img.get('url', 'N/A')}"
                )
        if msg.get("pdf_files"):
            attachment_info += "\n   📄 PDF ATTACHMENT(S):"
            for pdf in msg["pdf_files"]:
                size_kb = pdf.get("size", 0) // 1024
                attachment_info += (
                    f"\n   PDF: {pdf.get('name', 'file.pdf')} ({size_kb} KB)"
                    f"\n   Download URL: {pdf.get('url', 'N/A')}"
                )
        if msg.get("other_files"):
            attachment_info += "\n   📎 OTHER FILE ATTACHMENT(S):"
            for of in msg["other_files"]:
                size_kb = of.get("size", 0) // 1024
                attachment_info += (
                    f"\n   File: {of.get('name', 'file')} "
                    f"({of.get('mimetype', 'application/octet-stream')}, {size_kb} KB)"
                    f"\n   Download URL: {of.get('url', 'N/A')}"
                )

        messages_text += (
            f"\n--- Message {i} ({msg_type}) ---\n"
            f"From: {msg.get('user', 'Unknown')}\n"
            f"Time: {msg.get('timestamp', 'Unknown')}\n"
            f"Text: {msg.get('text', '')}{attachment_info}{thread_info}\n"
        )

    prompt = (
        f"You are {agent_name} {agent_emoji}, the {agent_role}.\n\n"
        "We are running you as a monitor agent, your specification is in "
        "agent-docs/MONITOR.md. For scheduled cron items see agent-docs/CRON.md.\n\n"
        f"The current time is {time.strftime('%Y-%m-%d %H:%M:%S')}. "
        f"You have {len(pending_messages)} message(s) that need your response. "
        f"Read ALL of them and respond to EACH ONE.\n\n"
        f"{messages_text}"
    )

    print(
        f"\n{agent_emoji} Sending {len(pending_messages)} message(s) to Claude...",
        flush=True,
    )

    texts = [msg.get("text", "") for msg in pending_messages if msg.get("text")]
    before_spend = get_current_spend()
    started_at = time.time()

    try:
        result = subprocess.run(
            [str(_REPO_ROOT / "claude-wrapper.sh"), "-c", "-p", prompt],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=180,
        )
        output = result.stdout + result.stderr
        success_count = (
            output.count("Message sent")
            + output.count("✅")
            + output.count("Timestamp:")
        )

        def _record_cost():
            task_cost = 0.0
            while task_cost <= 0:
                # get_current_spend calls an API that guarantees eventual-consistency
                # it needs to time.sleep() to get updated spending (it's temporary till we find a better approach)
                time.sleep(LITELLM_SPENDING_UPDATE_INTERVAL_SECONDS)
                after_spend = get_current_spend()
                task_cost = max(0.0, after_spend - before_spend)
            _write_task_log(texts, started_at, task_cost)

        threading.Thread(target=_record_cost, daemon=True).start()

        if is_customer_key_error(output):
            raise RunOutOfCreditsException

        capture(
            "ninja batch response completed",
            {"success": True, "response_count": success_count},
        )
        if success_count > 0:
            print(
                f"✅ Claude processed batch — {success_count} response indicator(s)",
                flush=True,
            )
        else:
            print(
                f"⚠️ Claude batch response (may have posted): {output[:300]}...",
                flush=True,
            )
        return True

    except RunOutOfCreditsException:
        try:
            top_up_url = f"{get_super_ninja_url()}/dashboard#buy-credits"
            messaging_say_fn(
                f"\U0001f4b3 **You're out of credits**\n"
                f"Your current task has been paused.\n"
                f"Add credits to pick up where you left off — your work is saved.\n\n"
                f"**[Add credits \u2192]({top_up_url})**"
            )
            print("\U0001f4b3 Posted insufficient credit notification", flush=True)
        except Exception as e:
            print(f"\u26a0\ufe0f Credit notification skipped: {e}", file=sys.stderr)
        capture(
            "ninja batch response failed because of the insufficient credits",
            {"success": False, "error": "insufficient credit"},
        )
        return False
    except subprocess.TimeoutExpired:
        capture(
            "ninja batch response completed", {"success": False, "error": "timeout"}
        )
        print("⚠️ Claude batch response timed out", flush=True)
        return False
    except Exception as e:
        capture("ninja batch response completed", {"success": False, "error": str(e)})
        print(f"⚠️ Error: {e}", flush=True)
        return False
