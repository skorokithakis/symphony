# AGENTS.md

Orientation for AI agents working on this repo. Pair this with `README.md`
(end-user docs) and the source itself — the code is the source of truth.

## What this is

`symphony-lite` is a single-process Python daemon that orchestrates AI work on
Linear tickets. The loop:

1. Poll Linear for issues with the trigger label (default `agent`).
2. For each new ticket: clone the project's repo into a per-ticket workspace,
   switch to the ticket branch, optionally run `.symphony/setup`, then run
   `opencode run` inside a bubblewrap sandbox with the ticket title +
   description as the prompt.
3. Post the AI's final message as a Linear comment and transition the ticket
   to `Needs Input`.
4. When a human comments, resume the OpenCode session with the new comment as
   user input, post the result, repeat.

There is no web UI, no API, no database. State lives in `state.json` in the
workspace dir. The only external services are Linear (GraphQL) and OpenCode
(launched as a subprocess inside `bwrap`).

## Stack

- Python 3.11+, packaged with `hatchling` (see `pyproject.toml`).
- `uv` for dependency management (`uv.lock` is committed). The venv is at
  `.venv/` — use `.venv/bin/python` and `.venv/bin/pytest` directly.
- Runtime deps: `pyyaml`, `pydantic` v2, `httpx`.
- Dev deps: `pytest`.
- External binaries required at runtime: `bwrap`, `git`, `opencode`.
- CLI entry point: `symphony-lite` → `symphony_lite.cli:main`.

## Layout

```
symphony_lite/
  cli.py            argparse + wiring; loads config, builds Orchestrator, runs it
  config.py         YAML + pydantic config; ~ and ${VAR} expansion; LINEAR_API_KEY fallback
  state.py          TicketState model + StateManager (atomic JSON writes, threading.Lock)
  linear.py         GraphQL client (httpx, sync); typed exceptions; Issue/Comment/Project models
  sandbox.py        Single function: run_in_sandbox() → builds the bwrap argv and returns a Popen
  opencode.py       run_initial / run_resume; parses OpenCode's NDJSON event stream
  workspace.py      prepare() / remove(): clone, branch switch, .symphony/setup; path-containment check
  orchestrator.py   The brain: poll loop, per-ticket pipelines, ThreadPoolExecutor, cancellation
  logging.py        stderr logging setup
tests/              pytest, 154 tests, mostly unit with mocks; integration tests marked `integration`
```

The flow worth knowing: `orchestrator._tick()` is called every
`poll_interval_seconds`. It fans out work via `_schedule_task()` onto a
5-worker `ThreadPoolExecutor`, with per-ticket serialization (a ticket only
gets one task in flight at a time). Subprocesses are tracked in
`_subprocesses` so they can be killed when a ticket is cancelled (label
removed, ticket moved to terminal state, daemon shutting down).

## Key invariants and gotchas

- **TicketStatus is daemon-internal**, distinct from Linear workflow states.
  Don't conflate `TicketStatus.needs_input` (in `state.json`) with the Linear
  state named "Needs Input".
- **The daemon polls tickets in both `in_progress_state` AND `needs_input_state`**
  (see `_fetch_triggered_issues`). When a human comments on a `needs_input`
  ticket, `_resume_pipeline` transitions it back to `in_progress` itself —
  users don't need to do that manually.
- **The bot's own comments are filtered out** via the bot user id (`viewer.id`
  cached on the Linear client). New "human" comments = comments whose
  `user_id != bot_user_id`. The `bot_user_email` in the config exists for
  documentation; the actual matching is by id.
- **Path containment is a security invariant.** `workspace._check_containment`
  uses `os.path.realpath` on both sides; never bypass it.
- **State writes are atomic** (`tempfile` + `os.replace`). Don't rewrite
  `StateManager.save()` without preserving that.
- **The sandbox shares the network namespace** (the agent needs internet) but
  unshares user/pid/ipc/uts. Credential dirs (`~/.ssh`, etc.) are hidden via
  `--tmpfs` (dirs) or `--ro-bind /dev/null` (files/sockets). Git ops run
  *outside* the sandbox using the daemon's credentials; OpenCode and
  `.symphony/setup` run *inside*.
- **The OpenCode session id is captured from the first NDJSON event** that
  includes `sessionID`. The final assistant message is the concatenation of
  all `"text"` events. Other event types are intentionally ignored.
- **No auto-retry on failure.** A failed ticket goes to `TicketStatus.failed`
  and only retries if the user comments (resume path) or if there's no
  session id yet (re-runs the initial pipeline).
- **Setup errors are sticky.** `setup_error` is set when project/repo-link/
  workspace prep fails, and is cleared only when the user comments on the
  ticket. Don't clear it elsewhere.

## Running and testing

```bash
.venv/bin/pytest                              # full suite (unit + integration)
.venv/bin/pytest -m "not integration"         # unit only
.venv/bin/pytest tests/test_orchestrator.py   # one file
.venv/bin/python -m symphony_lite --validate-config --workspace <dir>
```

There is no linter configured. There is no Makefile. If you add tooling,
update this file.

Tests heavily use `unittest.mock`. Look at `tests/test_orchestrator.py` for
the patterns — fake `LinearClient`, mocked `run_initial`/`run_resume`,
`tmp_path` fixtures for state files. Integration tests under the
`integration` marker actually shell out (require `bwrap`).

## Conventions

- Code style follows what's there: explicit `from __future__ import annotations`,
  PEP 604 unions (`str | None`), module-level `logger = logging.getLogger(__name__)`,
  typed exceptions per module (`LinearError`, `OpenCodeError`, `WorkspaceError`
  hierarchies). Don't reach for new frameworks.
- Pydantic v2 models for any structured data crossing module boundaries.
- Keep `sandbox.py` and `workspace.py` boring and side-effect-explicit. They're
  the security-sensitive parts.
- Logging is to `stderr` only, via the format set in `logging.py`. Don't add
  `print()` calls.

## Knowledge and tickets

- **Gnosis (`gn` CLI)** records cross-cutting "why" knowledge. Run
  `gn help plan` before starting non-trivial work and `gn help review` after.
  Entries live in `.gnosis/entries.jsonl`. Prefer code comments when the
  context attaches to a specific line.
- **Tickets (`tk` CLI)** track work items in `.tickets/`. Statuses are
  `open`, `in_progress`, `closed` — there is no "needs input" status; that's a
  Linear concept, not a `tk` one. Use `tk ready` to see unblocked tickets and
  `tk dep tree <id>` to inspect dependencies.

## Things not to do

- Don't add `git push` from inside the sandboxed agent — credentials are
  deliberately hidden. Pushing is a human task.
- Don't widen the sandbox to bind extra host paths unless there's a clear
  reason; the credential-hiding logic depends on the current mount layout.
- Don't add retries/backoff to Linear calls without thinking through the
  poll loop — the loop itself is the retry mechanism.
- Don't change `TicketStatus` values without a migration story for existing
  `state.json` files.
