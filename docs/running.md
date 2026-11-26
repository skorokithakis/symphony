# Installing and running

## Installation

Symphony is published on PyPI as `symphony-linear`, and that is also the
name of the command it installs.

### With uvx (recommended)

`uvx` runs Symphony in a managed virtual environment without touching your
system Python:

```bash
uvx symphony-linear --help
```

This is convenient for casual use and for `--validate-config`. For a
long-running daemon you may prefer to install it once rather than have
`uvx` resolve the environment on every start:

```bash
uv tool install symphony-linear
symphony-linear --help
```

### With pip

If you don't have `uv`, plain `pip` works:

```bash
pip install symphony-linear
symphony-linear --help
```

Use `pipx` or a virtualenv if you'd rather not install into your system
Python.

### From source

```bash
git clone https://github.com/skorokithakis/symphony.git
cd symphony
uv sync
.venv/bin/symphony-linear --help
```

### Runtime dependencies

These are not Python packages and won't be installed for you:

- **bwrap** (bubblewrap): `apt install bubblewrap`, `dnf install bubblewrap`,
  or `pacman -S bubblewrap`.
- **git**, configured well enough to clone the repos you want the agent to
  work on.
- **OpenCode** (the default; V2 only, V1 is unsupported), **OMP** or **pi**,
  installed and authenticated. The daemon invokes the selected `opencode`,
  `omp` or `pi` binary inside the sandbox; it must be on the sandbox's `$PATH`. By default the sandbox uses the
  daemon's `$PATH`; set `SYMPHONY_SANDBOX_PATH` to use a different one, for
  example under `systemd`.

## Running

```bash
symphony-linear
```

Symphony runs in the foreground and logs to stderr. For interactive use it
is fine to start it in `tmux` or `screen`; for anything more permanent a
`systemd --user` unit is the obvious home.

### Where the workspace lives

The workspace holds `config.yaml` and `state.json`, the ticket store for the
built-in tracker, and one directory per ticket. Symphony picks it in this
order:

1. `--workspace <path>` if you pass it. It is never created for you.
2. The current directory, if it holds a `config.yaml`.
3. `~/symphony`, when standard input is a terminal. Symphony creates it and
   writes a config there if it is missing.

The third step needs a terminal, so a supervised daemon with a wrong
`WorkingDirectory` fails with the missing-config error instead of quietly
starting a workspace of its own in `~/symphony`. `--validate-config` never
takes the fallback either, and never creates anything.

### The first run

With no arguments, no `config.yaml` in the current directory and a terminal,
Symphony configures itself. It looks for a coding agent on the sandbox `PATH`
(`SYMPHONY_SANDBOX_PATH` if set) in the order OpenCode, OMP, pi, and writes
this to `~/symphony/config.yaml`, mode 0600:

```yaml
agent: opencode   # the first of the three found
webui:
  port: 27149
  password: <random>
```

It prints the address and the password to stderr, then starts normally. The
password is random and mandatory, because the sandbox shares the host network
and could otherwise reach the UI; keep the file private. If none of the three
agents is installed, nothing is created and the daemon exits with an error
naming all of them.

A later run in any terminal directory resolves `~/symphony` again, because
its `config.yaml` now exists. Run from a directory with its own
`config.yaml`, or pass `--workspace`, to use a different workspace.

### systemd

A starting point for `~/.config/systemd/user/symphony.service`:

```ini
[Unit]
Description=Symphony Linear daemon
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/symphony
# No API key here. Keep it in config.yaml in the WorkingDirectory above —
# see "Where to keep credentials" below for why.
# systemd strips PATH. SYMPHONY_SANDBOX_PATH tells the sandbox (and startup
# validation) where to find the selected coding agent and anything your
# .symphony/setup script calls. Include %h/.local/bin if your setup script uses
# tools installed there, such as uv or uvx.
Environment=SYMPHONY_SANDBOX_PATH=%h/.local/bin:%h/.opencode/bin:/usr/local/bin:/usr/bin:/bin
# The daemon itself runs bwrap and git outside the sandbox, so those must be on
# the daemon's own PATH. Add a plain Environment=PATH= line if they live
# somewhere systemd's default does not cover.
# Point at your SSH agent, or every clone of a private repo over SSH fails
# with "Permission denied (publickey)". A systemd unit inherits nothing from
# your shell. Use %t/gcr/ssh for gnome-keyring, %t/openssh_agent for a plain
# ssh-agent. Drop this line if you clone over HTTPS with a token.
Environment=SSH_AUTH_SOCK=%t/gcr/ssh
ExecStart=%h/.local/bin/symphony-linear
Restart=on-failure

[Install]
WantedBy=default.target
```

Then `systemctl --user daemon-reload && systemctl --user enable --now symphony`.

### Where to keep credentials

The rule: the literal key should only ever be written into a file the agent
cannot read. Earlier versions of the README suggested
`Environment=LINEAR_API_KEY=lin_api_...` in the unit. Don't do that.

The coding agent reads files as you, and the sandbox masks the workspace
directory but not `~/.config`, which it should be able to read for other
reasons. So the same key is unreadable in `config.yaml` and readable in a
systemd unit.

Two options that satisfy the rule. Put the value in `config.yaml` in the
workspace directory, which is masked. Or keep using the environment
variables and load them with `EnvironmentFile=`, pointing at a file in the
workspace directory so the masking covers it too. The daemon's own
environment is never passed into the sandbox, so either is fine.

`${LINEAR_API_KEY}` in `config.yaml`, as used in the examples above, is a
reference rather than a value, so it is safe wherever it appears. What
matters is where the value it resolves to is stored.

Two variables in that sample cause most deployment failures, because both
work by accident when you launch Symphony from a terminal and stop working
under systemd:

- `SYMPHONY_SANDBOX_PATH` is the `PATH` inside the sandbox, and the one the
  startup check searches for the selected coding agent. If it omits that agent,
  the daemon refuses to start. If it omits a directory that `.symphony/setup`
  needs, the setup step exits with code 127 and a `command not found` message.
  It does not affect `bwrap` or `git`, which the daemon runs outside the
  sandbox using its own `PATH`.
- `SSH_AUTH_SOCK` is needed because Symphony clones project repos outside
  the sandbox with your credentials.

Other setup and SSH-agent failures appear later, on the first ticket.

By default the unit starts when you log in. To start it at boot instead,
run `loginctl enable-linger $USER`. Note that a lingering service starts
before you unlock your keyring, so an agent-based `SSH_AUTH_SOCK` is not
ready yet at that point.

### Flags

| Flag                 | Effect                                                              |
|----------------------|---------------------------------------------------------------------|
| `--debug`            | Enable DEBUG-level logging.                                         |
| `--workspace <path>` | Workspace directory. Default: the current directory, or `~/symphony` in a terminal. |
| `--validate-config`  | Load and validate the config, then exit. Never creates files.        |

### Startup behaviour

If the daemon stopped in the middle of a turn, it runs that turn again when
it starts. It resumes the agent's session with the comments it has not
answered yet, or starts the ticket from the beginning if the turn had no session yet.
A turn that has a session but no comment left to replay is not run again: the
ticket goes to the needs-input state with a comment that asks you to reply.

The daemon runs an interrupted turn again at most three times in a row. If
the third run is interrupted too, it stops trying, posts a comment that says
so, and moves the ticket to the needs-input state. Reply to the ticket to
start again.

State is persisted at `<workspace>/state.json` and rewritten atomically.

### Graceful shutdown

`SIGINT` (Ctrl+C) or `SIGTERM` triggers a clean shutdown: in-flight
subprocesses are killed, state is persisted, and the daemon exits.

## Troubleshooting

### Find the workspace and session id

For every ticket it processes, Symphony posts a small metadata comment in
this shape:

```
**Symphony**
- workspace: `<workspace>/TEAM-42/repo`
- session: `ses_abc123`
```

The workspace path is where the repo was cloned (Linear tickets use the
team key + number like `TEAM-42`; GitHub issues use
`<owner>-<repo>-<number>-<hash>`; the clone itself lives in the `repo/`
subdirectory of the ticket's directory). The session id belongs to the
configured coding agent. Sessions are keyed by the workspace path, so moving
the repo path prevents any agent from resuming its session.

### Resume a session by hand

For an OpenCode session:

```bash
cd <workspace>/TEAM-42/repo
opencode run --standalone --session ses_abc123 -- "Hello, what's the status?"
```

OpenCode session state lives under `~/.opencode/` and
`~/.local/share/opencode/`. These are bind-mounted into the sandbox so
session resumes work both from inside the daemon and from your shell.
OpenCode's lock directory, `~/.local/state/opencode/locks/`, is also
bind-mounted read-write (and created if missing); the rest of
`~/.local/state/opencode/` stays read-only. OMP
sessions live under `~/.omp/agent/sessions/<slugified-cwd>/`; use OMP's resume
command from the same workspace path. `~/.omp` is likewise bind-mounted into
the sandbox for OMP's session, authentication, and run state. pi sessions
live under `~/.pi/agent/sessions/<slugified-cwd>/`, and `~/.pi` is
bind-mounted the same way; resume one with `pi --session <id>` from the same
workspace path.

### Check daemon state

```bash
cat <workspace>/state.json | python -m json.tool
```

This shows every tracked ticket, its status, workspace path, branch, and
session id.
