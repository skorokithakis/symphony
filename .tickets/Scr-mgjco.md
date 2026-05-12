---
id: Scr-mgjco
status: closed
deps: [Scr-ufxmg, Scr-tdhvp, Scr-hesei]
links: []
created: 2026-05-12T13:28:23Z
type: task
priority: 2
assignee: Stavros Korokithakis
---
# Orchestrator: poll loop, per-ticket lifecycle, concurrency, error handling

The daemon's main loop. Polls Linear, manages per-ticket state machines, runs OpenCode turns concurrently in a small thread pool.

Main loop:
- Every `poll_interval_seconds`:
  1. Fetch issues with the trigger label in active states (`In Progress`, `Needs Input`).
  2. For each fetched ticket not already running and not in local state: schedule a 'new ticket' task.
  3. For each ticket in local state: check label still present AND state not terminal. If label removed → kill its task, drop from state. If state is terminal (Done/Cancelled/Canceled/Duplicate) → kill task, drop state, `remove` workspace.
  4. For each ticket in state with status `needs_input` not currently running: schedule a 'resume' task that checks for new comments and runs a turn if any.

Per-ticket task (runs in a thread; only one task active per ticket at a time):

NEW TICKET pipeline:
  1. Get project, find link labeled 'Repo' (case-insensitive). If absent: post error comment, do not save state, return.
  2. Workspace.prepare(identifier, repo_url, branch). If fails: post error comment, return.
  3. Transition to `In Progress`.
  4. Post metadata comment placeholder, capture comment id.
  5. Build prompt: short header explaining this is a Linear ticket and the bot relays replies, then `{title}\n\n{description}`.
  6. OpenCode.run_initial(...) — register Popen with task so the kill-handler can find it.
  7. Edit metadata comment to include session id + workspace path.
  8. Post final assistant message as a new comment, capture its id as `last_seen_comment_id`.
  9. Transition to `Needs Input`.
  10. Persist state (status `needs_input`).

RESUME pipeline:
  1. Fetch comments since `last_seen_comment_id`. Filter out the bot's own comments.
  2. If none: return.
  3. Concatenate with timestamps and authors as one user message.
  4. Transition to `In Progress`.
  5. OpenCode.run_resume(...).
  6. Post final message, update `last_seen_comment_id` to the just-posted comment id.
  7. Transition to `Needs Input`.
  8. Persist state.

Error handling within tasks:
- Clone/setup failure: post error, leave no state behind (so user can retry by re-triggering).
- OpenCode failure/timeout/cancellation: post error comment with brief message; leave ticket in `In Progress` so it's visible; persist state with status `failed` (orchestrator should not auto-retry; user-initiated comment will resume on next tick).
- Linear API failure: don't post anything (we couldn't anyway); let the task crash and be retried next tick. Log clearly.

Subprocess management:
- Each task tracks its current Popen via a shared map keyed by ticket id.
- Label-removal handler issues SIGKILL to the Popen and waits briefly for exit, then joins/discards the task.
- On daemon shutdown (SIGTERM/SIGINT): kill all subprocesses, persist state, exit cleanly.

Concurrency:
- ThreadPoolExecutor sized for ~5 workers (handles 1-3 concurrent tickets comfortably).
- Per-ticket re-entry guard so the same ticket isn't scheduled twice.
- State writes serialized via lock.

Startup recovery:
- Load state. For each ticket in state, no running subprocesses exist (daemon was restarted). Just continue polling — the next tick will pick up any `Needs Input` tickets via the normal resume path.

Out of scope: webhooks, hot config reload, mid-turn interrupts, auto-retry on agent failure, dashboards.

## Acceptance Criteria

Labeling a Linear ticket `agent` triggers the full new-ticket pipeline within ~poll-interval and ends with the ticket in `Needs Input` with a comment from the bot. Replying triggers the resume pipeline. Removing the label kills the subprocess within one poll cycle. Moving the ticket to `Done` cleans up the workspace. Multiple replies before the daemon polls are concatenated into one user turn. Two or three concurrent tickets work without crosstalk.

