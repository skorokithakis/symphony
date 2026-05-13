---
id: Scr-hesei
status: closed
deps: [Scr-kdcga, Scr-lpzwe]
links: []
created: 2026-05-12T13:28:04Z
type: task
priority: 2
assignee: Stavros Korokithakis
---
# OpenCode adapter

Launch OpenCode inside the sandbox and extract the session ID + final assistant message.

CLI surface to wrap:
- Initial run: `opencode run --dir <ws> --format json --dangerously-skip-permissions -m <model> --title <ticket-id> -- <prompt>`
- Resume: `opencode run --dir <ws> --session <sid> --format json --dangerously-skip-permissions -- <message>`

Functions:
- `run_initial(workspace_path, prompt, model, timeout_seconds, on_subprocess) -> (session_id, final_message)`
- `run_resume(workspace_path, session_id, message, model, timeout_seconds, on_subprocess) -> final_message`

`on_subprocess(popen)` callback lets the orchestrator register the Popen for kill-on-label-removal.

Implementation:
- Launch via sandbox wrapper.
- Read stdout line by line; each line is a JSON event.
- Maintain enough state to (1) capture the session id (probably from a startup/session event — verify against actual output before committing), and (2) accumulate the text of the final assistant message of the turn.
- If JSON parsing fails on a line, log at debug and skip.
- On non-zero exit: raise `OpenCodeError` with stderr tail.
- Turn timeout: kill subprocess, raise `OpenCodeTimeout`.
- External SIGKILL (label removed): subprocess exits, we should detect and raise `OpenCodeCancelled`.

REQUIRED first step for the developer: run `opencode run --format json` with a trivial prompt and inspect the event stream to confirm:
1. Where the session id appears.
2. How to identify 'last assistant message of the turn' (probably the final message events before a turn-complete event).
3. Whether stderr is needed for diagnostics or only stdout matters.

Adapt the parser to the actual schema. If parsing the JSON stream proves fragile, fall back to default-format output and capture the printed final-message section.

Out of scope: streaming intermediate output anywhere, token tracking, tool-call surfacing, supporting multiple turns within one invocation (each call to this adapter == one turn).

## Acceptance Criteria

Initial run returns a valid session id and the assistant's final message. Resume with that session id produces a context-aware second message. Timeout kills the process and raises `OpenCodeTimeout`. Non-zero exit raises `OpenCodeError`. External kill via Popen handle raises `OpenCodeCancelled`.
