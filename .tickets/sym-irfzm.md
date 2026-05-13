---
id: sym-irfzm
status: closed
deps: []
links: []
created: 2026-05-13T02:29:14Z
type: task
priority: 2
assignee: Stavros Korokithakis
---
# Config option: default-branch mode (no per-ticket branch)

Today the daemon always switches to (or creates) a per-ticket branch — either Linear's suggested `branchName` or `symphony/<id>` as fallback. This was designed for a workflow where a human pushes the branch later. Some users (e.g. those who configure the agent to push directly to the default branch) want the daemon to skip the branch switch entirely and leave the agent on the cloned default branch.

**Scope**: add a single config flag under `linear:` (or a new top-level section if more apt) — e.g. `auto_branch: true | false` defaulting to `true` (current behavior). When false, `prepare()` skips the `_git_switch_branch` step; the workspace stays on whatever `git clone` checked out.

**Symphony's scope explicitly does NOT include**:
- How/where the agent pushes (SSH, token, gh, none)
- Credentials management (user exposes them outside the sandbox)
- PR creation / push targets
- Branch naming policy beyond the on/off toggle

**Out of scope for this ticket**:
- Letting users override the branch name via config (Linear's branchName + symphony/<id> fallback stays as-is when the toggle is on)
- Anything that changes the cleanup/QA lifecycle (separate concern — see gnosis qa-serve entry)

**Acceptance**:
- Config flag exists with a sensible default (current behavior preserved when unset).
- When disabled, no `git switch` runs during prepare(); workspace HEAD is whatever clone selected.
- Documented in `config.yaml.example`.
