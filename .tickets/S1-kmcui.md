---
id: S1-kmcui
status: closed
deps: []
links: []
created: 2026-05-13T11:44:20Z
type: task
priority: 2
assignee: Stavros Korokithakis
---
# Rename PyPI package to symphony-linear, CLI command to symphony

Rename the distribution name and CLI command. Scope:
- pyproject.toml: change [project] name from 'symphony-lite' to 'symphony-linear'.
- pyproject.toml: change [project.scripts] entry from 'symphony-lite = symphony_lite.cli:main' to 'symphony = symphony_lite.cli:main'.
- Update README.md and config.yaml.example references from 'symphony-lite' (as a command/install name) to the new names. Be careful to distinguish: the Python module is still 'symphony_lite' (renaming the module is a separate follow-up ticket).
- Update AGENTS.md only where it refers to the CLI entry point.

Non-goals:
- Do NOT rename the symphony_lite/ Python module/package directory. That is a separate follow-up ticket.
- Do NOT change any imports.
- Do NOT bump the version.

Caveats:
- The PyPI name 'symphony-linear' is confirmed available; 'symphony' is taken so we cannot use it as a dist name.

## Acceptance Criteria

pyproject.toml has name='symphony-linear' and script 'symphony = symphony_lite.cli:main'. Module imports still work (symphony_lite/ untouched). All textual references in README.md/config.yaml.example/AGENTS.md to the CLI command and install name are updated consistently.
