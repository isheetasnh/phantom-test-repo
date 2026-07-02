# Microsoft Teams Interface CLI

A command-line tool and Python API for interacting with a Microsoft Teams
channel, backed by the Microsoft Graph API.

## Features

- 🔑 **Config-file credentials** — reads the Graph access token, team ID, and channel ID from `~/.agent_settings.json`
- 💬 **Channel messaging** — post top-level messages or threaded replies
- 🧵 **Threaded replies** — reply under an existing message via `-t <message_id>`
- 😀 **Reactions** — add emoji reactions to a message or a threaded reply
- 📎 **File uploads** — upload local files to the channel Files folder
- 🐍 **Python API** — use `TeamsInterface` as a library
- 🔁 **Transient-retry** — Graph calls retry on throttling/5xx (honoring `Retry-After`)

## Installation

The tool is included in this repository. No additional installation required.

### Dependencies

```bash
pip install requests
```

> The HTTP layer uses the Python standard library (`urllib`); no extra packages
> are required for the interface itself.

## Quick Start

### 1. Configure credentials

Teams uses a Microsoft Graph access token plus the target team and channel IDs.
There are no env vars to set — everything lives in the config file.

```bash
# Set the Microsoft Graph access token
python messaging/teams/interface.py config --set-access-token "<graph-access-token>"

# Set the team and channel the agent posts to
python messaging/teams/interface.py config --set-team-id "<team-id>"
python messaging/teams/interface.py config --set-channel-id "<channel-id>"

# Verify
python messaging/teams/interface.py config
```

### 2. Send messages

```bash
# Post a new top-level message to the configured channel
python messaging/teams/interface.py say "Hello team!"

# Reply under an existing message (thread)
python messaging/teams/interface.py say "Thread reply" -t "<message_id>"
```

Message text is Markdown — it is rendered to Teams-flavored HTML before sending.

### 3. Upload files

```bash
# Upload a file to the channel Files folder
python messaging/teams/interface.py upload report.pdf

# Override the detected content type
python messaging/teams/interface.py upload clip.dat --content-type audio/mp4

# Require the file to resolve to an audio/* type (used by audio flows)
python messaging/teams/interface.py upload voice.m4a --audio
```

## Configuration

### Config file location

Configuration is stored under the `teams` key of `~/.agent_settings.json`:

```json
{
  "teams": {
    "access_token": "<graph-access-token>",
    "team_id": "<team-id>",
    "channel_id": "<channel-id>",
    "access_token_expires_at": 1735689600
  }
}
```

### Setting values

```bash
# Set Graph access token
python messaging/teams/interface.py config --set-access-token "<graph-access-token>"

# Set team ID
python messaging/teams/interface.py config --set-team-id "<team-id>"

# Set channel ID
python messaging/teams/interface.py config --set-channel-id "<channel-id>"

# Clear all Teams configuration
python messaging/teams/interface.py config --clear

# View current config (token is shown truncated)
python messaging/teams/interface.py config
```

### Custom config file

```bash
python messaging/teams/interface.py -C /path/to/config.json config
```

## CLI Commands

### Configuration

```bash
# Show current configuration
python messaging/teams/interface.py config

# Set credentials
python messaging/teams/interface.py config --set-access-token "<token>"
python messaging/teams/interface.py config --set-team-id "<team-id>"
python messaging/teams/interface.py config --set-channel-id "<channel-id>"
```

### Messaging

```bash
# Post a top-level message to the configured channel
python messaging/teams/interface.py say "Your message here"

# Reply in a thread (under a parent message id)
python messaging/teams/interface.py say "Thread reply" -t "<message_id>"
```

### File Uploads

```bash
# Upload a file to the channel Files folder
python messaging/teams/interface.py upload path/to/file.png

# Override the content type
python messaging/teams/interface.py upload data.bin --content-type application/pdf

# Enforce an audio/* content type
python messaging/teams/interface.py upload voice.m4a --audio
```

### Reading Messages

```bash
# Read recent channel messages (default: 10)
python messaging/teams/interface.py read

# Read more messages (capped at the Graph page size of 50)
python messaging/teams/interface.py read --limit 50
```

### Reactions

```bash
# React to a channel message (default emoji: 🥷)
python messaging/teams/interface.py react "<message_id>"

# React with a specific emoji name or character
python messaging/teams/interface.py react "<message_id>" ghost

# React to a threaded reply (provide the parent message id)
python messaging/teams/interface.py react "<reply_id>" 🥷 --reply-to "<parent_message_id>"
```

## Python API

### Basic Usage

```python
from messaging.teams.interface import TeamsInterface

# Initialize (loads ~/.agent_settings.json by default)
teams = TeamsInterface()

# Check connection (token + team_id + channel_id all present)
if not teams.is_connected:
    print("Configure Teams credentials first!")
    exit(1)

# Post a top-level message
teams.say("Hello team!")

# Reply in a thread
teams.say("Thread reply", thread_ts="<message_id>")
```

### File Upload Example

```python
from messaging.teams.interface import TeamsInterface

teams = TeamsInterface()

# Upload a file to the channel Files folder; returns the Graph DriveItem
item = teams.upload_file("designs/mockup.png")
print(f"Uploaded: {item.get('name')} ({item.get('id')})")
```

### Reading and Reacting

```python
from messaging.teams.interface import TeamsInterface

teams = TeamsInterface()

# Recent channel messages (newest first)
for msg in teams.get_history(limit=10):
    print(f"{msg.get('from')}: {msg.get('text')}")

# Replies under a parent message
replies = teams.get_replies("<parent_message_id>", limit=20)

# React to a message (or a threaded reply via reply_to_id)
teams.react("<message_id>", "🥷")
teams.react("<reply_id>", "👻", reply_to_id="<parent_message_id>")
```

### Error Handling

```python
from messaging.teams.interface import TeamsInterface
from messaging.teams.exceptions import TeamsConfigError, TeamsAPIError

teams = TeamsInterface()

try:
    teams.say("Hello!")
except TeamsConfigError as e:
    print(f"Configuration error: {e}")  # missing token/team/channel, bad input
except TeamsAPIError as e:
    print(f"Graph API error ({e.status}): {e}")  # non-2xx response from Graph
```

## Authentication

The interface authenticates to Microsoft Graph with a bearer **access token**
loaded from the config file (`~/.agent_settings.json`). The token is populated
there at install time: the installer reads it from the `MSTeams=` entry in
`/dev/shm/mcp-token` and stores it via `config --set-access-token`.

At runtime the interface reads only the stored config value — it does **not**
re-read `/dev/shm/mcp-token` or any environment variable. To set or refresh the
token manually:

```bash
python messaging/teams/interface.py config --set-access-token "<graph-access-token>"
```

`access_token_expires_at` is stored alongside the token for reference, but the
interface does **not** auto-refresh. When a token expires (Graph returns
`401`/`403`), set a fresh one with the same command.

The token may be a delegated user token or an application token. Own-message
detection is id-based (tracking what this process posts), so a delegated token
that shares its identity with the human operator does not cause the monitor to
drop the operator's own messages.

## Troubleshooting

### "missing Teams destination value" / `TeamsConfigError`

A required value (token, team ID, or channel ID) is missing.

**Solution**: set all three:

```bash
python messaging/teams/interface.py config --set-access-token "<token>"
python messaging/teams/interface.py config --set-team-id "<team-id>"
python messaging/teams/interface.py config --set-channel-id "<channel-id>"
```

### Graph auth failed (`401` / `403`)

The access token is expired or lacks permission for the team/channel.

**Solution**: refresh the token, then re-set it:

```bash
python messaging/teams/interface.py config --set-access-token "<new-token>"
```

Verify with a health check from Python:

```python
from messaging.teams.interface import TeamsInterface
print(TeamsInterface().check_messaging_health())
```

### Reply posts as a new message instead of threading

`say` only threads when `thread_ts` (the parent message id) is provided.

**Solution**: pass the parent id:

```bash
python messaging/teams/interface.py say "Reply text" -t "<parent_message_id>"
```

## API Reference

### TeamsInterface Class

| Method                                                  | Description                                       |
| ------------------------------------------------------- | ------------------------------------------------- |
| `say(message, channel, thread_ts, ...)`                 | Post a message (top-level or threaded reply)      |
| `upload_file(file_path, channel, ..., require_audio)`   | Upload a local file to the channel Files folder   |
| `get_messages(channel, limit)`                          | Fetch recent channel messages (newest first)      |
| `get_history(channel, limit)`                           | MessagingInterface entry point → `get_messages`   |
| `get_replies(parent_message_id, channel, limit)`        | Fetch replies under a parent message              |
| `react(message_id, emoji, channel, reply_to_id)`        | Add an emoji reaction to a message/reply          |
| `collect_pending(...)`                                  | Monitor hook — classify/queue unanswered messages |
| `check_messaging_health()`                              | Validate credentials against Graph (never raises) |

### Properties

| Property       | Description                                                  |
| -------------- | ------------------------------------------------------------ |
| `is_connected` | Boolean — True when token, team_id, and channel_id are set   |

## License

MIT License - NinjaTech AI
