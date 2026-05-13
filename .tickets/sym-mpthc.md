---
id: sym-mpthc
status: closed
deps: []
links: []
created: 2026-05-13T00:39:31Z
type: task
priority: 2
assignee: Stavros Korokithakis
---
# Inherit daemon PATH in sandbox by default

Today the sandbox is launched with --clearenv and a hardcoded PATH (/usr/local/bin:/usr/bin:/bin plus, for opencode.py, ~/.npm-global/bin and ~/.local/bin). The daemon's own $PATH is ignored, so tools installed via mise/asdf/nix/homebrew/cargo/non-standard locations are invisible to the agent and to .symphony/setup scripts even though they work in the shell that launched the daemon.

Make sandbox.py the single source of PATH defaulting:

- In run_in_sandbox(), when the caller does not pass 'PATH' in env, set PATH from (in priority order): the SYMPHONY_SANDBOX_PATH env var if set, else os.environ['PATH'] if set, else '/usr/local/bin:/usr/bin:/bin' as the final fallback. The existing 'caller overrides' behaviour stays — if env contains 'PATH', use that verbatim.
- Remove the PATH/SYMPHONY_SANDBOX_PATH logic from opencode.py._execute(). It should just pass env={'HOME': home} and let the sandbox default kick in.
- workspace.py._run_setup already only sets HOME; it'll pick up the new default automatically, no change needed beyond confirming behaviour.
- Update the docstring on run_in_sandbox()'s 'env' parameter to describe the new resolution order, and update the inline comment on sandbox.py:164 ('Always provide a sensible PATH...').
- README.md: update the two PATH mentions. The 'opencode must be on $PATH' bullet should say the daemon's $PATH is inherited into the sandbox; SYMPHONY_SANDBOX_PATH is the override for cases where the daemon runs with a stripped PATH (systemd etc.). The 'provides only HOME, a minimal PATH' line in the Sandbox section should be updated to reflect inheritance.
- Tests: there are sandbox tests asserting --setenv PATH values; update them. Add a test that when env has no PATH and SYMPHONY_SANDBOX_PATH is unset, the daemon's os.environ['PATH'] is used; and that SYMPHONY_SANDBOX_PATH still wins when set.

Non-goals: don't change --clearenv behaviour; don't inherit any other env vars; don't change which paths are hidden; don't touch opencode.py beyond removing the now-redundant PATH construction.

## Acceptance Criteria

Daemon's PATH is visible to opencode and .symphony/setup without setting SYMPHONY_SANDBOX_PATH. SYMPHONY_SANDBOX_PATH still works as an override. PATH-resolution logic lives only in sandbox.py. README reflects the new behaviour. Tests cover the three cases (caller-supplied PATH, SYMPHONY_SANDBOX_PATH set, neither set).
