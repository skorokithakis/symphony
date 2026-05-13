---
id: sym-cbdis
status: closed
deps: []
links: []
created: 2026-05-13T01:30:47Z
type: task
priority: 2
assignee: Stavros Korokithakis
---
# QA serve: post-review fixes (bug bundle)

Fix five issues found in code review of the QA-serve feature. All in symphony_lite/orchestrator.py unless noted.

Restructure _active_serve from a tuple into a small mutable container (dataclass or NamedTuple-with-mutables, your call) so we can carry per-serve metadata:

    @dataclass
    class _ActiveServe:
        ticket_id: str
        ticket_identifier: str   # for comment formatting
        proc: subprocess.Popen[bytes]
        start_monotonic: float
        stdout_head: bytearray   # first ~1000 bytes captured by drainer
        stderr_head: bytearray   # first ~1000 bytes captured by drainer
        intentional_kill: threading.Event   # set when we (not the script) ended it

Then the fixes:

FIX 1 — Watchdog suppresses comment on intentional kill.
Wherever we kill the serve ourselves (_kill_active_serve, _cancel_ticket, _shutdown_handler, the winner-change path in _reconcile_serve, the dead-proc path), set _active_serve.intentional_kill before calling proc.kill(). In the watchdog, after proc.wait returns with non-zero rc, check intentional_kill.is_set() — if yes, log and exit silently; do not post a Linear comment. Don't rely on negative returncode (signal numbers vary, may not always be negative).

FIX 2 — Cancelled agent must not post final comment or edit metadata.
In _new_ticket_pipeline and _resume_pipeline, add 'if self._is_cancelled(tid): return' guards immediately before BOTH:
  - the meta_comment edit_comment call (in _new_ticket_pipeline only — _resume_pipeline doesn't edit metadata)
  - the _post_final_message call (in both pipelines)
The existing pre-transition guard stays. This closes the hole where a cancelled agent finishes OpenCode just before the SIGKILL lands and still writes to Linear.

FIX 3 — Bump comment must not spam on persistent transition failure.
In _reconcile_serve's loser loop, reverse the order: call transition_to_state(loser, needs_input_state) FIRST. Only if it succeeds, post the bump comment. If the transition fails (LinearError), log and skip the comment — the loser is still in qa_state and will be re-attempted next tick, but we won't accumulate duplicate comments.

FIX 4 — Drainer pipe-deadlock fixes (both stdout/stderr and watchdog stderr.read).
Replace the current sequential drainer with two daemon threads (one per pipe) that:
  - read in chunks (e.g. 4096 bytes)
  - append to the corresponding stdout_head / stderr_head bytearray on _active_serve, but stop appending once 1000 bytes have been captured (still keep reading and discarding to drain the pipe; just don't grow the buffer past 1000)
  - exit on EOF
Start both drainer threads immediately after start_serve returns — not after the 10s watchdog window. The watchdog can then read from _active_serve.stderr_head when it needs the tail (no proc.stderr.read() call from watchdog). This eliminates both the stdout-blocks-stderr deadlock and the watchdog-blocks-on-grandchild deadlock.

For the <10s failure case where the watchdog needs the stderr tail: after proc.wait returns non-zero (and intentional_kill is not set), the watchdog reads stderr_head from _active_serve. To handle the case where the drainer hasn't yet captured enough output, sleep briefly (e.g. 200ms) before reading the buffer — gives the drainer time to drain the post-mortem flush. Use a lock or just accept that bytearray reads are racy and tolerate it (worst case: empty or partial tail in the comment).

FIX 5 — Dead-proc detection in _reconcile_serve posts a 'serve died' comment and transitions to needs_input.
Currently the dead-proc check at top of _reconcile_serve silently clears _active_serve, causing a respawn-on-every-tick loop if the serve keeps dying. Replace the silent clear with:

  if self._active_serve and self._active_serve.proc.poll() is not None:
      av = self._active_serve
      rc = av.proc.returncode
      stdout_tail = bytes(av.stdout_head).decode(errors='replace')
      stderr_tail = bytes(av.stderr_head).decode(errors='replace')
      body = format_serve_died_comment(rc, stdout_tail, stderr_tail)  # see format below
      try:
          self._linear.post_comment(av.ticket_id, body)
      except Exception:
          logger.exception(...)
      try:
          self._linear.transition_to_state(av.ticket_id, self._config.linear.needs_input_state)
      except Exception:
          logger.exception(...)
      # Prevent re-serve within the same tick: drop this ticket from qa_tickets
      # so the algorithm doesn't pick it again.
      qa_tickets = [t for t in qa_tickets if t.id != av.ticket_id]
      qa_ids = {t.id for t in qa_tickets}
      self._active_serve = None

Don't set intentional_kill here — the proc is already dead.

If the watchdog had already posted a <10s-failure comment for the same proc, we'd be double-commenting. Prevent this: when the watchdog posts its failure comment, set a flag on _active_serve (e.g. failure_comment_posted: bool) and have the dead-proc path check it; if already posted, just transition + clear without re-posting.

Comment format (single body):

    **Symphony**: QA serve exited (rc={rc}). Transitioning ticket back to Needs Input — re-enter QA to retry.

    **stdout** (first 1000 chars):
    

    **stderr** (first 1000 chars):
    

Cap each tail at 1000 chars (already bounded by the drainer buffer, but defense-in-depth).

Tests to add:
- Intentional kill (any path) → watchdog does NOT post a failure comment.
- Real OpenCode-style cancel race: agent task is mid-flight, reconcile cancels and starts serve; the agent's run_initial returns just after the kill but before the new guards — verify edit_comment and _post_final_message are NOT called.
- transition_to_state on loser fails → no bump comment posted; succeeds → bump comment posted.
- Drainer captures output without blocking: simulate a fake Popen with stdout/stderr pipes that emit chatty output past 1000 bytes; verify stdout_head/stderr_head are capped at 1000 and the proc isn't blocked.
- Dead-proc path: proc exits cleanly (rc=0) post-10s while still in QA → comment posted, ticket transitioned to needs_input, qa_tickets locally pruned, _active_serve cleared, no re-serve this tick.
- Dead-proc deduplication with watchdog: <10s non-zero failure → watchdog posts comment, sets failure_comment_posted; next tick's dead-proc check transitions without re-posting.

Non-goals:
- Fancier retry suppression beyond what's spec'd.
- Bigger refactor of the cancellation model. Keep _cancel_ticket as-is.

## Acceptance Criteria

All five fixes implemented. New unit tests cover each fix. .venv/bin/pytest passes (unit + integration).


## Notes

**2026-05-13T01:30:57Z**

Comment format clarification (got shell-mangled). The body should be a single Linear comment string built like:

    **Symphony**: QA serve exited (rc=<RC>). Transitioning ticket back to Needs Input — re-enter QA to retry.

    **stdout** (first 1000 chars):
    <triple-backtick fence>
    <captured stdout, or '(empty)'>
    <triple-backtick fence>

    **stderr** (first 1000 chars):
    <triple-backtick fence>
    <captured stderr, or '(empty)'>
    <triple-backtick fence>

Use Python f-strings or .format() in the code as you like.
