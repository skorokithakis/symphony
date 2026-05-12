---
id: sym-xgrdb
status: open
deps: []
links: []
created: 2026-05-12T19:39:17Z
type: task
priority: 2
assignee: Stavros Korokithakis
---
# Avoid duplicate metadata comment and re-run when daemon restarts mid-bootstrap

Recovery in Orchestrator._recover_state drops any ticket found in
TicketStatus.bootstrapping. But the new-ticket pipeline posts the
\"**Symphony** workspace: ... session: pending\" metadata comment and saves
metadata_comment_id to state *while status is still bootstrapping* (lines
~444-454 in orchestrator.py). If the daemon dies between metadata-post
and the status=working flip, restart wipes state, the next poll sees the
ticket as new, and the whole pipeline runs again — producing a duplicate
metadata comment and a duplicate initial reply.

Fix: in _recover_state, when a bootstrapping ticket has metadata_comment_id
set, edit that comment via LinearClient.edit_comment to a clear
restart-notice message, then remove the state. When metadata_comment_id is
not set, keep current behaviour (drop state silently — no Linear
side-effects have happened). The label is still on the ticket, so the
next poll tick will naturally re-run the new-ticket pipeline; the user
does nothing.

Suggested replacement body for the edited comment:
\"**Symphony**: Restarted before setup completed. Picking this ticket up
again on the next poll.\"

Edge case to acknowledge in a comment (no need to handle): if the daemon
dies between Linear acknowledging the metadata post and the state.save()
that records metadata_comment_id, the orphaned comment will stay
un-edited and a fresh one will appear on retry. This window is tiny and
not worth additional machinery.

Add unit tests in tests/test_orchestrator.py covering:
- bootstrapping ticket with metadata_comment_id set: edit_comment called
  with the new body, state removed.
- bootstrapping ticket with metadata_comment_id None: no Linear call,
  state removed (existing behaviour).
- edit_comment failure during recovery is logged but does not crash and
  still removes state.

Out of scope: changing the order of operations in _new_ticket_pipeline
itself; introducing a new ticket status; closing the
post-comment-then-save race.

## Acceptance Criteria

After daemon restart mid-bootstrap, the previously-posted metadata comment is edited in place rather than duplicated, state is removed, and the next poll resumes the pipeline cleanly. New unit tests pass; full suite passes.

