---
id: sym-dllst
status: closed
deps: []
links: []
created: 2026-05-12T18:22:10Z
type: task
priority: 2
assignee: Stavros Korokithakis
---
# Config & state: relocate to workspace dir, drop env var, add LINEAR_API_KEY fallback

Refactor symphony_lite/config.py and symphony_lite/state.py so config and state live alongside per-ticket workspaces under a single workspace directory.

Changes to config.py:
- Remove the workspace_root field from AppConfig.
- Remove DEFAULT_CONFIG_PATH, _resolve_config_path, and all use of the SYMPHONY_CONFIG env var.
- Change load_config to take a workspace_dir (Path) and read `<workspace_dir>/config.yaml`. No other lookup mechanism.
- If `<workspace_dir>/config.yaml` is missing, raise FileNotFoundError with a message that names the expected path and tells the user to create it (the CLI will surface this as a friendly error in task B).
- Add LINEAR_API_KEY environment fallback: if linear.api_key is missing/empty in the YAML, fall back to `os.environ['LINEAR_API_KEY']`. If neither is set, raise a validation error mentioning both options. The existing \${VAR} substitution in YAML strings stays.

Changes to state.py:
- Remove DEFAULT_STATE_PATH.
- StateManager's path becomes required (no default). load_state(workspace_dir: Path) reads/writes `<workspace_dir>/state.json`.
- Atomic-write and locking behaviour unchanged.

Tests:
- Update tests/test_config.py: drop workspace_root assertions, drop SYMPHONY_CONFIG test, add a test for the LINEAR_API_KEY fallback (use monkeypatch), add a test for the missing-config-file error.
- Update tests/test_orchestrator.py fixture to stop setting workspace_root (it will be plumbed differently in task B; for now just drop the key — the orchestrator test will be reconciled in task B).

Non-goals:
- Do not touch cli.py or orchestrator.py in this task (B handles wiring).
- No migration of old paths.
- No new abstractions (no 'Workspace' class etc.) — just move the paths.

## Acceptance Criteria

config.py no longer references workspace_root or SYMPHONY_CONFIG; load_config takes a workspace_dir and reads config.yaml from it; LINEAR_API_KEY env var works when api_key is unset in YAML; state.py's load_state takes a workspace_dir and reads/writes state.json there; tests for config and state pass.

