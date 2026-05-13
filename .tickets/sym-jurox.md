---
id: sym-jurox
status: closed
deps: []
links: []
created: 2026-05-13T02:13:37Z
type: task
priority: 2
assignee: Stavros Korokithakis
---
# QA reconciler: comment + transition when workspace missing

When `_reconcile_serve` picks a QA winner that has no usable workspace, today it logs a warning and silently returns — the user sees nothing happen and has no idea why.

Two silent-bail sites in `orchestrator._reconcile_serve` (currently around lines 453-461):

1. `ts = self._state.get(winner_id)` returns `None` (state entry missing — common cause: ticket was previously in a non-active state, which deletes the workspace).
2. `ts.workspace_path` is empty (state exists but workspace path was never set).

Replace both with: post a Linear comment explaining the situation, then transition the ticket to `needs_input_state`. This matches how other QA failure paths behave (`_format_serve_died_comment`, `ServeScriptMissing`).

Comment body should be specific and actionable — name the symptom and tell the user what to do next. Suggested wording (refine as you like, but keep it concrete):

> **Symphony**: Can't start QA — no workspace exists for this ticket. This usually happens after the ticket was moved out of an active state (e.g. to Done), which cleans up the workspace. Transitioning back to `<needs_input_state>`; re-trigger the agent to reclone, then move to QA again.

Use `_post_comment_safe` (consistent with the rest of the file) and follow the existing transition+log-on-failure pattern used elsewhere in `_reconcile_serve`.

**Out of scope**:
- Auto-cloning a fresh workspace on QA entry. The reason it's not in scope: the agent's commits live only in the local workspace clone (push is out of Symphony's responsibility), so a fresh clone would serve the wrong code. Deferred.
- De-duping the comment beyond the transition. The transition out of QA is the de-dup mechanism — if the user moves it back to QA without re-triggering, getting the comment again is fine.
- Anything about the "Cloning ... (branch: X)" log line in workspace.py (separate concern; not in this ticket).
