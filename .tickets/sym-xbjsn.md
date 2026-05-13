---
id: sym-xbjsn
status: closed
deps: []
links: []
created: 2026-05-12T23:33:35Z
type: task
priority: 2
assignee: Stavros Korokithakis
---
# Add sandbox.extra_rw_paths config option + config.yaml.example

Add a configurable list of additional read-write bind paths to the bwrap sandbox. Strictly additive on top of the built-in RW paths (workspace, /tmp, ~/.cache, ~/.local/share, OpenCode state dirs).

Scope:
- config.py: add extra_rw_paths: list[str] to _SandboxConfig, default []. ~ and ${VAR} expansion happens for free via _expand_values.
- sandbox.py: add extra_rw_paths: list[str] param to run_in_sandbox. Bind each as --bind SRC SRC (NOT --bind-try) so missing paths fail loudly. Apply BEFORE hide_paths so hide still wins on collision.
- opencode.py: thread through run_initial, run_resume, and the internal helper — mirror existing hide_paths plumbing.
- workspace.py: thread through prepare() and _run_setup_script — mirror existing sandbox_hide_paths plumbing.
- orchestrator.py: pass self._config.sandbox.extra_rw_paths at the three sandbox call sites (currently mirroring how sandbox.hide_paths is passed).
- Tests: in test_sandbox.py, verify argv contains --bind (not --bind-try) for each extra path and that these binds appear before any hide_paths-related args. In test_config.py, verify default empty list and a user-set list with tilde expansion.
- README.md: document the new field under the sandbox: block with a short warning that listed paths bypass the read-only host root. Add a one-liner pointing to the new config.yaml.example.
- New file config.yaml.example at the repo root: fully annotated example mirroring the one currently embedded in the README, including a commented-out extra_rw_paths example.

Non-goals:
- No extra_ro_paths (host root is already RO).
- No replacement of the built-in RW paths.
- No path-containment / safety check on user-listed paths (they're host paths by design).

Caveats:
- Use --bind, not --bind-try, for extra_rw_paths.
- Order matters: extras must be applied before hide_paths so hide wins on collision (later bwrap mounts override earlier).
