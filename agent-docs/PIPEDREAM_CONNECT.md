# Pipedream Connect Guide — Using Third-Party Integrations

You have access to 3,000+ connected apps (Gmail, Google Calendar, GitHub, Notion,
HubSpot, Salesforce, Linear, Jira, and more) through the integrations gateway.
Use the `pdx` CLI to interact with them.

---

## When to use integrations

Use `pdx chat` whenever a task involves a connected app — reading emails,
creating issues, updating calendar events, querying CRMs, etc. The gateway
handles authentication and tool dispatch automatically.

Use `pdx http` when you need to make a specific, known API call directly and
don't need the LLM to figure out which tool to use — it's faster and more
predictable than `pdx chat` for well-defined operations.

Use the browser for anything without a gateway integration.

---

## Checking what's available

```bash
pdx status
# → {"ok": true, "user_id": "...", "unique_channel": "...", "gateway": "https://..."}
```

If `ok` is false, the gateway is not configured — fall back to the browser.

---

## Running a task

```bash
pdx chat "What's on my Google Calendar today?"
# → {"ok": true, "response": "You have 3 events: standup at 9am, ..."}

pdx chat "Create a GitHub issue titled 'Bug fix' in acme/repo"
# → {"ok": true, "response": "Done — issue #42 created."}

pdx chat "Send a summary email via Gmail" --thread-id "$THREAD_TS"
```

The gateway selects the right tools automatically. One `pdx chat` call can
span multiple apps — just describe what you need.

---

## Raw HTTP proxy

Use `pdx http` to make a specific authenticated API call directly — no LLM
involved. The gateway resolves credentials from `x-ninja-user-id` +
`x-ninja-integration-channel-id` and proxies the request upstream.

```bash
# GET request
pdx http github GET https://api.github.com/user

# POST with JSON body
pdx http github POST https://api.github.com/repos/acme/repo/issues \
    --json '{"title": "Bug fix", "body": "Details here"}'

# GET with query parameters
pdx http gmail GET https://www.googleapis.com/gmail/v1/users/me/messages \
    --query maxResults=10 --query labelIds=INBOX

# POST with extra headers
pdx http hubspot POST https://api.hubapi.com/crm/v3/objects/contacts \
    --json '{"properties": {"email": "user@example.com"}}' \
    --header 'Content-Type:application/json' \
    --thread-id "$THREAD_TS"
```

Output envelope:

```json
{
  "ok": true,
  "app_slug": "github",
  "request": {"method": "GET", "url": "...", "headers": {}, "query": {}, "json": null},
  "response": {"status": 200, "headers": {...}, "body": {...}},
  "upstream_ok": true
}
```

`upstream_ok` reflects whether the upstream API returned a 2xx status. The
outer `ok` reflects whether the Pipedream Connect gateway itself succeeded.

---

## App not connected

If the user hasn't connected an app yet, the response will include a connect URL.
Post it to the user:

```bash
pdx connect-link
# → {"ok": true, "link": "https://..."}

python messaging/teams/interface.py say "Connect your apps here (expires in 30 min): <link>"
```

---

## Error handling

All failures return:

```json
{ "ok": false, "error": "<description>" }
```

| Exit code | Meaning                                                      |
| --------- | ------------------------------------------------------------ |
| `1`       | Bad arguments                                                |
| `2`       | Configuration error — `pdx status` to diagnose               |
| `3`       | Runtime error — gateway unreachable or app returned an error |

If exit code is `3` and the error mentions a connect URL, the app isn't
connected — use `pdx connect-link` to get the user a fresh link.
