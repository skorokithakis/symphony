---
id: S1-exitj
status: open
deps: []
links: []
created: 2026-05-13T11:44:47Z
type: task
priority: 2
assignee: Stavros Korokithakis
---
# Document Conventional Commits requirement in AGENTS.md

Add a short, prominent directive to AGENTS.md requiring all commits to follow the Conventional Commits specification (https://www.conventionalcommits.org/). This is needed because Release Please derives version bumps and changelog entries from commit messages.

Content to add (suggested wording, adjust to match the file's tone):
- A subsection (e.g. under 'Conventions' or as a top-level 'Commit messages' section) stating:
  - All commits MUST follow Conventional Commits.
  - Common prefixes: feat, fix, chore, docs, refactor, test, ci, build, perf, style.
  - Breaking changes: append '!' after type (e.g. 'feat!: ...') or add a 'BREAKING CHANGE:' footer.
  - Release Please uses these to compute the next version: fix → patch, feat → minor, breaking → major.

Keep it terse; this is an agent-facing reference.

Non-goals:
- No commitlint hook in this ticket.
- No rewriting existing commit history.

## Acceptance Criteria

AGENTS.md contains a clear directive that commits must follow Conventional Commits, with enough detail that an agent reading the file knows the common prefixes and breaking-change syntax.
