---
id: sym-xiaom
status: closed
deps: [sym-ophkz]
links: []
created: 2026-05-12T18:22:26Z
type: task
priority: 2
assignee: Stavros Korokithakis
---
# README: document workspace-dir model

Update README.md to reflect the new workspace-dir model. Builds on tickets A and B.

Sections to rewrite:
- Configuration: config now lives at `<workspace>/config.yaml`. Explain that the workspace is the CWD by default, or set with `--workspace`. Remove the `~/.config/symphony-lite/config.yaml` and `$SYMPHONY_CONFIG` references. Remove the `workspace_root` field from the annotated example. Document `LINEAR_API_KEY` env var as a way to keep the key out of the YAML file.
- Running: drop `--config` from the flag table; add `--workspace` with a one-line description.
- What happens at startup: state path is now `<workspace>/state.json` (not `~/.local/share/...`).
- Troubleshooting: "Inspect a workspace" and "Check daemon state" should reference the workspace dir, not `~/symphony/ws` or `~/.local/share/symphony-lite/state.json`. Use a placeholder like `<workspace>/TEAM-42` or note that examples assume the user is in their workspace dir.
- Minimal config example stays minimal — just `linear.api_key` (or LINEAR_API_KEY env) and `linear.bot_user_email`.

Do not introduce new sections or restructure beyond what's needed for accuracy. Keep the existing tone.

## Acceptance Criteria

README accurately describes the workspace-dir model; no references to workspace_root, SYMPHONY_CONFIG, ~/.config/symphony-lite, or ~/.local/share/symphony-lite remain; --workspace is documented; LINEAR_API_KEY env-var option is documented.
