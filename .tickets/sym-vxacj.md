---
id: sym-vxacj
status: closed
deps: []
links: []
created: 2026-05-13T01:05:17Z
type: task
priority: 2
assignee: Stavros Korokithakis
---
# Add qa_state config + include in Linear poll

Add an optional 'qa_state' field to symphony_lite.config._LinearConfig (str | None, default None). When set, this is the Linear workflow state name the daemon polls in addition to in_progress_state and needs_input_state.

Scope:
- symphony_lite/config.py: new field on _LinearConfig.
- symphony_lite/orchestrator.py: _fetch_triggered_issues passes qa_state in active_states when configured; _is_still_triggered treats qa_state as an active state too. Both methods must remain correct when qa_state is unset.

Non-goals:
- No serve lifecycle yet. Tickets that land in qa_state will just be tracked (status stays needs_input or whatever it was). The serve mechanism itself comes in a later task.
- No config validation against Linear (we don't verify the state name exists).

Tests:
- _LinearConfig accepts qa_state and defaults to None.
- _fetch_triggered_issues includes qa_state in active_states only when set.
- _is_still_triggered returns True for an issue whose state matches qa_state.

## Acceptance Criteria

Existing tests pass. New config field round-trips through YAML. When qa_state is configured and an issue is in that state, the daemon tracks it and does not clean it up.
