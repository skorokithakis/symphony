# Web UI

Symphony can also be its own tracker. With the `webui:` block there is no
Linear or GitHub account involved: tickets are stored as JSON files under
`<workspace>/tickets/`, and a small web UI served by the daemon is the front
end.

Exactly one backend is active per daemon. Web UI tickets are not visible in
Linear or GitHub, and Linear or GitHub tickets are not visible in the web UI.
To use the web UI beside an existing Linear or GitHub daemon, run a second
daemon with its own workspace directory and its own port.

```yaml
webui:
  port: 8080
  host: 127.0.0.1          # default
  password: change-me
  # Optional. Enables automatic titles and reply summaries (see Titles and
  # summaries below).
  # anthropic_api_key: ${ANTHROPIC_API_KEY}
  # Optional. Base URL for the Messages API, e.g. an Anthropic-compatible
  # gateway (see Titles and summaries below).
  # anthropic_base_url: https://anthropic-gateway.example.com
  # Optional. Soniox API key enabling microphone dictation in the reply and
  # ticket-text boxes (see Dictation below).
  # soniox_api_key: ${SONIOX_API_KEY}
  # Optional. Bearer token for the JSON API at /api/v1, for an external agent
  # (see Agent API below). Unset disables the API: /api/v1 then answers 404.
  # api_token: ${SYMPHONY_WEBUI_API_TOKEN}
  # Optional. Public base URL of this UI, sent as the contact claim of a
  # Web Push notification's token (see Notifications below).
  # public_url: https://symphony.example.com
```

Open `http://127.0.0.1:8080/`. The browser asks for a username and the
password once, using HTTP Basic auth; there is no login page. The username is
ignored (any value works, including an empty one), so only the password
matters. There is no `username` setting: older configs that still have
`webui.username` fail to load until you delete that line. Served over HTTPS, the UI can be installed as an app and then opens
standalone.

With HTTPS (or on localhost), the header shows a bell that subscribes this
browser to Web Push notifications for tickets that reach Needs Input; it hides
itself once the browser has subscribed, and the ticket page's notification is
suppressed while you are looking at it.

**Put HTTPS in front of the port before exposing it.** Basic auth sends the
password with every request, so plain HTTP is only reasonable on localhost.
Keep `host` on `127.0.0.1` and terminate TLS in a reverse proxy (nginx, Caddy,
Traefik, and so on).

The proxy must forward the original `Host` header: the server's same-origin
check compares it with the browser's `Origin`. Caddy and Traefik do this by
default; with nginx, set `proxy_set_header Host $http_host` (not `$host`,
which drops a non-default port and breaks every write) and
`proxy_set_header X-Forwarded-Proto $scheme` next to it, so the daemon knows
the browser is on TLS. `X-Forwarded-Proto: https` tells it to refuse posts
that come from the plain-http origin of the same host name. No other
forwarded header is trusted.

A proxy login service that adds Basic credentials after its own login works
without any Symphony change. For example, Tinyauth can inject an
`Authorization: Basic ...` header from its `basic-auth` response labels, and
the daemon sees an ordinary authenticated request.

## Tickets and states

Tickets are in one of five fixed states:

| State | What it means | Actions |
|---|---|---|
| Draft | an idea in the editor; nothing has run | Start, Discard |
| In Progress | the agent is working | Stop |
| Needs Input | the agent has replied and is waiting for you | Send, Send to QA, Done, Retry (after a failed turn) |
| QA | your `.symphony/serve` script is running | Send, Back to Needs Input, Done |
| Done | finished and archived | Reopen |

- **Start** validates the editor and moves the Draft to In Progress. The
  daemon picks it up on the next poll tick. Start requires the ticket text and
  a repository; a title too, unless automatic titles are enabled.
- **Send** posts the composer's text as a human comment and wakes the daemon.
  It does not change the state itself: the daemon resumes the session on the
  next tick and moves the ticket to In Progress, exactly as it does for a
  comment on Linear or GitHub.
- **Stop** moves an In Progress ticket back to Needs Input. Any in-flight turn
  is killed on the next tick.
- **Send to QA** moves a Needs Input ticket to QA without posting the composer
  text. If the repo has a `.symphony/serve` script, the daemon starts it, as
  described under [Manual QA](configuration.md#manual-qa); QA is always enabled for this
  backend.
- **Done** stops tracking the ticket and deletes its workspace. If the
  workspace has uncommitted or unpushed work, the daemon refuses the first
  time: it posts a comment telling you what is at risk and moves the ticket
  back to Needs Input. Moving it to Done a second time deletes the workspace.
- **Reopen** moves a Done ticket back to work. If the daemon still tracks the
  ticket, it goes to Needs Input, so a reply resumes the session through the
  normal path. If it does not (the cleanup tick already ran), it goes to In
  Progress: the daemon's new-ticket pipeline picks it up and itself ends in
  Needs Input, with or without a session snapshot. On that path Send is
  unavailable until the first turn ends.
- **Discard** deletes a Draft, its text and its uploaded files. Its number is
  never reused.

The editor autosaves a Draft on the server as you type. A ticket only comes
into existence once the editor has some content or a file is attached, so an
abandoned empty form leaves nothing behind.

## Reply composer

Every started ticket except a Done one has a reply box. It stays editable while
the agent works, and its text is saved on the server, so a reload or another
device sees the same draft. Only Send is unavailable while the ticket is In
Progress; the page says so next to the disabled button and re-enables it when
the turn finishes. In QA, sending resumes the agent, which pulls the ticket out
of QA and stops the serve on the next tick.

When a turn fails, or the ticket's workspace setup does, the ticket returns to
Needs Input with an error comment and the composer grows a **Retry** button.
Retry posts the fixed comment `Retry.`, which is exactly what resumes the
agent; it deliberately does not use the composer's text or its pending
attachments, so the reply you are writing stays where it is. A failed turn is
never retried on its own, so Retry is the quick path when nothing needs to
change in your instructions.

The composer's **Model** dropdown lists Default plus the aliases under the
top-level `models:` map (the same control the editor has, and it is absent when
no aliases are configured). The choice is sent with Send and with Retry and
stored on the ticket, so it applies from the next turn; changing it does not
interrupt a turn that is already running, and like Send it is unavailable while
the agent works. The Details panel shows the model currently stored on the
ticket.

## Repositories

The editor's Repository field is a dropdown of the repositories you have
stored, managed from the **Repositories** page (`/repos`, also linked from the
editor). Each entry has a name and a clone URL; the ticket stores the URL
exactly as before, and the name is only what the dropdown shows. On a new
ticket the dropdown starts at the repository of the most recent ticket that is
still in the list, or at the first entry.

To add one while writing a ticket, choose **Add a repository…** and type the
URL (and optionally a name); Start adds it to the list. Saving or autosaving
the Draft only stores the URL on the ticket, so the list is never filled with
the partial URLs typed along the way; on reload the Draft shows its repository
as an extra option. Removing an entry from `/repos` never changes tickets that
already use it. The list is stored in `<workspace>/tickets/repos.json`, so the
sandbox's workspace masking keeps it out of the agent's reach.

## Models

The editor's Model dropdown lists the aliases under the top-level `models:`
map. What you pick is stored on the ticket and presented to the orchestrator
as a `Model: <alias>` label, so the usual model resolution applies (see
[Model override](configuration.md#model-override)). Choosing "Default" uses the agent's own
default. The reply composer offers the same dropdown on a started ticket, so
the model can be changed with a Send or a Retry; see [Reply
composer](#reply-composer). The stored model only affects turns that start
after the change.

## Attachments

The editor and the reply box accept any file type, up to 50 MB each. PNG,
JPEG, GIF and WebP files are passed to the agent as image input. Every other
file is downloaded into the ticket's `attachments/` directory, which the
sandbox sees at `/tmp/symphony-attachments`, and the agent opens it with its
own tools. The daemon's existing per-message total of 50 MB applies: uploads
beyond it are skipped and the daemon posts a "skipped attachments" comment
naming them.

Only the four image types are ever served inline. Every other upload downloads
as an attachment; an HTML or SVG file served inline from the UI's origin would
run with your credentials.

## Titles and summaries

With `webui.anthropic_api_key` set, the editor hides the Title field. Start
stores an empty title and asks the Anthropic Messages API for one in the
background, using a model id fixed in `symphony_linear/anthropic.py`. Until
it arrives, and if the request fails, the first line of the ticket text is
shown as the title. Nothing is retried and no error is shown; the fallback just
stays.

Without the key, the editor shows the Title field and Start refuses an empty
one.

The same key also turns on one-line summaries of agent replies. When the
daemon posts a reply, a background request asks for a single line saying what
the agent did and what it needs from you next, and that line appears under the
title on the ticket's row in the Needs Input list. It is absent while the
request runs, if it fails, and on any other list or page: the ticket page
already shows the full reply. As with titles, nothing is retried, no error is
shown, and neither the prompt nor the model is configurable.

`webui.anthropic_base_url` points both requests at an Anthropic-compatible
gateway or proxy instead of `https://api.anthropic.com`. It takes the same
form as the SDK's `ANTHROPIC_BASE_URL`: everything before `/v1`, since
`/v1/messages` is always appended. The gateway must accept the `x-api-key`
header and the fixed model id. The key is still the on/off switch; setting
only the base URL does nothing.

## Dictation

With `webui.soniox_api_key` set, the ticket reply box and the editor's ticket
text box get a microphone button. It records from the microphone and streams
the audio to [Soniox](https://soniox.com)'s real-time Speech-to-Text API,
inserting the recognised words at the cursor as they arrive. Click it again to
stop: the last confirmed words stay in the box and the button returns to its
idle state.

The long-lived key stays on the daemon. Each recording first asks
`POST /api/soniox-key` for a single-use temporary key that has 60 seconds in
which to open the connection, then the browser opens the WebSocket directly to
`wss://stt-rt.soniox.com`; audio is not proxied through Symphony. The route
uses the same Basic auth and same-origin check as every other page and is not
registered at all without the key, and the Content-Security-Policy allows the
Soniox WebSocket origin only when dictation is configured. The key needs the
**Temporary API keys** permission and the real-time speech-to-text permission.

The button stays hidden when dictation is not configured, when the page is not
on HTTPS (or localhost), when the browser has no microphone API, or when it
cannot record a format Soniox accepts. Dictation needs JavaScript; without it
both text boxes work as before.

## Notifications

Web Push is always on for the web UI backend and needs no configuration. The
header's bell subscribes the current browser; it needs HTTPS (or localhost),
because push subscriptions only work in a secure context. Once subscribed, the
daemon sends one browser notification each time a ticket actually changes to
Needs Input on its own initiative: when a turn ends, when a turn fails, when a
cleanup is refused because the workspace has uncommitted changes, and when a QA
serve dies. Only those daemon-driven transitions notify: moving a ticket to
Needs Input yourself with the web UI's Stop or Needs Input action sends nothing,
and neither does a transition that changes nothing (a ticket already in Needs
Input). The notification is suppressed only while that ticket's page is being
watched: the tab is visible, the window has focus, and you interacted with it in
the last 10 minutes. It is never suppressed by looking at another page.

```yaml
webui:
  # ... the rest of the webui block
  public_url: https://symphony.example.com   # optional
```

`public_url` is sent as the contact (`sub`) claim of the notification's VAPID
token. It does not change where a click goes: the notification opens the ticket
page relative to wherever this UI is served. Tapping the notification focuses an
already-open tab at that ticket's page when there is one, and otherwise opens a
new tab.

The notification's body is only the ticket's identifier and title (for example
`SYM-4: Fix the frobnicator`); it never contains comment or reply text.
Delivery is best-effort: a failure is one WARNING in the daemon log, and never
affects the turn. The icon is the copy of `static/icon.png` in the public
Symphony GitHub repository, because a notification's icon is fetched without
the page's Basic credentials.

## Agent API

An external agent can drive the built-in tracker over HTTP instead of using
the browser. Set `webui.api_token` and point it at `<public_url>/api/v1` (or
`http://127.0.0.1:<port>/api/v1`):

```yaml
webui:
  # ... the rest of the webui block
  api_token: ${SYMPHONY_WEBUI_API_TOKEN}
```

```bash
curl -s "https://symphony.example.com/api/v1" \
  -H "Authorization: Bearer $SYMPHONY_WEBUI_API_TOKEN"
```

`GET /api/v1` returns the whole guide as Markdown: what the states mean, every
endpoint with a `curl` example and a sample response, and the polling loop for
creating a ticket, waiting for the agent's reply and answering it. That one
request needs no token, so an agent can read the guide before it has the key;
every other `/api/v1` path requires it. The token lives in `config.yaml`,
which the sandbox masks from coding agents; Basic credentials are not accepted
on `/api/v1`, and the bearer token is not accepted anywhere else. Leaving
`api_token` unset disables the API: the whole namespace answers `404`, the
guide included. Anyone with the token can start coding agents on any saved
repository, so treat it like the Basic password.

What an agent can do:

- **Tickets:** list them (filtered by `state` and `updated_since`), read one
  with its comments, create one as a Draft or started straight away, edit,
  start or delete a Draft, stop it, send it to QA, mark it Done, reopen it,
  and reply to the agent.
- **Repositories:** list, add, rename (the name only, never the URL) and
  remove.
- **Models:** list the aliases configured under `models:`.

The API is built to be used by an agent that has read nothing but the guide:

- Every ticket carries an `allowed_actions` list, so the caller never needs to
  know the state machine. `start` only appears when Start would succeed.
- Every error is `{"error": "...", "hint": "..."}`, and the hint says what to
  do next, for example to wait until the ticket is in Needs Input before
  replying.
- Every agent comment has a `kind` taken from its footer (`final`, `error`,
  `workspace` and so on). The guide tells the caller to wait for a
  turn-ending comment newer than the last one it saw, rather than trusting the
  latest comment: when a turn fails, the ticket moves to Needs Input before the
  error comment is posted.
- A reply sent over the API leaves your unsent browser composer text and its
  pending attachments alone.

Known limits:

- A retried reply can post twice. The browser's Send has a client token that
  prevents this; the API does not.
- There is no pagination: a list returns every ticket and a ticket returns all
  of its comments.
- File uploads are not available over the API.
