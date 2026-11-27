# Symphony agent API

Symphony is a daemon that runs a coding agent against a ticket: you describe a
task and a repository, Symphony clones the repository into a sandbox, runs the
agent, posts the agent's reply as a comment, and waits. You drive it by
creating tickets, polling for the agent's reply and answering it. This API is
the machine-facing interface to Symphony's built-in tracker; it is not the
browser UI's `/api/*` endpoints and it does not touch Linear or GitHub.

All examples use `BASE` for the web UI's base URL (`http://127.0.0.1:8080` or
your `public_url`) and `$SYMPHONY_TOKEN` for the token below. Every response is
JSON except this guide, which is Markdown, and the empty CORS preflight answer
(see CORS below).

## Authentication

Set `webui.api_token` in `config.yaml` (it supports `${VAR}` expansion) and
send it on every request:

```
Authorization: Bearer $SYMPHONY_TOKEN
```

The one exception is this guide: `GET /api/v1` (and `HEAD`) needs no token, so
you can read the API's shape before anyone gives you the key. A browser's
`OPTIONS` preflight also needs no token (see CORS below). Every other request
under `/api/v1` needs the bearer token.

HTTP Basic credentials are not accepted on `/api/v1`, and the bearer token is
not accepted anywhere else in the web UI. If `webui.api_token` is unset the
whole namespace answers `404`, this guide included. A missing or wrong token on
any other `/api/v1` path answers `401` with the usual error object.

POST and PATCH always need a `Content-Length`-framed body, even when there is
nothing to pass: use `-d '{}'`. Chunked transfer encoding is not supported.

## CORS

When `webui.api_token` is set, every `/api/v1` response carries
`Access-Control-Allow-Origin: *`, so a page on another origin can call the API
from the browser. An `OPTIONS` preflight to any path under `/api/v1` gets a
`204` with no body and needs no token. It sends `Access-Control-Allow-Methods`,
`Access-Control-Allow-Headers` and `Access-Control-Max-Age: 86400`. The answer is
the same for every path, and the real request still gets the JSON `404` or
`405`. The wildcard is safe because the namespace accepts only the bearer token.
It never accepts Basic credentials or cookies, and a browser never attaches a
bearer token by itself, so another origin has no credential to borrow.

If a forward-auth proxy sits in front of the web UI, let unauthenticated
`OPTIONS` requests under `/api/v1` through to Symphony. A preflight never
carries `Authorization`, so otherwise the proxy answers it and the browser
never sends the real request.

## Errors

Every failure is an object with two strings:

```json
{
  "error": "Ticket SYM-7 cannot take a comment while it is in In Progress.",
  "hint": "Comments are accepted in Needs Input and QA only; the agent is likely still working. Poll GET /api/v1/tickets/SYM-7 until state is \"Needs Input\" or \"QA\", then post the comment again."
}
```

The hint says what to do next. When a call fails, read the hint and follow it
rather than guessing. An unknown path anywhere under `/api/v1` answers `404`
with a hint pointing back at this guide.

## States

| State | Meaning for you |
|---|---|
| `Draft` | Created but not started. The agent never sees it. Fix its fields, start it, or delete it. |
| `In Progress` | The agent is working or queued. Do not post comments; stop it if you must. |
| `Needs Input` | The agent has replied and is waiting for you. Read the last agent comment, then reply or finish the ticket. |
| `QA` | A `.symphony/serve` script from the repository is running for manual review. Comments are accepted and resume the agent, which ends the serve. |
| `Done` | Finished and archived. Reopen it if more work is needed. |

`allowed_actions` on every ticket tells you exactly what is possible now, so
you never need to encode this table yourself. It includes `start` only while
the Draft passes the same validation Start would apply (a description, a
repository, and a title unless automatic titles are configured). The names
mean:

| `allowed_actions` entry | Call |
|---|---|
| `reply` | `POST /api/v1/tickets/{ticket_id}/comments` |
| `edit` | `PATCH /api/v1/tickets/{ticket_id}` |
| `start` | `POST /api/v1/tickets/{ticket_id}/start` |
| `delete` | `DELETE /api/v1/tickets/{ticket_id}` |
| `stop`, `needs_input`, `qa`, `done`, `reopen` | `POST /api/v1/tickets/{ticket_id}/actions` with `{"action": "<name>"}` |

## The ticket object

A summary, returned by the list endpoint:

```json
{
  "id": "SYM-7",
  "title": "Add --dry-run to the importer",
  "state": "Needs Input",
  "repo": "acme/widgets",
  "model": "sonnet-4.5",
  "created_at": "2026-11-25T09:00:00+00:00",
  "updated_at": "2026-11-25T09:41:12.345678+00:00",
  "state_changed_at": "2026-11-25T09:41:12.345678+00:00",
  "allowed_actions": ["qa", "done", "reply"]
}
```

`title` falls back to the first line of the description while an automatic
title is pending. `model` is the configured alias, or `null` for the agent's
default. Timestamps are ISO-8601 with a UTC offset; compare them as strings
only after parsing, and keep the largest `updated_at` you have seen.

The detail endpoint adds the description, the daemon's view of the agent, and
every comment:

```json
{
  "id": "SYM-7",
  "title": "Add --dry-run to the importer",
  "state": "Needs Input",
  "repo": "acme/widgets",
  "model": "sonnet-4.5",
  "created_at": "2026-11-25T09:00:00+00:00",
  "updated_at": "2026-11-25T09:41:12.345678+00:00",
  "state_changed_at": "2026-11-25T09:41:12.345678+00:00",
  "allowed_actions": ["qa", "done", "reply"],
  "description": "Add a --dry-run flag to the importer and a test.",
  "agent_status": "needs_input",
  "comments": [
    {
      "id": "c-0f4a9b8e6d2c4f0e9a1b3c5d7e9f1a2b",
      "author": "agent",
      "kind": "final",
      "body": "Added the flag and one test; all tests pass.\n\nWhat next?\n\n*Symphony · final*",
      "created_at": "2026-11-25T09:41:12.345678+00:00"
    }
  ]
}
```

`agent_status` is the daemon's internal status (`bootstrapping`, `working`,
`needs_input`, `failed`) or `null` when the daemon is not tracking the ticket.
It is read-only: use the ticket `state` to decide what to do. Comments are
oldest first; `author` is `agent` for anything Symphony posted (the reply
includes the `*Symphony · ...*` footer in `body`) and `human` otherwise.
`kind` is the footer label, `null` for a human comment: `final` (possibly
`final · model: ...`), a `context: ... tokens` label (possibly with
` · model: ...`), `error`, `cleanup`, `qa`, `restart`, `workspace`,
`attachments` or `ignored`. The polling rules below use it to tell a
turn-ending comment from a notice.

## Endpoints

### `GET /api/v1`

Returns this guide as `text/markdown; charset=utf-8`.

```bash
curl -s "$BASE/api/v1" -H "Authorization: Bearer $SYMPHONY_TOKEN"
```

The response body is this document.

### `GET /api/v1/tickets`

List ticket summaries, newest `updated_at` first. Both filters are optional.

- `state`: one of `Draft`, `In Progress`, `Needs Input`, `QA`, `Done`.
- `updated_since`: an ISO-8601 timestamp. Only tickets whose `updated_at` is
  strictly greater are returned, so poll with the largest `updated_at` you
  have already seen.

```bash
curl -sG "$BASE/api/v1/tickets" \
  --data-urlencode "state=Needs Input" \
  --data-urlencode "updated_since=2026-11-25T08:00:00+00:00" \
  -H "Authorization: Bearer $SYMPHONY_TOKEN"
```

```json
{
  "tickets": [
    {
      "id": "SYM-7",
      "title": "Add --dry-run to the importer",
      "state": "Needs Input",
      "repo": "acme/widgets",
      "model": "sonnet-4.5",
      "created_at": "2026-11-25T09:00:00+00:00",
      "updated_at": "2026-11-25T09:41:12.345678+00:00",
      "state_changed_at": "2026-11-25T09:41:12.345678+00:00",
      "allowed_actions": ["qa", "done", "reply"]
    }
  ]
}
```

### `POST /api/v1/tickets`

Create a ticket. It is a Draft unless `start` is `true`.

```json
{
  "description": "Add a --dry-run flag to the importer and a test.",
  "repo": "acme/widgets",
  "title": "Add --dry-run to the importer",
  "model": "sonnet-4.5",
  "start": true
}
```

`description` is required. `title` is optional (Symphony writes one from the
description when automatic titles are configured; otherwise Start requires it).
`model` is a configured alias; omit it for the agent's default. `repo` is a
clone URL or an `owner/name` shorthand. `start` follows the editor's Start
rules: description, repository and (when automatic titles are off) title are
required, the repository is added to the managed list, and the ticket enters
In Progress.

```bash
curl -s -X POST "$BASE/api/v1/tickets" \
  -H "Authorization: Bearer $SYMPHONY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "description": "Add a --dry-run flag to the importer and a test.",
    "repo": "acme/widgets",
    "title": "Add --dry-run to the importer",
    "model": "sonnet-4.5",
    "start": true
  }'
```

`201 Created` returns the ticket detail. A started ticket looks like:

```json
{
  "id": "SYM-7",
  "title": "Add --dry-run to the importer",
  "state": "In Progress",
  "repo": "acme/widgets",
  "model": "sonnet-4.5",
  "created_at": "2026-11-25T09:00:00+00:00",
  "updated_at": "2026-11-25T09:00:00+00:00",
  "state_changed_at": "2026-11-25T09:00:00+00:00",
  "allowed_actions": ["stop"],
  "description": "Add a --dry-run flag to the importer and a test.",
  "agent_status": null,
  "comments": []
}
```

Without `"start": true`, the result has `"state": "Draft"` and
`"allowed_actions": ["edit", "start", "delete"]`. A Draft is invisible to the
agent until started.

### `GET /api/v1/tickets/{ticket_id}`

Return one ticket as the detail object above, including every comment.

```bash
curl -s "$BASE/api/v1/tickets/SYM-7" \
  -H "Authorization: Bearer $SYMPHONY_TOKEN"
```

Poll this while the agent works. A turn is ready when the state is
`Needs Input` (or `QA`) **and** there is an `agent` comment after your comment
cursor whose `kind` is turn-ending: `final` or `final · model: ...`, a
`context: ... tokens` label, `error`, `cleanup`, `qa` or `restart`. The state
alone is not enough: on
failure the daemon moves the ticket to `Needs Input` *before* it posts the
error comment, so a bare state change can show your previous reply. The other
kinds are notices: `workspace` (workspace ready), `attachments` (skipped
uploads) and `ignored` (a mid-turn comment the daemon will not read). A
`restart` comment saying "no reply needed" is a re-run notice; only the
"reply to continue" and give-up variants wait for you. The response is the
detail object shown under "The ticket object".

### `PATCH /api/v1/tickets/{ticket_id}`

Edit a Draft. Any of `title`, `description`, `repo` and `model` may be sent;
omitted fields keep their value. `null` clears a field, and `"model": null`
returns to the agent's default. Editing a started ticket answers `409`.

```bash
curl -s -X PATCH "$BASE/api/v1/tickets/SYM-7" \
  -H "Authorization: Bearer $SYMPHONY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title": "Add --dry-run and a test", "repo": "acme/widgets"}'
```

The response is the updated ticket detail, in the same shape as
`GET /api/v1/tickets/{ticket_id}`.

### `POST /api/v1/tickets/{ticket_id}/start`

Validate a Draft and start it, using exactly the editor's Start rules. The body
is ignored, but POST still needs `Content-Length`, so send `-d '{}'`.

```bash
curl -s -X POST "$BASE/api/v1/tickets/SYM-7/start" \
  -H "Authorization: Bearer $SYMPHONY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}'
```

Returns the ticket detail in `In Progress`, in the same shape as
`GET /api/v1/tickets/{ticket_id}`. Starting a non-Draft answers `409`; a
validation failure answers `400` with the reason in `error`.

### `DELETE /api/v1/tickets/{ticket_id}`

Delete a Draft, its text and its uploaded files. Its number is never reused.
Started tickets answer `409`.

```bash
curl -s -X DELETE "$BASE/api/v1/tickets/SYM-7" \
  -H "Authorization: Bearer $SYMPHONY_TOKEN"
```

```json
{"deleted": "SYM-7"}
```

### `POST /api/v1/tickets/{ticket_id}/actions`

Apply one state action from `allowed_actions`:

```bash
curl -s -X POST "$BASE/api/v1/tickets/SYM-7/actions" \
  -H "Authorization: Bearer $SYMPHONY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"action": "done"}'
```

The response is the updated ticket detail, so read the new `state` and
`allowed_actions` from it; it has the same shape as
`GET /api/v1/tickets/{ticket_id}`. `qa` starts the repository's `.symphony/serve`
script (a pure transition: it does not post a comment). `reopen` returns a Done
ticket to `Needs Input` when the daemon still tracks it, otherwise to
`In Progress`. An action that is not allowed in the current state answers
`409` with the list that is.

### `POST /api/v1/tickets/{ticket_id}/comments`

Post a reply as a human comment. The same rule as the web UI's Send applies:
comments are accepted in `Needs Input` and `QA`, and refused while the agent is
working. The call does not change the state; the daemon's resume pipeline moves
the ticket to `In Progress` on its next poll.

```bash
curl -s -X POST "$BASE/api/v1/tickets/SYM-7/comments" \
  -H "Authorization: Bearer $SYMPHONY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"body": "Please also cover the CSV path."}'
```

`201 Created` returns the stored comment:

```json
{
  "id": "c-8b1d4c6e2f0a4b3c9d7e5f1a2b4c6d8e",
  "author": "human",
  "kind": null,
  "body": "Please also cover the CSV path.",
  "created_at": "2026-11-25T09:45:00+00:00"
}
```

If the ticket is `In Progress`, this answers `409`; the hint tells you to poll
until it is `Needs Input` (or `QA`) and retry.

### `GET /api/v1/repos`

List the managed repositories. Tickets store their own repository string, so
this list is a convenience, not a constraint.

```bash
curl -s "$BASE/api/v1/repos" -H "Authorization: Bearer $SYMPHONY_TOKEN"
```

```json
{
  "repos": [
    {"url": "acme/widgets", "name": "Widgets"},
    {"url": "https://github.com/acme/gadgets.git", "name": "Gadgets"}
  ]
}
```

### `POST /api/v1/repos`

Add a repository. An empty name is derived from the URL. Adding an existing URL
is a no-op that keeps the original name.

```bash
curl -s -X POST "$BASE/api/v1/repos" \
  -H "Authorization: Bearer $SYMPHONY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"url": "acme/widgets", "name": "Widgets"}'
```

`201 Created` returns the stored entry:

```json
{"url": "acme/widgets", "name": "Widgets"}
```

### `PATCH /api/v1/repos`

Rename a managed repository's label. The URL identifies the entry and never
changes, and tickets that already use it are not touched.

```bash
curl -s -X PATCH "$BASE/api/v1/repos" \
  -H "Authorization: Bearer $SYMPHONY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"url": "acme/widgets", "name": "Widgets monorepo"}'
```

Returns the renamed entry:

```json
{"url": "acme/widgets", "name": "Widgets monorepo"}
```

An unknown URL answers `404`.

### `DELETE /api/v1/repos`

Remove a repository from the managed list. Tickets are never changed, and an
unknown URL is a no-op.

```bash
curl -s -X DELETE "$BASE/api/v1/repos?url=acme/widgets" \
  -H "Authorization: Bearer $SYMPHONY_TOKEN"
```

```json
{"deleted": "acme/widgets"}
```

### `GET /api/v1/models`

List the configured model aliases, in configuration order. Use one of these as
`model` when creating or editing a ticket; omit it for the agent's default.

```bash
curl -s "$BASE/api/v1/models" -H "Authorization: Bearer $SYMPHONY_TOKEN"
```

```json
{"models": ["sonnet-4.5", "strong"]}
```

## How to manage work

1. **Create and start a ticket.** Send `title` unless you know the server has
   automatic title generation (`webui.anthropic_api_key` set); without it,
   Start rejects a ticket that has no title.

   ```bash
   curl -s -X POST "$BASE/api/v1/tickets" \
     -H "Authorization: Bearer $SYMPHONY_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{
       "description": "Add a --dry-run flag to the importer and a test.",
       "repo": "acme/widgets",
       "title": "Add --dry-run to the importer",
       "start": true
     }'
   ```

   Keep the returned `id`, and keep a **comment cursor**: the `id` of the
   newest comment you have read so far. Start with none.

2. **Poll until there is a new agent comment.** `In Progress` means queued or
   working. `Needs Input` means a turn ended, but the ticket can be in
   `Needs Input` with only your previous reply visible for a moment: on
   failure the daemon moves the ticket there *before* it posts the error
   comment. So a turn is ready only when both hold:

   - the state is `Needs Input` (or `QA`), and
   - `comments` contains an `agent` comment that comes **after** your cursor
     in the array (comments are oldest first) whose `kind` is turn-ending:
     `final` or `final · model: ...`, a `context: ... tokens` label, `error`,
     `cleanup`, `qa` or `restart`.

   `workspace`, `attachments` and `ignored` comments are notices, not turn
   endings. A `restart` comment that says "no reply needed" is a re-run
   notice, not a turn end.

   ```bash
   curl -sG "$BASE/api/v1/tickets" \
     --data-urlencode "state=Needs Input" \
     --data-urlencode "updated_since=2026-11-25T08:00:00+00:00" \
     -H "Authorization: Bearer $SYMPHONY_TOKEN"
   ```

   Then `GET /api/v1/tickets/{ticket_id}` for the one you care about. Do not
   treat a bare state change as "the agent replied"; that stale-comment window
   is exactly what the cursor closes. A `failed` `agent_status` is still a
   finished turn: the failure is the new agent comment.

3. **Read the new agent comment** (the turn-ending one after the cursor) and
   move the cursor to its id. The `*Symphony · ...*` footer at its end is
   metadata, not part of the answer.

4. **Answer it or finish it.** If the work is incomplete, post a comment, move
   the cursor to the returned comment's `id`, and go back to step 2:

   ```bash
   curl -s -X POST "$BASE/api/v1/tickets/SYM-7/comments" \
     -H "Authorization: Bearer $SYMPHONY_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"body": "Please also cover the CSV path."}'
   ```

   If the work is done, move the ticket to `Done`:

   ```bash
   curl -s -X POST "$BASE/api/v1/tickets/SYM-7/actions" \
     -H "Authorization: Bearer $SYMPHONY_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"action": "done"}'
   ```

   `allowed_actions` is the authority at every step; if it lacks `reply` or
   `done`, follow the state it is in (and the `409` hint) instead of retrying
   the same call.
