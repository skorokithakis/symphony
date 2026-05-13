---
id: S1-bklza
status: closed
deps: []
links: []
created: 2026-05-13T11:44:30Z
type: task
priority: 2
assignee: Stavros Korokithakis
---
# Add pre-commit config and GitHub Actions workflow

Add .pre-commit-config.yaml with the exact config below, and .github/workflows/pre-commit.yml mirroring the catt project's setup.

.pre-commit-config.yaml contents (verbatim):

repos:
- repo: https://github.com/pre-commit/pre-commit-hooks
  rev: v6.0.0
  hooks:
  - id: check-case-conflict
  - id: check-json
  - id: check-merge-conflict
  - id: check-shebang-scripts-are-executable
  - id: check-symlinks
  - id: check-toml
  - id: check-xml
  - id: check-yaml
  - id: debug-statements
  - id: end-of-file-fixer
  - id: fix-byte-order-marker
  - id: mixed-line-ending
  - id: trailing-whitespace
- repo: https://github.com/astral-sh/ruff-pre-commit
  rev: v0.15.12
  hooks:
    - id: ruff
      args: [ --fix ]
    - id: ruff-format
- repo: https://github.com/pre-commit/mirrors-mypy
  rev: v2.1.0
  hooks:
  - id: mypy
    name: Run MyPy typing checks.
    args: ["--ignore-missing-imports", "--install-types", "--non-interactive"]

.github/workflows/pre-commit.yml: on pull_request and push to master, runs pre-commit/action@v3.0.1 with extra_args '--all-files --hook-stage=manual'. Use actions/checkout@v4 and actions/setup-python@v5 (newer than the catt reference; the older v3 versions are deprecated). Python 3.11.

After adding the configs, run pre-commit locally with '.venv/bin/python -m pip install pre-commit' (or via uv) and execute 'pre-commit run --all-files' to surface what ruff/ruff-format/mypy flag. Fix the findings so the workflow will pass on CI.

Constraints on fixes:
- Prefer minimal, mechanical fixes (ruff --fix output, formatting, obvious type annotations).
- Do NOT change runtime behavior.
- Do NOT alter the public API of any module.
- Preserve all invariants documented in AGENTS.md (atomic state writes, path containment, sandbox layout, etc.).
- If mypy errors are extensive or require non-trivial behavior changes, STOP and report back with the error list rather than power through. A '[tool.mypy]' or '[tool.ruff]' section in pyproject.toml is acceptable to silence specific noisy rules if justified.

Non-goals:
- No pytest workflow in this ticket.
- No coverage, dependabot, codeql, etc.
- Do not add any other linters or formatters.

## Acceptance Criteria

'.pre-commit-config.yaml' exists with the exact config above. '.github/workflows/pre-commit.yml' exists. 'pre-commit run --all-files --hook-stage=manual' passes locally on a clean checkout.
