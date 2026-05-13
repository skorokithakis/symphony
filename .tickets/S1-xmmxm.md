---
id: S1-xmmxm
status: closed
deps: []
links: []
created: 2026-05-13T11:01:33Z
type: task
priority: 2
assignee: Stavros Korokithakis
---
# Process human comments on QA tickets by resuming agent

Currently the orchestrator skips ALL per-status scheduling for tickets in `qa_state` (see step 4 of `_tick` in `symphony_lite/orchestrator.py`). This means human comments left on a QA ticket are silently ignored.

Change the behavior so that a new human comment on a QA ticket triggers the normal resume pipeline:

1. In `_tick` step 4, narrow the QA-skip guard so that tickets in `qa_state` are still scheduled into `_resume_pipeline` when they have new human comments. The cleanest shape is to drop the early `continue` for QA tickets and let `_resume_pipeline` do its normal early-return-on-no-new-comments check. (Step 2's QA skip should remain — new-ticket / setup-error / failed-no-session retries do not apply to QA tickets.)

2. No changes needed to `_resume_pipeline` itself. It already transitions the ticket to `in_progress_state` at the start and `needs_input_state` at the end. Because the ticket leaves `qa_state`, the existing `_reconcile_serve` logic on the next tick will kill the active serve.

3. Update the AGENTS.md note that says 'Comments on a ticket in `qa_state` are **not** processed by the agent; the per-status loop in `_tick` skips any ticket whose Linear state is `qa_state`.' to describe the new behavior: a human comment on a QA ticket bumps it to `in_progress_state`, runs the resume pipeline, ends in `needs_input_state`, and kills the serve as a side effect of leaving QA.

4. Update the README 'Manual QA' section that currently says 'The agent doesn't process comments while a ticket is in QA — move it out of QA (or remove the trigger label) to resume the conversation.' Replace with a description matching the new behavior: commenting on a QA ticket pulls it out of QA into in_progress, kills the serve, runs the agent, and lands in needs_input. To retest, move the ticket back to QA.

Caveats / non-goals:

- Do NOT add any new state or persistence around QA + agent interaction. The serve is killed by existing reconcile logic on the next tick.
- Do NOT change the cancellation guards in `_resume_pipeline` or `_new_ticket_pipeline` (the 'cancelled before final transition — skipping' blocks). They remain correct.
- Do NOT touch `_reconcile_serve`. It already kills the serve when its owner leaves QA.
- Do NOT modify the bot-comment filtering — existing `user_id != bot_user_id` logic is what we want.
- Brief overlap window: between scheduling the resume task and the serve being killed on the next tick, the serve may still be running while the agent is spinning up. This is acceptable; do not engineer around it.

## Acceptance Criteria

- A human comment on a ticket in `qa_state` causes the resume pipeline to run on the next poll, transitioning the ticket to `in_progress_state` then `needs_input_state`, with the serve killed by existing reconcile logic.
- A QA ticket with no new human comments is untouched: serve stays running, no agent turn fires.
- Bot's own comments still do not trigger a resume.
- AGENTS.md and README updated to describe the new behavior.
- Tests in `tests/test_orchestrator.py` cover (a) QA ticket with new human comment triggers resume, (b) QA ticket with no new comments does nothing, (c) QA ticket with only a bot comment does nothing.
