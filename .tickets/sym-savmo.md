---
id: sym-savmo
status: closed
deps: []
links: []
created: 2026-05-12T19:39:06Z
type: task
priority: 2
assignee: Stavros Korokithakis
---
# Fix list_comments_since silently dropping newest comments

list_comments_since in symphony_lite/linear.py queries comments without an
explicit orderBy. Linear's default for the comments connection is
descending (newest first), so the returned list is in the opposite order
from what the function assumes. The position-based slice
all_comments[i+1:] therefore returns comments *older* than the reference,
not newer, and any new human comment on a needs_input ticket is silently
dropped — the resume pipeline never fires.

Fix: add orderBy: createdAt to the GraphQL query and reverse the returned
nodes in Python so the function returns a chronologically-ascending list,
matching its docstring and existing tests. Apply the same orderBy to the
comments(first: 50) query in get_issue for consistency (it isn't used in
the bug path today, but keeping them in sync prevents the same trap
later).

Add a unit test that simulates the realistic Linear response order
(newest first) and asserts that list_comments_since(id, last_seen) returns
strictly newer comments. Existing tests assume oldest-first input nodes
and should keep passing once the reversal is in place — verify and update
their fixtures if needed so the input represents what Linear actually
returns.

Out of scope: switching to timestamp-based comparison; refactoring the
pagination or "comment_id missing" behaviour.

## Acceptance Criteria

After the fix, a Linear response with [newest, ..., oldest] yields ascending output, and list_comments_since(id, last_seen_id) returns only the comments newer than last_seen_id. Full unit suite passes.
