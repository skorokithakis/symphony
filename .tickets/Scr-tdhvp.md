---
id: Scr-tdhvp
status: closed
deps: [Scr-kdcga, Scr-lpzwe]
links: []
created: 2026-05-12T13:27:52Z
type: task
priority: 2
assignee: Stavros Korokithakis
---
# Workspace lifecycle (clone, branch, setup, remove)

Per-ticket workspace management. All git operations run OUTSIDE the sandbox using the daemon's own credentials. The `.symphony/setup` script runs INSIDE the sandbox.

Functions:
- `prepare(ticket_identifier: str, repo_url: str, branch_name: str | None) -> str`:
  - Sanitize `ticket_identifier` to `[A-Za-z0-9._-]` (replace others with `_`) → `workspace_key`.
  - Compute `workspace_path = <workspace_root>/<workspace_key>` and assert it's contained within `workspace_root` after normalization (safety invariant).
  - If directory exists: skip clone, just `cd` and `git switch <branch>` (create if missing).
  - If not: `git clone <repo_url> <workspace_path>` then `git switch -c <branch>` (default branch name: lowercase identifier).
  - If `.symphony/setup` exists and is executable: run it inside the sandbox (call into sandbox wrapper). Failure raises `SetupFailed`.
  - Return `workspace_path`.
- `remove(ticket_identifier: str)`:
  - Compute path, assert containment, `rm -rf`.
  - Idempotent (no error if already gone).

Errors:
- `CloneFailed`, `BranchFailed`, `SetupFailed` — caller decides how to surface.

Out of scope: git worktrees (we use full clones), push, remote rewriting, partial-failure cleanup of half-cloned dirs (just leave them and let the next run reuse/repair, or have caller call `remove` first).

## Acceptance Criteria

Clones a real repo, switches to the right branch, runs setup script when present (inside sandbox), idempotent on re-prepare. Remove cleans up. Bad identifiers are sanitized. Path containment check rejects malicious inputs (e.g. identifier with `../`).
