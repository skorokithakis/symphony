---
id: Scr-ufxmg
status: closed
deps: [Scr-kdcga]
links: []
created: 2026-05-12T13:27:33Z
type: task
priority: 2
assignee: Stavros Korokithakis
---
# Linear API client

Thin wrapper around Linear's GraphQL API. Synchronous is fine (we run per-ticket tasks in threads).

Operations needed:
- `list_triggered_issues(label, active_states) -> list[Issue]`: tickets with the trigger label currently in any active state.
- `get_issue(issue_id) -> Issue`: state name, labels, branch_name field, project (id+name), all comments.
- `get_project(project_id) -> Project`: includes its links (label + url).
- `list_comments_since(issue_id, comment_id) -> list[Comment]`: comments after the given comment id, chronological.
- `post_comment(issue_id, body) -> Comment`: returns the new comment's id.
- `edit_comment(comment_id, body) -> None`: for updating the metadata comment.
- `transition_to_state(issue_id, state_name)`: looks up the state on the issue's team by name.
- `current_user_id() -> str`: cached, used to filter the bot's own comments.

Other:
- API key from `LINEAR_API_KEY`.
- Typed exceptions: `LinearAuthError`, `LinearRateLimitError`, `LinearTransientError`, `LinearNotFoundError`.
- Don't catch transient errors here — let the orchestrator retry on next tick.
- Minimal logging at debug level for each call.

Out of scope: webhooks, caching beyond `current_user_id`, batched mutations, pagination beyond what's needed for the operations above (assume sane limits).

## Acceptance Criteria

All methods callable against a real Linear workspace. Auth failure raises `LinearAuthError`. State transition succeeds for valid state names and raises clearly for unknown ones. Project link lookup returns the structured links list.
