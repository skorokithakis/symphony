---
id: Scr-ezwjc
status: closed
deps: [Scr-mgjco]
links: []
created: 2026-05-12T13:28:30Z
type: task
priority: 2
assignee: Stavros Korokithakis
---
# README and Linear bot setup docs

User-facing README covering everything someone needs to set this up from scratch.

Sections:
- What this is (one paragraph)
- Prerequisites: Python 3.11+, bwrap installed, OpenCode installed and authenticated, git configured
- One-time Linear setup:
  - Create a bot user (separate email; gmail+aliases work). Invite to workspace.
  - Generate Personal API key for the bot, set `LINEAR_API_KEY`.
  - Add a custom workflow state named `Needs Input` (or override the name in config).
  - Create a label named `agent` (or override).
- Per-repo Linear setup:
  - Create a Linear project.
  - Attach a project link labeled `Repo` with the git URL.
- Repo conventions:
  - `.symphony/setup` (executable) for bootstrap.
- Configuration reference (full schema with defaults).
- Running: `symphony-lite` foreground in tmux/nohup.
- How it works (brief mental model: poll, sandbox per-ticket, dumb pipe).
- Limitations:
  - Sandbox masks credential dirs (~/.ssh etc.), so `git push` from inside the agent will fail. Push is your job, outside the sandbox.
  - Free Linear plan caps members (10) and issues (250).
  - No mid-turn steering; comments are queued and delivered at end of turn.
  - No auto-retry on agent failure.
- Troubleshooting: how to find session id (metadata comment), how to inspect workspaces.

Keep it tight. No marketing copy.

Out of scope: tutorials, examples beyond the minimum, screenshots.

## Acceptance Criteria

A fresh user can follow the README and get from zero to a working daemon polling Linear.

