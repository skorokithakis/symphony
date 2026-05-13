---
id: Scr-lpzwe
status: closed
deps: [Scr-kdcga]
links: []
created: 2026-05-12T13:27:41Z
type: task
priority: 2
assignee: Stavros Korokithakis
---
# Sandbox wrapper (bwrap)

Wrap arbitrary commands in a bwrap sandbox configured for OpenCode + repo work.

Function: `run_in_sandbox(cmd: list[str], workspace_path: str, hide_paths: list[str], env: dict, stdin=None, stdout=PIPE, stderr=PIPE) -> subprocess.Popen`

bwrap rules:
- `--ro-bind / /` (most of filesystem readable)
- `--bind <workspace> <workspace>` (writable)
- `--bind /tmp /tmp`
- `--bind ~/.cache ~/.cache` (OpenCode + tooling caches)
- `--bind ~/.local/share ~/.local/share` IF OpenCode requires writable state here — verify against actual OpenCode behavior and document.
- For each entry in `hide_paths`: `--tmpfs <path>`
- `--dev /dev`, `--proc /proc`
- `--unshare-user --unshare-pid --unshare-ipc --unshare-uts` (do NOT unshare network)
- `--die-with-parent`
- `--new-session`
- `--chdir <workspace>`
- Pass through `env` cleanly (don't inherit everything)

Caller is responsible for timeout/kill. Return Popen so caller can SIGKILL on label-removal.

Verify before declaring done:
- A script inside the sandbox can read /usr/bin/git, cannot read ~/.ssh, can write inside workspace, cannot write outside it, has working DNS/network, cannot reach /var/run/docker.sock, cannot sudo.

Caveat: bwrap may need tweaks for OpenCode plugin loading or self-update. If something breaks, document the additional writable path needed.

Out of scope: capability dropping beyond no-new-privs (bwrap default), cgroup limits, rootless docker setup.

## Acceptance Criteria

Smoke-test script (committed to repo as a fixture) demonstrates: workspace rw, hide_paths masked, network OK, no docker socket, no sudo. Function cleanly hands back a Popen the caller can manage.
