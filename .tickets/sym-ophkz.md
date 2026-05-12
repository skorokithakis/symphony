---
id: sym-ophkz
status: closed
deps: [sym-dllst]
links: []
created: 2026-05-12T18:22:20Z
type: task
priority: 2
assignee: Stavros Korokithakis
---
# CLI & orchestrator: --workspace flag, plumb workspace dir through

Wire the new workspace-dir concept through the CLI and orchestrator. Builds on ticket A.

Changes to cli.py:
- Add `--workspace <path>` argument. Default: `Path.cwd()`. Resolve to an absolute path (Path(...).resolve()) before use.
- Remove the `--config` argument and any reference to SYMPHONY_CONFIG.
- Call `load_config(workspace)` and `load_state(workspace)` with the resolved path.
- On FileNotFoundError from load_config, print a friendly stderr message naming the expected path (`<workspace>/config.yaml`) and a one-line hint, then exit 1. Avoid stack traces in the normal missing-config case.
- Update the `--validate-config` summary: log the resolved workspace dir; drop the workspace_root line (the field is gone).
- Pass the workspace path into Orchestrator.

Changes to orchestrator.py:
- Accept the workspace dir as a constructor argument (e.g. `workspace: Path` alongside config/state/linear).
- Replace all uses of `self._config.workspace_root` with the new attribute. Three call sites today: workspace.remove() x2 and workspace.prepare() x1.
- No other behavioural changes.

Tests:
- Update tests/test_orchestrator.py: construct Orchestrator with the new workspace argument; the existing tmp_path/'workspaces' value works fine, just pass it in directly instead of via the config dict.
- No changes needed in tests/test_workspace.py (workspace.py still takes workspace_root as a function arg — that's correct and stays).

Non-goals:
- Do not change workspace.py.
- Do not add upward search for config (`--workspace` or CWD only).

## Acceptance Criteria

symphony-lite --workspace <dir> works and uses <dir>/config.yaml and <dir>/state.json; running without --workspace uses CWD; missing config produces a clear stderr error and non-zero exit (no traceback); Orchestrator no longer reads workspace_root from config; tests pass.

