---
id: S1-hkdhy
status: open
deps: [S1-kmcui]
links: []
created: 2026-05-13T11:44:39Z
type: task
priority: 2
assignee: Stavros Korokithakis
---
# Add Release Please workflow and config

Add Release Please automation for version bumping and changelog generation.

Files to create:
1. .github/workflows/release-please.yml — triggers on push to master, uses googleapis/release-please-action@v4 with token=secrets.RELEASE_PLEASE_TOKEN, config-file=release-please-config.json, manifest-file=.release-please-manifest.json. Permissions: contents: write, pull-requests: write.

2. release-please-config.json — single root 'python' package. Minimal config:
{
  "packages": {
    ".": {
      "release-type": "python",
      "package-name": "symphony-linear",
      "changelog-path": "CHANGELOG.md",
      "include-component-in-tag": false
    }
  }
}

3. .release-please-manifest.json — seed at current version:
{
  ".": "0.1.0"
}

Notes:
- The 'python' release type bumps version in pyproject.toml's [project] version field. That matches our setup.
- Do NOT create CHANGELOG.md manually; Release Please creates it on first release PR.
- RELEASE_PLEASE_TOKEN will be added by the user post-merge; the workflow file references it now.
- Depends on the package rename ticket having landed (package-name must be 'symphony-linear').

Non-goals:
- No multi-package / monorepo setup.
- No custom changelog sections beyond defaults.

## Acceptance Criteria

All three files exist with the contents described. YAML/JSON are valid (the pre-commit check-yaml and check-json hooks should pass on them).
