---
id: sym-gmoah
status: closed
deps: []
links: []
created: 2026-05-13T01:05:24Z
type: task
priority: 2
assignee: Stavros Korokithakis
---
# Workspace helper: launch .symphony/serve in sandbox

Add a function in symphony_lite/workspace.py that launches '.symphony/serve' inside the sandbox and returns the Popen handle without waiting on it. This is the analogue of _run_setup_script but for a long-running process.

Suggested signature:

    def start_serve(
        workspace_path: str,
        hide_paths: list[str],
        extra_rw_paths: list[str] | None = None,
    ) -> subprocess.Popen[bytes]

Behavior:
- If '.symphony/serve' is missing or not executable, raise a new ServeScriptMissing(WorkspaceError) with a clear message. Do NOT silently no-op (unlike setup) — the caller wants to report failure on Linear.
- Launch via run_in_sandbox with the same hide_paths / extra_rw_paths semantics as the setup script.
- stdout/stderr should both be subprocess.PIPE so the caller can capture stderr tail on early failure. The caller is responsible for draining or closing the pipes.
- Do not pass on_subprocess — the caller registers the Popen itself (the serve is not tied to the per-ticket subprocess slot used for tasks).
- No timeout. Return immediately after Popen creation.

Non-goals:
- Failure detection / 10s watchdog — handled by the orchestrator.
- Lifecycle / killing — handled by the orchestrator.

Tests:
- ServeScriptMissing raised when script absent or non-executable.
- Returns a live Popen when script exists and is executable (use a trivial 'sleep 60' script under tmp_path; mark as integration if it shells out to bwrap).

## Acceptance Criteria

Function exists with the documented signature, raises the typed exception on missing script, returns a Popen otherwise. Tests cover both branches.
