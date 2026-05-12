---
id: sym-lzres
status: closed
deps: []
links: []
created: 2026-05-12T22:46:08Z
type: chore
priority: 2
assignee: Stavros Korokithakis
---
# Unify ticket cleanup: workspace + state removed whenever ticket isn't triggered

Replace the three separate cleanup branches in `orchestrator._tick` (step 3) with one rule:

A tracked ticket should be cleaned up (cancel in-flight subprocesses, remove state entry, remove workspace) whenever it is not currently *triggered*. Triggered means all of:
- trigger label present
- Linear state in {in_progress_state, needs_input_state}
- not archived
- not deleted

Anything else fires cleanup, including:
- label removed (currently keeps the workspace — change to also delete it)
- Linear state in terminal set (Done/Cancelled/Canceled/Duplicate — already deletes)
- Linear state in any other non-active state, e.g. Backlog/Todo, with the label still on (currently dead weight — should delete)
- archived (`archivedAt != null`) — currently no handling at all
- deleted (`LinearNotFoundError`) — already deletes

Scope:
- `symphony_lite/orchestrator.py`: collapse the cleanup branches in step 3 of `_tick` into one predicate; drop the 'label removed = keep workspace' carve-out.
- `symphony_lite/linear.py`: add `archived_at: datetime | None` to the `Issue` model and select `archivedAt` in the GraphQL queries that hydrate `Issue` (at minimum the single-issue `issue(id:)` query; the list query already excludes archived by default and that's fine).
- Tests: cover the four cleanup paths (label removed, archived, non-active non-terminal state with label still on, ticket deleted). Terminal-state path already has coverage — adapt if needed.

Non-goals:
- Do NOT add `includeArchived: true` to the list query. Archived tickets fall out of the list naturally and are caught by the `get_issue` lookup path.
- Do NOT touch `workspace.prepare()` or the resume pipeline. `prepare()` is already idempotent and creates the workspace from scratch if missing; the resume path will never see a missing workspace because state-entry-exists ⟺ workspace-exists once this rule is in place.
- Do NOT change `_fetch_triggered_issues`.

Caveats:
- The current comment in `_tick` explaining 'label removal is reversible, leave the workspace for re-trigger' becomes obsolete — remove it. Re-triggering after label removal is now a fresh `_new_ticket_pipeline` run that reclones. This is intentional and acceptable: clones are cheap and the prior session state was deleted alongside the workspace anyway.
- Update `AGENTS.md` to reflect the new cleanup invariant (state entry exists ⟺ workspace exists; cleanup fires whenever the ticket isn't triggered). The current 'Key invariants and gotchas' section discusses related lifecycle details; keep that style.

## Design

The cleanup predicate in step 3 of `_tick` should be expressible as a single helper, e.g. `_is_still_triggered(issue) -> bool`, returning True iff label present AND state in active states AND `archived_at` is None. The outer loop then becomes: for each tracked ticket, look it up; if `LinearNotFoundError` or `not _is_still_triggered(issue)`, run cleanup; else continue.

The state-entry-exists ⟺ workspace-exists invariant is what makes the resume path safe without defensive prepare() calls. It must hold at every cleanup site.

## Acceptance Criteria

- A triggered ticket whose label is removed has its workspace deleted (not just its state entry).
- A triggered ticket that is archived in Linear (without being moved to a terminal state) has its workspace and state entry deleted.
- A triggered ticket moved to a non-active, non-terminal Linear state (e.g. Backlog) while keeping the label has its workspace and state entry deleted.
- Existing terminal-state and ticket-deleted cleanup paths still work.

