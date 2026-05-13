---
id: sym-pbmvd
status: closed
deps: []
links: []
created: 2026-05-13T00:11:35Z
type: task
priority: 2
assignee: Stavros Korokithakis
---
# Format final OpenCode message with tool-call separators

In `symphony_lite/opencode.py`, change how the final assistant message is assembled from the OpenCode NDJSON stream so that tool invocations between text bursts are visible to humans reading the Linear comment.

Today the parser concatenates every `type == "text"` event with no separator and ignores everything else. Tool calls (`type == "tool_use"`) leave no trace, so two text bursts that bracket a tool call run together into one paragraph.

New behavior: walk events in stream order and build a list of segments.
- `type == "text"`: segment is `part.text` (when a non-empty string).
- `type == "tool_use"`: segment is `*<part.state.title>*` if that title is a non-empty string; otherwise `*<part.tool>*` if the tool name is a non-empty string; otherwise no segment at all (the event is skipped).
- All other event types: ignored, as today.

Drop empty segments, then join with `\n\n` and `.strip()` the result. The return value of `_parse_stream`/`run_initial`/`run_resume` (whichever holds the assembly) keeps its existing shape — just the message body changes.

Update the module docstring's event-stream section to document `tool_use` (shape per the real sample: `part.tool`, `part.state.title`, `part.state.status`) and the new assembly rule. Drop the speculative `tool_use`/`tool_result` line that currently says they're ignored.

Extend `tests/fixtures/opencode_events.jsonl` (or add a second fixture) with a text → tool_use → text sequence so the new behavior has a regression test. Add unit tests covering: (a) tool_use with a title, (b) tool_use with no title but a tool name, (c) tool_use with neither (skipped), (d) the existing single-text-burst fixture still parses unchanged.

## Acceptance Criteria

Final message for a text → tool_use(title) → text stream is `<text1>\n\n*<title>*\n\n<text2>` after stripping. Existing fixture in tests/fixtures/opencode_events.jsonl still yields `hi`. Markdown italics use single asterisks (`*foo*`), not underscores.

