# Symphony

AI-powered ticket orchestration daemon. Polls Linear for tickets labelled
`agent`, clones the linked repo into a per-ticket workspace, runs OpenCode in a
bubblewrap sandbox, and posts the AI's output back as a Linear comment. It
repeats: when you comment on the ticket, the daemon resumes the session with
your message as input, runs another turn, and posts the result.

---

## Prerequisites

- **Python 3.11+**
- **bwrap** (bubblewrap) — available in most package managers:
  ```bash
  apt install bubblewrap       # Debian/Ubuntu
  dnf install bubblewrap       # Fedora
  pacman -S bubblewrap         # Arch
  ```
- **OpenCode** — installed and authenticated (`opencode auth`). The daemon
  invokes `opencode` inside the sandbox; the daemon's own `$PATH` is
  inherited into the sandbox automatically, so any tool visible in the shell
  that launched the daemon is also visible to the agent.  Set
  `SYMPHONY_SANDBOX_PATH` to override this (useful when the daemon runs with
  a stripped `$PATH`, e.g. under systemd).
- **git** — configured and able to clone the target repos.

## Install

```bash
pip install .
```

The package name is `symphony-lite`. The CLI entry point is `symphony-lite`.

---

## One-time Linear setup

### 1. Create a bot user

Create a separate Linear user for the bot. Gmail aliases work:
`yourname+symphony@gmail.com`. Invite the bot to your Linear workspace.

### 2. Generate a Personal API key

Sign into Linear **as the bot user**. Go to **Settings** → **API** →
**Personal API keys**, create a key, and copy it.

### 3. Add a custom workflow state

In Linear **team settings** → **Workflow**, add a state named **Needs Input**.
This is the state the daemon sets while waiting for your reply. You can
customise the name later in the config.

### 4. Create a trigger label

Create a label named **agent** in the team. Tickets with this label are picked
up by the daemon. You can override the label name in the config.

### 5. (Optional) Add a QA workflow state

If you want to manually QA an agent's work — e.g. run a dev server and click
around — add a workflow state named **QA** (or whatever you prefer) and set
`linear.qa_state` in `config.yaml`. When you move a ticket into this state,
the daemon runs `.symphony/serve` from that ticket's workspace and pauses the
agent. See **Manual QA** below.

---

## Per-repo Linear setup (repeat for each repo)

### 1. Create a Linear project

One project per repository. Any team project works; the daemon just uses it to
find the repo URL.

### 2. Attach a Repo link

Open the project, go to **Resources**, and add a link with:
- **Label**: `Repo` (case-insensitive; must be exactly this or `repo` or `REPO`)
- **URL**: the git clone URL (e.g. `git@github.com:you/your-project.git` or
  `https://github.com/you/your-project.git`)

The daemon reads this link to discover which repo to clone.

---

## Repo conventions

### `.symphony/setup` (optional)

If your repo has an **executable** file at `.symphony/setup`, the daemon runs
it inside the sandbox after cloning. Use it to install dependencies, set up
environments, etc. Exit non-zero to abort the ticket with an error comment. The
script has a 5-minute timeout.

### `.symphony/serve` (optional)

If your repo has an **executable** file at `.symphony/serve`, the daemon runs
it inside the sandbox when its ticket enters the configured `qa_state` (see
`linear.qa_state` in the config). Use it to launch a dev server, worker, or
any other process you want to interact with manually. The script has no time
limit; it stays running until the ticket leaves the QA state (or another
ticket takes over QA). Only one serve runs globally at a time. See
**Manual QA** below.

---

## Configuration

Create `config.yaml` in your workspace directory (the current working
directory by default, or the path passed to `--workspace`). The daemon refuses
to start without a valid config.

Full annotated example:

```yaml
# config.yaml (placed in the workspace directory)

linear:
  # REQUIRED. Linear Personal API key from the bot account.
  # Use ${LINEAR_API_KEY} to read from the environment (or set LINEAR_API_KEY
  # env var — the daemon falls back to it if this field is missing or empty).
  api_key: ${LINEAR_API_KEY}

  # Name of the label that triggers the bot (default: agent).
  trigger_label: agent

  # Linear workflow state set while the AI is working (default: In Progress).
  in_progress_state: In Progress

  # Linear workflow state set while waiting for human reply (default: Needs Input).
  needs_input_state: Needs Input

  # REQUIRED. Email address of the bot user in Linear.
  bot_user_email: yourname+symphony@gmail.com

  # Optional. Linear workflow state that enables manual QA. When a ticket
  # enters this state, the daemon runs the repo's .symphony/serve script
  # inside the sandbox. Only one serve runs globally; moving a different
  # ticket into this state bumps the previous one back to needs_input_state.
  # Omit to disable the feature entirely.
  # qa_state: QA

sandbox:
  # Paths to conceal inside the sandbox (defaults shown below).
  # Directories are overlaid with an empty tmpfs; files and sockets are
  # replaced with /dev/null.  ~ and symlinks are expanded.
  hide_paths:
    - ~/.ssh
    - ~/.gnupg
    - ~/.aws
    - ~/.config/gcloud
    - ~/.netrc
    - ~/.docker
    - /run/docker.sock

  # Additional host paths to bind read-write inside the sandbox.
  # These use --bind (not --bind-try), so missing paths cause a fatal error.
  # Applied before hide_paths, so hide still wins on collision.
  # WARNING: Listed paths bypass the read-only host root mount.
  # extra_rw_paths:
  #   - ~/projects/shared-tools

opencode:
  # Optional. Model in provider/model format. If omitted, OpenCode uses
  # whatever model its own config selects.
  model: anthropic/claude-sonnet-4

# Seconds between Linear poll cycles (default: 30, minimum: 1).
poll_interval_seconds: 30

# Max seconds per AI turn before the process is killed (default: 1800).
turn_timeout_seconds: 1800
```
A standalone copy of this annotated example is available at
`config.yaml.example` in the repo root.

### Minimal config

At minimum you need `linear.api_key` and `linear.bot_user_email`:

```yaml
linear:
  api_key: ${LINEAR_API_KEY}
  bot_user_email: yourname+symphony@gmail.com
```

All other fields use the defaults shown above. If you don't set
`opencode.model`, OpenCode picks the model from its own configuration.

You can also set the `LINEAR_API_KEY` environment variable and omit
`linear.api_key` from the config file entirely — the daemon uses the env var
as a fallback.

### Validate

```bash
symphony-lite --validate-config
```

Exits 0 if the config is valid, prints a summary, and stops. Use this to check
your config before launching the daemon.

---

## Running

```bash
symphony-lite
```

Runs in the foreground. Start it in tmux, screen, or nohup:

```bash
tmux new -s symphony 'symphony-lite'
# or
nohup symphony-lite > /dev/null 2>&1 &
```

Flags:

| Flag | Effect |
|------|--------|
| `--debug` | Enable DEBUG-level logging |
| `--workspace <path>` | Override workspace directory (default: current working directory) |
| `--validate-config` | Load and validate config, then exit |

Logs go to **stderr**.

### What happens at startup

On launch the daemon recovers any orphaned tickets (daemon restarted while a
ticket was `working`). It posts a recovery comment and sets the ticket to
`Needs Input` so you know it's waiting. State is persisted at
`<workspace>/state.json`.

### Graceful shutdown

Send `SIGINT` (Ctrl+C) or `SIGTERM`. The daemon kills all in-flight
subprocesses, persists state, and exits.

---

## How it works

**Poll loop.** Every `poll_interval_seconds` the daemon queries Linear for
tickets that have the trigger label and are in an active state. For each new
ticket it:

1. Looks up the project's `Repo` link to find the git URL.
2. Clones/updates the repo into `<workspace>/<sanitized-identifier>`.
3. Switches to the ticket's branch (or creates a new one). Skipped when
   `auto_branch: false` is set — the workspace stays on whatever `git clone`
   checked out (the remote default branch).
4. Runs `.symphony/setup` inside the sandbox if present.
5. Launches `opencode run` inside the sandbox with the ticket title and
   description as the prompt.
6. Posts the AI's final message as a comment, along with a metadata comment
   (workspace path + session id).
7. Transitions the ticket to `Needs Input`.

**Resume.** When you comment, the daemon picks up new human comments, launches
`opencode run --session <id>`, and posts the result.

**Manual QA.** If you configure `linear.qa_state` and add a `.symphony/serve`
script to your repo, moving a ticket into that workflow state launches the
script inside the sandbox so you can interact with the agent's work (run a
server, exercise a worker, etc.). Only one serve runs globally; moving a
second ticket into QA bumps the first back to `Needs Input` and starts the
new one. The agent doesn't process comments while a ticket is in QA — move
it out of QA (or remove the trigger label) to resume the conversation.
If the script exits non-zero within 10s of launch, or exits later for any
reason, the daemon posts a comment with the exit code and the first 1000
chars of stdout/stderr and transitions the ticket back to `Needs Input`.
Clean exits within 10s are silent (assumed to be a parent that daemonized
a child).

**Sandbox.** Each OpenCode turn runs inside a bubblewrap sandbox. The workspace
is mounted read-write; the rest of the host filesystem is read-only. Credential
directories (SSH, GPG, cloud credentials, Docker socket) are concealed. The
network namespace is shared so the agent can access the internet, but user/PID/
IPC/UTS namespaces are isolated. The sandbox clears the host environment and
provides `HOME` and a `PATH` inherited from the daemon's own `$PATH` (override
with `SYMPHONY_SANDBOX_PATH` for stripped-environment deployments).

**Concurrency.** Up to 5 turns run in parallel across different tickets. Per-ticket
tasks are serialised — a ticket won't get a new turn while a previous one is
still running.

**Interaction model.** The agent and human communicate entirely through Linear
comments. The agent doesn't have access to your terminal, notifications, or
any conversational channel other than Linear.

---

## Limitations

- **No `git push` from inside the agent.** The sandbox conceals `~/.ssh`,
  `~/.gnupg`, and other credential stores, so the agent cannot push commits.
  Pushing is your job — do it outside the sandbox after reviewing the agent's
  work.

- **No mid-turn steering.** You cannot interrupt or redirect the agent during a
  turn. Comments you post while a turn is running are queued and delivered at
  the start of the *next* turn.

- **No auto-retry.** If the AI turn fails (timeout, crash, model error), the
  daemon posts an error comment and sets the ticket to `failed`. It does not
  retry automatically. Comment on the ticket to re-trigger.

- **Free Linear plan limits.** Free plans cap workspace members at 10 and
  issues at 250. The bot user counts toward the member limit.

- **Single workspace per ticket.** The daemon reuses the same workspace
  directory across turns. It does not create a fresh clone per turn.

- **Label-based trigger only.** The agent label must be present for the daemon
  to pick up a ticket. There's no other trigger mechanism.

- **No priority or ordering.** Tickets are picked up in whatever order Linear
  returns them. There is no queue priority system.

- **One QA server at a time.** Only a single `.symphony/serve` process runs
  globally. There is no port allocation or routing — your serve script is
  responsible for binding to whatever port you (or your reverse proxy)
  expect.

---

## Troubleshooting

### Find the session id

The daemon posts a metadata comment on every ticket it processes, formatted as:

```
**Symphony**
- workspace: `<workspace>/TEAM-42`
- session: `ses_abc123`
```

Look for this comment on the ticket to find the workspace path and OpenCode
session id.

### Inspect a workspace

Workspaces live inside your workspace directory. Each subdirectory is named
after the sanitised ticket identifier (e.g. `TEAM-42`, `SCR-123`). You can
`cd` into the workspace and inspect the repo, the agent's changes, or run
OpenCode commands manually.

### Resume a session manually

```bash
cd <workspace>/TEAM-42
opencode run --session ses_abc123 -- "Hello, what's the status?"
```

### See what happened in a turn

OpenCode stores session state (prompt history, tool output, etc.) in
`~/.opencode/` and `~/.local/share/opencode/`. These directories are bind-
mounted into the sandbox, so session resume works across daemon turns and
manual invocations.

### Check daemon state

```bash
cat <workspace>/state.json | python -m json.tool
```

Shows every tracked ticket, its status, workspace path, branch, and session id.
