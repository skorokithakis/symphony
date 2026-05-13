---
id: S1-hckzr
status: closed
deps: []
links: []
created: 2026-05-13T11:44:42Z
type: task
priority: 2
assignee: Stavros Korokithakis
---
# Add PyPI publish workflow (hatchling, OIDC)

Add .github/workflows/publish-pypi.yml that publishes to PyPI on version tags, adapted from catt's workflow for our hatchling build backend.

Workflow shape:
- name: Publish to PyPI
- Triggers: push tags matching 'v*'; workflow_dispatch with input 'tag' (string, required) for manual reruns.
- Permissions: contents: read, id-token: write (OIDC trusted publishing).
- Single job 'publish' on ubuntu-latest:
  1. actions/checkout@v4 with ref=${{ github.event.inputs.tag || github.ref }}
  2. actions/setup-python@v5 with python-version '3.11'
  3. Install build: 'python -m pip install --upgrade build'
  4. Build: 'python -m build' (produces sdist + wheel in dist/)
  5. Publish: pypa/gh-action-pypi-publish@release/v1 with verbose: true

Notes:
- Do NOT use poetry — we use hatchling.
- Trusted publishing on PyPI side will be configured by the user out-of-band for package 'symphony-linear'. No PYPI_API_TOKEN secret is used.
- The 'v*' tag trigger pairs with Release Please's tag creation flow.

Non-goals:
- No TestPyPI step.
- No matrix or multi-Python build (pure-Python package; one wheel is fine).

## Acceptance Criteria

'.github/workflows/publish-pypi.yml' exists with the described structure. YAML is valid. The workflow file references 'symphony-linear' nowhere directly (the package name comes from the built artifact).
