# Development

```bash
git clone https://github.com/skorokithakis/symphony.git
cd symphony
uv sync
.venv/bin/pytest                              # full suite
.venv/bin/pytest -m "not integration"         # unit only
```

Integration tests shell out to `bwrap` and `git` but never to the real
`opencode`, `omp` or `pi` binary or any LLM. See `AGENTS.md` for an orientation to
the codebase.

## Auto-restart on checkout changes

When the daemon runs from a git checkout — an editable install or
`uv run symphony-linear` — it notices when that checkout's `HEAD` moves
(`git pull`, `jj new`, `jj rebase`, ...) and restarts itself in place so new
work runs the new code. It checks only between poll ticks and waits until no
ticket is being worked on, so a running turn is never cut short. Before
re-execing it runs `--validate-config` in a fresh interpreter; if that fails it
logs the error and keeps running the old code until `HEAD` moves again.

Uncommitted edits do not trigger a restart: they do not move `HEAD`, by
design. This is a development convenience, not a deployment mechanism. It
requires running from a git checkout where the package itself is tracked (an
editable install or `uv run`). A wheel/site-packages install is not a tracked
checkout — including one installed into the repository's own `.venv` — so the
feature is off.

The restart uses `os.execv`, so it keeps the same PID — systemd (and anything
else supervising the process) needs no changes.
