---
id: Scr-kdcga
status: closed
deps: []
links: []
created: 2026-05-12T13:27:26Z
type: task
priority: 2
assignee: Stavros Korokithakis
---
# Project skeleton, config, and state file

Set up the Python project foundation: pyproject.toml + virtualenv-friendly layout, single CLI entry point (proposed name: `symphony-lite`), basic structured logging.

Config:
- Path: `~/.config/symphony-lite/config.yaml` (overridable via `$SYMPHONY_CONFIG`)
- Schema (typed loader):
  - `linear.api_key`: string, supports `$VAR` indirection
  - `linear.trigger_label`: string (default `agent`)
  - `linear.in_progress_state`: string (default `In Progress`)
  - `linear.needs_input_state`: string (default `Needs Input`)
  - `linear.bot_user_email`: string (used to identify own comments)
  - `workspace_root`: path, default `~/symphony/ws`, expand `~` and `$VAR`
  - `poll_interval_seconds`: int, default 30
  - `turn_timeout_seconds`: int, default 1800
  - `sandbox.hide_paths`: list of paths, default `[~/.ssh, ~/.gnupg, ~/.aws, ~/.config/gcloud, ~/.netrc, ~/.docker]`
  - `opencode.model`: string (e.g. `anthropic/claude-sonnet-4`)
- Validate on load. Fail startup with clear errors for missing required values.

State:
- Path: `~/.local/share/symphony-lite/state.json`
- Schema (per ticket): `{ticket_id, ticket_identifier, project_id, repo_url, session_id, workspace_path, branch, last_seen_comment_id, status, metadata_comment_id}`
- Status enum: `bootstrapping | working | needs_input | failed`
- Atomic writes via tmpfile+rename.
- Lock for concurrent access (used later by orchestrator threads).

Out of scope: daemon loop, any Linear/OpenCode/bwrap-specific logic.

## Acceptance Criteria

`symphony-lite --help` runs. Loading a valid config returns a typed config object. Loading an invalid config raises a clear error. State file round-trips correctly and writes are atomic. Concurrent writes from multiple threads do not corrupt the file.

