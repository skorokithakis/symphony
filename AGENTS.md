# AGENTS.md

Orientation for AI agents working on this repo. Pair this with `README.md`
(end-user docs) and the source itself — the code is the source of truth.

## What this is

`symphony-linear` is a single-process Python daemon that orchestrates AI work on
Linear or GitHub issues (one tracker backend and coding-agent CLI per daemon
instance). The loop:

1. Poll the tracker for issues with the trigger signal (label or project
   field, depending on backend).
2. For each new issue: clone the project's repo into a per-ticket workspace,
   switch to the ticket branch, optionally run `.symphony/setup`, then run
   the configured coding-agent CLI inside a bubblewrap sandbox with the ticket
   title + description as the prompt.
3. Post the AI's final message as a comment and transition the ticket
   to the configured "needs input" state.
4. When a human comments, resume the coding-agent session with the new comment
   as user input, post the result, repeat.

There is no web UI, no API, no database. State lives in `state.json` in the
workspace dir. The only external services are the issue tracker (Linear or
GitHub, accessed via GraphQL) and the selected coding agent (launched as a
subprocess inside `bwrap`).

## Stack

- Python 3.11+, packaged with `hatchling` (see `pyproject.toml`).
- `uv` for dependency management (`uv.lock` is committed). The venv is at
  `.venv/` — use `.venv/bin/python` and `.venv/bin/pytest` directly.
- Runtime deps: `pyyaml`, `pydantic` v2, `httpx`.
- Dev deps: `pytest`.
- External binaries required at runtime: `bwrap`, `git`, and the selected
  coding-agent CLI (`opencode`, `omp`, or `pi`).
- CLI entry point: `symphony` → `symphony_linear.cli:main`.
- Backends: the orchestrator talks to a `Tracker` protocol (see `tracker.py`).
  Concrete implementations exist for Linear (`linear_tracker.py`) and
  GitHub Projects v2 (`github_tracker.py`). Exactly one backend is active
  per daemon, selected at config time.
- Coding-agent adapters: OpenCode (`opencode.py`) and the pi family: OMP
  (`omp.py`) and pi (`pi.py`), sharing `pi_protocol.py`. Exactly one
  coding-agent CLI is active per daemon, selected at config time.

## Layout

```
symphony_linear/
  cli.py            argparse + wiring; loads config, builds Orchestrator, runs it
  config.py         YAML + pydantic config; ~ and ${VAR} expansion; token fallback
  state.py          TicketState model + StateManager (atomic JSON writes, threading.Lock)
  tracker.py        Tracker-neutral protocol, errors, and enums (the seam for orchestrator)
  linear.py         Linear GraphQL client (httpx, sync); typed exceptions; Issue/Comment/Project models
  linear_tracker.py Linear backend adapter implementing the Tracker protocol
  github.py         GitHub GraphQL client (httpx, sync); typed exceptions
  github_tracker.py GitHub Projects v2 backend adapter implementing the Tracker protocol
  sandbox.py        Single function: run_in_sandbox() → builds the bwrap argv and returns a Popen
  agent_runner.py   Shared sandbox process runner; drains pipes and enforces turn watchdogs
  opencode.py       OpenCode adapter: run_initial / run_resume; parses OpenCode NDJSON
  pi_protocol.py    Shared pi-family JSON parser for OMP and pi
  omp.py            OMP adapter: run_initial / run_resume; shared pi-family parser
  pi.py             pi adapter: run_initial / run_resume; shared pi-family parser
  workspace.py      prepare() / remove(): clone, branch switch, .symphony/setup; path-containment check
  orchestrator.py   The brain: poll loop, per-ticket pipelines, ThreadPoolExecutor, cancellation
  webhook.py        Optional webhook receiver; wakes poll loop on Linear updates. Absent config means polling only.
  logging.py        stderr logging setup
tests/              pytest, mostly unit with mocks; integration tests marked `integration` (shell out to `bwrap`/`git` — they never call the real `opencode`, `omp`, or `pi` binary or any LLM)
```

The flow worth knowing: `orchestrator._tick()` is called every
`poll_interval_seconds`. It fans out work via `_schedule_task()` onto a
5-worker `ThreadPoolExecutor`, with per-ticket serialization (a ticket only
gets one task in flight at a time). Subprocesses are tracked in
`_subprocesses` so they can be killed when a ticket is cleaned up (daemon
shutting down, or the ticket is no longer triggered — see `_is_still_triggered`).

## Key invariants and gotchas

- **TicketStatus is daemon-internal**, distinct from Linear workflow states.
  Don't conflate `TicketStatus.needs_input` (in `state.json`) with the Linear
  state named "Needs Input". The same applies to GitHub: the daemon's internal
  status is separate from the project's Status field value.
- **The daemon polls tickets in `in_progress_state`, `needs_input_state`, and
  (if configured) `qa_state`** (see `_fetch_triggered_issues`). When a human
  comments on a `needs_input` ticket, `_resume_pipeline` transitions it back
  to `in_progress` itself — users don't need to do that manually. Human
  comments on a ticket in `qa_state` also trigger the normal resume pipeline:
  the ticket is transitioned to `in_progress_state`, the agent runs, the
  ticket lands in `needs_input_state`, and the existing `_reconcile_serve`
  logic kills the active serve on the next tick because its owner left QA. This
  still holds when a ticket enters QA mid-turn: `_reconcile_serve` cancels the
  in-flight turn and, for a `working` or `bootstrapping` state entry, parks the
  daemon status at `TicketStatus.needs_input` with one `qa`-kind
  reply-to-continue comment while deliberately leaving the tracker ticket in
  QA. Tick step 4 repairs a stale `working` + in-QA entry to
  `TicketStatus.needs_input` instead of skipping it. The repair starts no
  turn, so the normal comment-gated resume pipeline runs on the following tick
  when a session id is available; without one, the existing fresh-start branch
  handles the reply.
- **The bot's own comments are filtered out** via a visible Markdown footer
  of the form `*Symphony · {kind}*` appended at the tracker-adapter layer
  from a `kind` string supplied by the caller (e.g. `"workspace"`, `"final"`,
  `"error"`, `"context: 37,074 tokens"`). Detection uses the helper
  `is_bot_comment(body)` in `tracker.py`, which checks for the substring
  `*Symphony · ` (middle dot is U+00B7). New "human" comments = comments
  whose body does *not* contain the marker. There is no longer a separate
  bot identity; the daemon can run with the user's personal API token or a
  dedicated bot account — it doesn't care. The `bot_user_email` config field
  has been removed.
- **State entry exists ⟹ workspace exists (not the reverse).** `orchestrator._tick`
  step 3 fires cleanup (cancel subprocesses, remove state entry, remove workspace)
  whenever a tracked ticket is no longer *triggered* — i.e. the trigger label
  is absent, the Linear state is no longer an active state, the ticket is
  archived, or the ticket was deleted. With `linear.trigger_label: null`, the
  trigger instead requires an active state and a project `Repo` external link,
  with its label compared case-insensitively after stripping whitespace.
  **Dirty workspaces are never deleted without a second move.** On the first
  cleanup of a dirty workspace (per
  `workspace.dirty_summary`), the daemon refuses: it posts a comment, transitions
  the ticket back to Needs Input, and sets `TicketState.cleanup_refused_state`
  to the workflow state the ticket was left in (Needs Input if the transition
  succeeded, the state it was already in if it failed). A later cleanup deletes
  (rmtree) only when the ticket has moved away from that recorded state; if the
  ticket is still in it — human never moved it again, or the refusal transition
  failed — the state entry is dropped but the dirty directory is kept, so a
  workspace may exist with no state entry, and a re-triggered ticket then
  reuses that directory via `clone_workspace`'s fetch-or-reuse path. Ticket
  deletion is the exception: the `TrackerNotFoundError` branch cleans up
  unconditionally, dirty or not. `cleanup_refused_state` is cleared
  (set to `None`) when the agent next takes a turn (the pipelines do this
  alongside `TicketStatus.working`), so new work re-arms the guard. A re-trigger
  of the same ticket after cleanup is handled as a fresh `_new_ticket_pipeline`.
- **Path containment is a security invariant.** `workspace._check_containment`
  uses `os.path.realpath` on both sides; never bypass it.
- **State writes are atomic** (`tempfile` + `os.replace`). Don't rewrite
  `StateManager.save()` without preserving that.
- **The sandbox shares the network namespace** (the agent needs internet) but
  unshares user/pid/ipc/uts. Credential dirs (`~/.ssh`, etc.) are hidden via
  `--tmpfs` (dirs) or `--ro-bind /dev/null` (files/sockets). The per-ticket
  `tmp/` dir is bind-mounted read-write at `/tmp` inside the sandbox —
  it is per-ticket, disk-backed, and deleted with the ticket dir.
  `sandbox.dir_map` (sandbox path → host source) adds `--bind` mounts AFTER
  the hide block so an explicit mapping punches through a broad hide:
  relative sources live under the per-ticket `mounts/` dir (deleted with the
  ticket), absolute sources are shared host dirs that survive cleanup and are
  writable by up to 5 concurrent workers. Resolution, containment, and
  `mkdir -p` of both sides happen in `workspace.ensure_dir_map` (bwrap cannot
  create mount points under the read-only root bind); `run_in_sandbox` only
  emits argv from the resolved pairs. Git ops run
  *outside* the sandbox using the daemon's credentials; the selected coding
  agent and `.symphony/setup` run *inside*. OpenCode's session directories and
  the pi family's `~/.omp` and `~/.pi` are deliberate read-write binds. OMP's
  auth vault and run/daemons directory both live under `~/.omp`;
  `PI_CODING_AGENT_DIR` does not redirect OMP's `run/` path. pi's state lives
  under `~/.pi/agent/` and its credential vault is separate from OMP's —
  authenticating one does not authenticate the other. Upstream pi honours
  `PI_CODING_AGENT_DIR`, but it has no effect here: `run_in_sandbox` passes
  `--clearenv` and the adapters set only `HOME` (plus `PATH`), so a sandboxed
  pi always resolves its state to `~/.pi`, which is why that exact path is the
  one bound read-write.
- **The OpenCode session id is captured from the first NDJSON event** that
  includes `sessionID`; that value is the main session and any event whose
  top-level `sessionID` differs is subagent chatter. The final assistant
  message is assembled differently per path: a *successful* turn is trimmed
  to the closing reply (`_assemble_final_reply`: subagent-session events are
  dropped, only `"text"` segments after the last `tool_use` event are kept,
  and an empty result falls back to the full assembly), while the *timeout*
  path keeps the full `_assemble_message` trace — every `"text"` segment
  plus one `*tool title*` line per tool call — because it is the only
  diagnostic on a killed turn. Other event types are intentionally ignored.
  `AgentCancelled` carries a parsed session id when available, and the
  orchestrator salvages it (plus the current agent tag) only into a
  still-present state entry with no session id, without changing its status.
  Thus a killed first turn with a later reply resumes instead of restarting.
  The pi family (OMP and pi) emits a `session` event as its first stdout line
  before any model work, so its id is normally available immediately.
  OpenCode's id first appears on the first event carrying `sessionID`
  (`step_start`); a kill before that produces `None` and follows the existing
  fresh-start path.
- **pi is OMP's upstream and their JSON protocols are byte-compatible.**
  `omp.py` and `pi.py` share `pi_protocol.py`; only their argv differs. Do not
  split the parser.
- **The pi family namespaces sessions by cwd path**, under
  `~/.omp/agent/sessions/<slugified-cwd>/` for OMP and
  `~/.pi/agent/sessions/<slugified-cwd>/` for pi, exactly like OpenCode.
  Moving a repo path therefore breaks resume for all agents; retain the
  per-ticket workspace path across turns.
- **The pi family's prompts are unconditionally prefixed with a newline.** Any
  message argv beginning with `@` is an attachment, so OMP or pi exits 1 with
  "File not found" for ticket text that starts with `@developer`. The prefix
  is applied in both paths even when the prompt already starts with a newline.
- **pi resumes with `--session <id>`, never `-r <id>`.** In pi, `-r` is a
  boolean session *picker* and does not take a value. Measured behaviour of
  `pi -p --mode json -r <id>`: pi opens the interactive picker, writes zero
  bytes to stdout, and exits 0. Under the daemon that yields no `session`
  event at all, so the turn either fails the missing-session-id check or
  stalls until the idle watchdog kills it. `--session <id>` is the only
  resume flag that works; it is verified to carry context across turns.
- **pi must always use `--no-approve`; never pass `-a` or `--approve`.** An
  interactive trust approval inherited from global settings would let a
  checked-out repo load extensions, skills, prompts, and themes;
  `--no-approve` makes daemon behavior deterministic.
- **`OPENCODE_PERMISSION` is injected into the sandbox env for every turn** to
  pre-answer the three permissions that default to `ask` (external_directory,
  doom_loop, read); do not delete it as redundant with
  `--dangerously-skip-permissions` — that flag auto-approves only top-level
  session events, so a subagent permission ask would hang the turn forever.
- **OMP and pi need no `OPENCODE_PERMISSION` equivalent.** OMP's
  `--auto-approve` setting lets a task-tool subagent run to completion; pi has
  no tool-approval gate, so the OpenCode subagent-permission hang does not
  reproduce.
- **Turns have an idle watchdog on top of the absolute timeout.** `_execute`
  drains stdout/stderr with one thread per stream and kills the process when
  neither produced output for `turn_idle_timeout_seconds` (default 1200s) or
  after `turn_timeout_seconds` in total; the tracker comment says which limit
  fired (`AgentTimeout.reason`). Load-bearing for OpenCode: `--print-logs` in both
  `run_initial` and `run_resume` mirrors OpenCode's internal log to stderr —
  the parent's NDJSON stdout is silent while a subagent task runs, so without
  the flag stdout alone is no liveness signal. Do not filter out the hourly
  `cleanup prune` log line; it resets the watchdog, and that is accepted.
  OMP and pi need no `--print-logs` equivalent: both emit
  `tool_execution_update` roughly once a second on the parent stream while a
  subagent runs, so stdout alone is a real liveness signal.
- **Do not trust pi-family JSON-mode exit codes for mid-turn failures.**
  `_validate_final_turn` must inspect the last `turn_end`'s
  `message.stopReason` because auto-retry emits several; treat `error` and
  `aborted` as failures and surface `message.errorMessage`. pi and OMP can exit
  0 after an API failure. Check `returncode < 0` first so a signal kill remains
  a cancellation.
- **Sessions are tagged with the agent that created them.** `TicketState.agent`
  and `SessionRecord.agent` persist the tag; `None` is a pre-change record and
  reads as `opencode`. Because session IDs are adapter-specific, a mismatch
  with the configured agent discards the live session and durable snapshot and
  starts a fresh session rather than attempting a resume.
- **Restart recovery re-runs the interrupted turn, capped.** When the daemon
  restarts (or a pipeline thread dies) mid-turn, the ticket stays in
  `TicketStatus.working` and tick step 4 calls `_rerun_interrupted_turn`,
  which re-runs the interrupted turn instead of bouncing the ticket to Needs
  Input: with a session id and pending human comments it resumes the session;
  without a session id it restarts the initial pipeline (carrying any pending
  comments into the prompt); with a session id but nothing pending it parks
  the ticket in `needs_input` with a "reply to continue" comment. Each re-run
  increments `TicketState.interrupted_turns` (persisted before the re-run
  starts), and after `_MAX_INTERRUPTED_TURNS` (3) consecutive interruptions
  the daemon posts a give-up comment, advances `last_seen_comment_id`
  best-effort, and parks the ticket in Needs Input without resetting the
  counter. The give-up advance moves `last_seen_comment_id` past the
  triggering comment, so that comment is deliberately not replayed — the
  human must reply again. The counter is reset to 0 only when a turn starts
  from genuinely new input (the pipelines do this alongside
  `status = working`), and the recovery re-run only fires for tickets that are
  still in the tick's trigger list and not cleanup-refused, so untriggered
  tickets and dirty-workspace refusals are left alone.
- **Mid-turn comments are explicitly discarded, not queued.** A human
  comment posted while a turn is genuinely running (a task is in flight in
  `_active_tasks`) is never consumed by that turn, so tick step 4 posts one
  "I did not read this" notice; the following ticks stay silent. The
  warning is read-only with respect to persisted state: it never touches
  `last_seen_comment_id` or state.json, because that id is the anchor
  `_rerun_interrupted_turn` replays from after a restart — a turn that
  completes normally already advances it past the warned comment via its
  end-of-turn write, so a mid-turn write is redundant, and in the sequence
  resume-consumes-H1 / human-posts-H2 / daemon-restarts it would leave
  recovery nothing to replay (parking the ticket with "reply to continue"
  instead of replaying H1 and H2). The in-flight lookup
  (`_is_task_in_flight`, same `_active_tasks` map and `_task_lock` that
  `_schedule_task` dedups with) is the signal, not the `working` status: a
  ticket stuck in `working` with no task in flight is an interrupted turn,
  and `_rerun_interrupted_turn` must still receive the pending comments as
  replay input — warning there would swallow them. The check sits after the
  untriggered / cleanup-refused / in-QA guards, which never warn, and is
  re-run after the warning's comment fetch: a turn that finishes while that
  fetch blocks gets no notice (its final reply already went out). "Pending"
  is measured from the per-turn `_turn_input_marker` (in-memory, guarded by
  `_task_lock`; key present = the running turn's input is fixed), never from
  `last_seen_comment_id` — no pipeline advances that marker until the turn
  ends, so measuring from it would accuse the comment that *started* the
  turn. Each pipeline writes the marker when it fixes its input
  (`_new_ticket_pipeline` and the replay path take a fresh
  baseline via `_baseline_comment_id`; the normal resume path reuses the
  newest id of the comments it already fetched), and a warning advances the
  marker past the comments it warned about, so each comment is warned at
  most once per turn; `_task_wrapper` pops it with the task. Tracker errors
  are swallowed: a failed comment fetch leaves the markers untouched and the
  next tick retries, while a failed warning post still advances the turn
  marker — a failing post must not cause per-tick spam. Presence-gating
  is deliberate: while the key is absent (input not yet fixed), a pending
  comment cannot be told apart from the triggering one, so the daemon stays
  silent — better a silent discard in that window than a false accusation.
  Timestamps were rejected because tracker server time versus daemon local
  time makes a comment posted moments before the turn starts exactly the one
  that misclassifies. The marker is in-memory by design: on restart the map
  is empty, nothing warns, and recovery behaves exactly as it does today.
  Accepted consequence of the read-only warning: on a turn path that returns
  without an end-of-turn `last_seen_comment_id` write (`AgentCancelled` or an
  `_is_cancelled` early return), a warned comment remains pending. In the QA
  case, `_reconcile_serve` parks the cancelled `working`/`bootstrapping` entry
  at `TicketStatus.needs_input` without touching `last_seen_comment_id`, so
  the next turn on a human reply consumes the pending comments. An untriggered
  ticket is left alone; preserving restart recovery is worth more than
  suppressing that late read.
- **No auto-retry on failure.** A failed ticket goes to `TicketStatus.failed`
  and only retries if the user comments (resume path) or if there's no
  session id yet (re-runs the initial pipeline). The internal status and the
  tracker state are deliberately decoupled: every failure path that ends in
  `TicketStatus.failed` with a tracker comment also transitions the tracker
  ticket to its Needs Input state — before posting the error comment (a
  comment without a state change self-amplifies through the webhook) and only
  when the ticket is not cancelled (a human may have moved it to QA
  mid-turn) — via `_transition_failed_to_needs_input`. The internal status
  stays `failed` so the retry routes above keep working. Interrupted turns
  are the exception: they are re-run by the restart-recovery invariant above.
- **Setup errors are sticky.** `setup_error` is set when project/repo-link/
  workspace prep fails, and is cleared only when the user comments on the
  ticket. Don't clear it elsewhere.
- **QA serve is a global singleton, in-memory only.** When `linear.qa_state`
  (or `github.qa_status`) is configured and a ticket enters that state,
  `_reconcile_serve` runs the repo's `.symphony/serve` script in the sandbox
  and stores the Popen in `Orchestrator._active_serve` (an `_ActiveServe`
  dataclass). At most one serve runs across the whole daemon. The newest QA
  entrant always wins — incumbents are killed and bumped back to
  `needs_input_state`. When a winner has an in-flight agent turn,
  `_reconcile_serve` cancels it before starting the serve. For a `working` or
  `bootstrapping` state entry, the cancellation parks the internal status at
  `TicketStatus.needs_input` and posts one `qa`-kind comment telling the human
  to reply to continue; it deliberately leaves the tracker ticket in QA.
  Nothing about the serve is persisted to `state.json`; on daemon restart the
  reconciliation loop sees the ticket still in `qa_state` and relaunches the
  serve naturally. A serve that dies (within or after the 10s watchdog window)
  gets a tracker comment with the rc and a stdout/stderr tail, and the ticket
  is transitioned back to `needs_input_state` to avoid a respawn loop — except
  clean exits within 10s, which are silent (the script is assumed to have
  daemonized a child).
- **Model override via `Model: <value>` labels, two tiers.** Resolved per
  turn from the freshly fetched issue by `model_for_issue` in `tracker.py`:
  the issue's own labels first, then `issue.project.labels` (a `None`
  project leaves only the issue tier), so precedence is issue label >
  project label > the agent's own default. Both tiers go through the same
  `model_from_labels` (case-insensitive prefix match; value looked up in the
  top-level `models:` alias map case-insensitively, passed through verbatim
  on a miss), so alias handling and the empty-value / multiple-label
  warnings behave identically either way. The resolved id becomes `--model`
  for the primary agent's turns, initial and resume; subagents are
  unaffected. Nothing is persisted — changing or removing either label takes
  effect on the next turn. The final-comment footer kind appends
  `· model: <id>` when overridden (the middle dot is the U+00B7 footer
  separator, so `is_bot_comment` still matches); it does not say which tier
  won. Each configured alias is provisioned on Linear at startup alongside
  the trigger label as *two* labels, one per namespace: `IssueLabel` and
  `ProjectLabel` are unrelated objects and neither applies where the other
  belongs (`provision_model_labels`, no state caching — unlike the trigger
  label it re-checks every start, so two API calls per alias per start).
  The labels must be flat and named exactly `Model: <alias>`: a Linear label
  *group* named `Model` with children such as `Strong` does not work,
  because Linear reports only the child's own name in every
  `labels { nodes { name } }` selection, at either tier. That is reachable
  only by hand-creating a group; the provisioned ones are flat.
  The GitHub backend ignores the list and has no project tier
  at all — Projects v2 has no project labels (`ProjectV2` has no `labels`
  field and does not implement `Labelable`), and its issue labels are
  per-repository while a project spans repos.

## Running and testing

```bash
.venv/bin/pytest                              # full suite (unit + integration)
.venv/bin/pytest -m "not integration"         # unit only
.venv/bin/pytest tests/test_orchestrator.py   # one file
.venv/bin/python -m symphony_linear --validate-config --workspace <dir>
```

A `pre-commit` config runs `ruff` (lint + format) and `mypy` on every commit,
so there's no need to invoke them manually. There is no Makefile. If you add
tooling, update this file.

Tests heavily use `unittest.mock`. Look at `tests/test_orchestrator.py` for
the patterns — fake `LinearClient`, mocked `run_initial`/`run_resume`,
`tmp_path` fixtures for state files. Integration tests under the
`integration` marker shell out to `bwrap` and `git`; they do **not** invoke
the real `opencode`, `omp` or `pi` binary or any LLM. Don't add tests that do —
they're flaky (model nondeterminism), costly (API calls), and exercise the
agent's behaviour, not ours. The NDJSON parsers are unit-tested against
fixtures.

## Conventions

- This repo is managed with jj (jujutsu): git HEAD is detached by design.
  Don't try to "fix" it or expect a checked-out branch.
- Code style follows what's there: explicit `from __future__ import annotations`,
  PEP 604 unions (`str | None`), module-level `logger = logging.getLogger(__name__)`,
  typed exceptions per module (`LinearError`, `OpenCodeError`, `WorkspaceError`
  hierarchies). Don't reach for new frameworks.
- Pydantic v2 models for any structured data crossing module boundaries.
- Keep `sandbox.py` and `workspace.py` boring and side-effect-explicit. They're
  the security-sensitive parts.
- Logging is to `stderr` only, via the format set in `logging.py`. Don't add
  `print()` calls.
- Commit messages MUST follow Conventional Commits (https://www.conventionalcommits.org/),
  with the description starting with a capital letter (e.g. `feat: Add new
  feature`, not `feat: add new feature`). Common prefixes: `feat:`, `fix:`,
  `chore:`, `docs:`, `refactor:`, `test:`, `ci:`, `build:`, `perf:`, `style:`.
  Breaking changes: append `!` after type (`feat!: ...`) or add
  `BREAKING CHANGE:` footer. Release Please uses these to compute the next
  version — `fix:` → patch, `feat:` → minor, breaking → major.

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

- Don't push from inside the sandboxed agent unless the user explicitly asked
  for it. Credentials (`~/.ssh`, `~/.config/gh`, etc.) are hidden by default,
  so pushing requires the user to opt in by unhiding those paths via config.
- Don't widen the sandbox to bind extra host paths unless there's a clear
  reason; the credential-hiding logic depends on the current mount layout.
- Don't add retries/backoff to tracker API calls without thinking through the
  poll loop — the loop itself is the retry mechanism.
- Don't change `TicketStatus` values without a migration story for existing
  `state.json` files.
