---
id: sym-qwvip
status: closed
deps: [sym-vxacj, sym-gmoah]
links: []
created: 2026-05-13T01:05:50Z
type: task
priority: 2
assignee: Stavros Korokithakis
---
# Orchestrator: QA-state serve lifecycle reconciliation

Add the runtime that turns 'ticket is in qa_state' into 'global singleton .symphony/serve is running for that ticket', and tears it down when the ticket leaves QA.

Scope:
- symphony_lite/orchestrator.py.
- One new helper method (_reconcile_serve) called from _tick after pipeline scheduling, before the per-status step.
- Touches: _tick, _cancel_ticket, _shutdown_handler, the per-status loop, _resume_pipeline gating.

State held on the Orchestrator (in-memory, not persisted):
  self._active_serve: tuple[ticket_id, subprocess.Popen[bytes], start_monotonic] | None
  self._serve_lock: threading.Lock

Reconciliation algorithm, given the list of issues fetched this tick:
  qa_tickets = [i for i in issues if qa_state is configured and i.state == qa_state]
  qa_ids = {i.id for i in qa_tickets}
  active_id = self._active_serve[0] if self._active_serve else None

  1. If active_id is set and active_id is not in qa_ids: kill the active Popen and clear _active_serve. (Owner left QA.)
  2. If qa_tickets is non-empty:
     - If active_id is in qa_ids: keep current winner (do not switch on ties to avoid thrash).
     - Else: winner = max(qa_tickets, key=lambda i: i.updated_at or i.identifier). Start serve for winner (see below), set _active_serve.
     - For every QA ticket that is not the winner: post a bot comment 'Bumped out of QA — <winner identifier> took over' and transition them to needs_input_state. Catch and log LinearError on each; do not abort the loop.

Starting a serve:
  - Call workspace.start_serve(workspace_path, hide_paths, extra_rw_paths).
  - If ServeScriptMissing or WorkspaceError raised: post a Linear comment on the ticket with the error, log, do NOT touch ticket status, do NOT set _active_serve.
  - On success: set _active_serve to (ticket_id, proc, time.monotonic()). Spawn a daemon thread that does proc.wait(timeout=10): if it returns with rc != 0 within the window, drain stderr tail, post a Linear comment 'QA serve failed (rc=N):\n' on the ticket and clear _active_serve (under _serve_lock). If timeout hits, exit the thread quietly (serve is healthy). If it exits with rc == 0, treat as silent done and clear _active_serve.

Cancellation / cleanup integration:
  - _cancel_ticket(tid): if _active_serve owner is tid, kill it and clear (under _serve_lock). Use the same kill pattern as _subprocesses.
  - _shutdown_handler: kill the active serve (if any) alongside the other subprocesses, before the executor shutdown.

Resume pipeline gating:
  - In _tick's per-status loop, when scheduling _resume_pipeline for a ticket with status == needs_input, skip if the latest fetched issue's state is qa_state. (Use issues_by_id to look up.) This prevents the agent waking up on comments while the human is QA'ing.
  - The bumped-out tickets we transitioned to needs_input above will resume normally on the NEXT tick (their Linear state has changed), which is fine.

Edge cases to handle:
- qa_state unset: _reconcile_serve is a no-op. No state poll changes (those are in the dependency ticket).
- Daemon restart with a ticket still in qa_state: _active_serve is None on startup; the first tick sees the ticket in QA, picks it as winner, starts the serve. No special restart code needed.
- Ticket disappears from Linear (404) while owning the serve: existing cleanup in _tick step 3 calls _cancel_ticket(tid), which will now also kill the serve. Verify this path is correct.
- The winner ticket fails the serve script: comment posted, _active_serve stays None, next tick will retry (and fail again). Acceptable per agreement.

Non-goals:
- No persistence of serve state across restarts.
- No port allocation, URL surfacing, no env var injection.
- No queueing or fairness between QA tickets — newest wins, others get kicked.
- No retry suppression for repeatedly-failing serve scripts.

Tests (unit, with fake LinearClient and mocked workspace.start_serve):
- qa_state unset: no behavior change.
- Single ticket transitions into QA: serve started, _active_serve populated.
- Owner leaves QA: serve killed, _active_serve cleared.
- Second ticket enters QA while one is already active: older keeps serving (or newest takes over per algorithm; verify chosen behavior); the loser is transitioned to needs_input with a bump comment.
- Comments on a QA-state ticket do not trigger _resume_pipeline.
- start_serve raises ServeScriptMissing: comment posted on the ticket, _active_serve unchanged.
- 10s watchdog: simulate proc exiting non-zero quickly → bot comment posted; simulate proc surviving past 10s → no comment.
- _cancel_ticket on the serve owner kills the serve.
- Daemon shutdown kills the serve.

## Design

Why in-memory _active_serve rather than persisted? The serve dies with the daemon (--die-with-parent), so persistence would be misleading. State is reconstructed from 'ticket is in qa_state' on the next tick. Simpler and correct.

Why post the bump comment after picking the winner, not before? We want the winner's serve to be running before anyone is told they've been bumped, so the QA flow is uninterrupted from the reviewer's perspective.

Why tick-time reconciliation rather than event-driven? Polling is already the daemon's mode of operation. Reusing it keeps the design uniform. The cost is up to one poll interval of latency on entering QA, which is acceptable for a manual-QA feature.

## Acceptance Criteria

All the listed scenarios pass as unit tests. The full test suite (.venv/bin/pytest) passes. The feature is a no-op when qa_state is unset.


## Notes

**2026-05-13T01:06:05Z**

Clarification: the 'QA serve failed' comment body should be a markdown fenced code block containing the stderr tail (last ~20 lines), formatted like the existing setup_error comments — e.g. 'QA serve failed (rc=N):' followed by a triple-backtick fence around the stderr tail. The earlier description got shell-mangled on creation.

**2026-05-13T01:08:34Z**

Caveats from dependency completion:

1. Pipe-buffer deadlock: start_serve() returns a Popen with stdout=PIPE and stderr=PIPE. The 10s watchdog reads stderr on early failure; after the watchdog exits (serve healthy past 10s), nothing is draining the pipes, and a chatty serve will eventually block on write. Solution: after the 10s watchdog decides 'healthy', either redirect both pipes to /dev/null (close+reopen is awkward), or spawn a long-lived drainer thread that reads-and-discards stdout/stderr until proc exits. Drainer thread is simpler. Choose whichever.

2. workspace.start_serve also raises FileNotFoundError when bwrap is missing on PATH (propagated from run_in_sandbox). Catch alongside WorkspaceError/ServeScriptMissing and post the same kind of Linear failure comment.

3. qa_state lives at config.linear.qa_state (already what the ticket assumes — confirming).

**2026-05-13T01:14:22Z**

Two corrections after initial implementation review:

CORRECTION 1 — Dead-proc stuck state (bug in initial implementation):
At the top of _reconcile_serve, before any other logic, check if _active_serve is set AND _active_serve[1].poll() is not None. If so, the serve process has exited (cleanly or otherwise) without anyone noticing — clear _active_serve. Then proceed with the rest of the algorithm normally. This prevents the daemon from getting stuck thinking a dead process is the active serve.

CORRECTION 2 — Wrong winner semantics on contention (bug in T3 spec):
The original spec said 'If active_id is in qa_ids: keep current winner (do not switch on ties to avoid thrash).' This contradicts the agreed semantic that 'newest entry into QA wins, incumbent gets kicked'. Correct algorithm:

    winner = max(qa_tickets, key=lambda i: i.updated_at)
    if winner.id != active_id:
        kill active serve (if any), clear
        start serve for winner, set _active_serve
    for loser in qa_tickets if loser.id != winner.id:
        post bump comment, transition to needs_input

This requires adding updated_at to the Issue model AND the GraphQL query in linear.py. Add it as a datetime field (alias 'updatedAt') alongside the existing 'archivedAt' alias. Update the GraphQL fragment in list_triggered_issues and get_issue accordingly.

Thrash check: a kicked loser gets transitioned to needs_input — their Linear state changes, so they no longer appear in qa_tickets next tick. No oscillation possible.

**2026-05-13T01:20:07Z**

CORRECTION 3 — Cancel in-flight agent work when ticket enters QA:

Race: a ticket can be moved to qa_state while its agent pipeline is still running (OpenCode subprocess mid-flight, or between final message and transition). The agent currently completes its run and then calls transition_to_state(tid, needs_input_state), clobbering the human's QA move. Also two processes (agent + serve) end up writing to the same workspace.

Fix:

(a) In _reconcile_serve, BEFORE calling start_serve for the winner, check whether winner.id is currently in self._active_tasks (under self._task_lock). If yes, call self._cancel_ticket(winner.id) and proceed. _cancel_ticket kills the in-flight OpenCode subprocess and sets the cancellation flag; the pipeline will bail at its next _is_cancelled check or via the OpenCodeCancelled exception. Do not block waiting for the task to actually exit — the cancellation flag is sufficient to prevent further state changes.

(b) Add one more 'if self._is_cancelled(tid): return' check in _new_ticket_pipeline AND _resume_pipeline, immediately before the final transition_to_state(tid, needs_input_state) call (the one that fires after _post_final_message). This closes the small window where the agent finishes OpenCode successfully, posts the final comment, but then gets cancelled before transitioning — without this check the transition would clobber the QA state.

Tests:
- New unit test: ticket has in-flight task (simulated by inserting into _active_tasks and _subprocesses), human moves to QA, _reconcile_serve cancels the task and starts the serve. Verify _cancel_ticket was called and start_serve was called.
- New unit test (or extension): pipeline is past the OpenCode run, _is_cancelled returns True before final transition → no transition_to_state call is made.

**2026-05-13T01:22:51Z**

CORRECTION 4 — Extend QA gate to ALL per-status scheduling:

Correction 3 added a gate that prevents _resume_pipeline from running for a ticket whose Linear state is qa_state. But step 4 of _tick also schedules _recover_working_ticket (for status=working) and another _resume_pipeline (for status=failed with session_id). Neither is gated. Both call transition_to_state(needs_input_state), which clobbers the human's QA move.

Concrete failure path (the dev's 'Notable decision 2' rationale is incorrect):
1. Agent is mid-flight. Human moves ticket to qa_state.
2. Reconcile cancels the agent, starts serve. Ticket's internal status remains 'working'.
3. The agent task exits (OpenCodeCancelled). _task_wrapper clears the _cancelled flag.
4. Next tick: ticket is in qa_state (we added qa_state to the triggered states, so it's still polled). Step 4 sees status=working → schedules _recover_working_ticket → posts 'Daemon restarted...' comment + transition_to_state(needs_input_state). QA state clobbered.

Fix: at the top of step 4's per-ticket loop, look up the ticket in issues_by_id (we have it already). If its state is qa_state, 'continue' — skip ALL per-status scheduling for that ticket. The serve is handled in step 3.5; nothing else should fire while the human is QA'ing. Remove the now-redundant gate added in Correction 3 (the new outer gate subsumes it).

Same fix applies if step 2 (new-ticket pipeline) ever runs for a ticket landing directly in qa_state with the trigger label — exotic, but for consistency: skip _new_ticket_pipeline scheduling if the issue's state is qa_state. The reviewer can flip the ticket to in_progress if they want the agent to run.

Tests:
- Ticket with status=working AND linear_state=qa_state: _recover_working_ticket is NOT scheduled.
- Ticket with status=failed + session_id AND linear_state=qa_state: _resume_pipeline is NOT scheduled.
- New issue arriving in qa_state with trigger label: _new_ticket_pipeline is NOT scheduled.
