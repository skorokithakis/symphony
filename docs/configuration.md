# Configuration

Symphony reads `config.yaml` from its workspace directory, which defaults to
the current working directory and can be overridden with `--workspace`. The
daemon refuses to start without a valid config; validate it any time with
`symphony-linear --validate-config`.

## Minimal config

For Linear:

```yaml
linear:
  api_key: ${LINEAR_API_KEY}
```

For GitHub:

```yaml
github:
  token: ${GITHUB_TOKEN}
  project: users/your-username/projects/1
  # Optional: enable manual QA by uncommenting.
  # qa_status: QA
```

For the built-in web UI:

```yaml
webui:
  port: 8080
  password: change-me
```

For Linear or GitHub, you can omit the credential entirely and let the daemon
read the appropriate environment variable (`LINEAR_API_KEY` or
`GITHUB_TOKEN`) directly; that is often nicer for systemd or secret managers.

## Coding agent

The optional top-level `agent` key selects the coding-agent CLI for the
daemon. It accepts `opencode` (the default), `omp`, or `pi`:

```yaml
agent: omp
```

The selected binary (`opencode`, `omp`, or `pi`) must be on the sandbox's
`$PATH`. The sandbox uses `SYMPHONY_SANDBOX_PATH` when it is set, otherwise it
uses the daemon's `$PATH`. Agents do not share sessions: changing `agent` makes
a ticket with a session from a different agent start a fresh session on its next
turn instead of resuming the old one.

`pi` keeps credentials in `~/.pi/agent/auth.json`, a separate vault from OMP's:
authenticating OMP does not authenticate pi.

## Full annotated config

The annotated config below shows the Linear backend. The GitHub and `webui`
blocks are also documented in `config.yaml.example` in the repo root, side by
side.

```yaml
# config.yaml (placed in the workspace directory)

# Coding-agent CLI to run (default: opencode). Must be `opencode`, `omp`, or `pi`.
agent: opencode

# Choose exactly one backend — linear, github or webui.
linear:
  # REQUIRED. Linear Personal API key.
  # Use ${LINEAR_API_KEY} to read from the environment, or omit this field
  # entirely and the daemon will fall back to the LINEAR_API_KEY env var.
  # The key can belong to your own account or a dedicated bot account.
  api_key: ${LINEAR_API_KEY}

  # Name of the label that triggers the bot (default: Agent). Set to null to
  # use active workflow states plus a project's Repo external link instead.
  trigger_label: Agent

  # Workflow state set while the AI is working (default: In Progress).
  in_progress_state: In Progress

  # Workflow state set while waiting for human reply (default: Needs Input).
  needs_input_state: Needs Input

  # Optional. Workflow state that enables manual QA. When a ticket enters
  # this state the daemon runs the repo's .symphony/serve script inside the
  # sandbox. Only one serve runs globally; the newest entrant wins. Omit to
  # disable the feature entirely.
  # qa_state: QA

# Built-in tracker and web UI. Comment out `linear:` above and uncomment this
# block to use it instead. See docs/web-ui.md.
# webui:
#   # REQUIRED. Port the web UI listens on.
#   port: 8080
#   # Bind address (default: 127.0.0.1).
#   host: 127.0.0.1
#   # REQUIRED. HTTP Basic auth password. The username is ignored, so any
#   # username (including an empty one) is accepted. There is no login page.
#   password: change-me
#   # Optional. Anthropic API key for automatic ticket titles and reply summaries.
#   # anthropic_api_key: ${ANTHROPIC_API_KEY}
#   # Optional. Messages API base URL, e.g. an Anthropic-compatible gateway.
#   # anthropic_base_url: https://anthropic-gateway.example.com
#   # Optional. Soniox API key for microphone dictation in the reply and
#   # ticket-text boxes. Unset hides the microphone button.
#   # soniox_api_key: ${SONIOX_API_KEY}
#   # Optional. Bearer token for the JSON API at /api/v1, for an external
#   # agent. Unset disables the API: /api/v1 then answers 404.
#   # api_token: ${SYMPHONY_WEBUI_API_TOKEN}
#   # Optional. Public base URL of this UI, sent as the contact claim of a
#   # Web Push notification's token.
#   # public_url: https://symphony.example.com
#   # Web Push notifications need no configuration; the header's bell
#   # subscribes the browser (see Notifications).

# Optional. Model overrides for the primary agent, keyed by alias. See
# "Model override" below.
# models:
#   Strong: anthropic/claude-opus-5
#   Cheap: openai/gpt-5-mini

sandbox:
  # Paths to conceal from the agent inside the sandbox. Directories become
  # an empty tmpfs; files and sockets are replaced with /dev/null. ~ and
  # symlinks are expanded.
  hide_paths:
    - ~/.ssh
    - ~/.gnupg
    - ~/.aws
    - ~/.config/gcloud
    - ~/.netrc
    - ~/.docker
    - /run/docker.sock

  # Optional. Extra host paths bound read-write into the sandbox.
  # Missing paths cause a fatal error. Applied before hide_paths, so hiding
  # still wins in case of collision.
  # WARNING: these bypass the read-only host root mount.
  # extra_rw_paths:
  #   - ~/projects/shared-tools

  # Optional. Map a path inside the sandbox to a host directory (key =
  # sandbox path, value = host source). Relative values resolve under the
  # ticket's mounts/ directory and are deleted with the ticket; absolute
  # values are shared host directories that survive ticket cleanup. Both
  # sides are created on the host. Binds apply after hide_paths, so an
  # explicit mapping can punch through a broad hide. That includes the
  # masking of the daemon's own workspace directory: if an absolute value
  # is the workspace root, or any parent of it such as your home directory,
  # config.yaml becomes readable inside the sandbox. The daemon warns at
  # startup when that happens but does not stop you, because there are
  # legitimate reasons to want it.
  # CAUTION: a shared absolute directory is used by up to 5 concurrent
  # ticket workers — not every tool tolerates that.
  # dir_map:
  #   ~/.config/npm: npm
  #   ~/.npm: ~/sandboxes/caches/.npm

  # Optional. Directory of per-repo secrets files, one KEY=value file per
  # repo at <secrets_dir>/<host>/<owner>/<name>.env. Defaults to
  # <workspace>/secrets. See "Secrets" below.
  # secrets_dir: ~/symphony-secrets

# Seconds between poll cycles (default: 30, minimum: 1).
poll_interval_seconds: 30

# Max seconds per AI turn before the process is killed (default: 1800).
turn_timeout_seconds: 1800

# Max seconds an AI turn may go without producing any output on stdout or
# stderr before the process is killed (default: 1200). This is the idle
# watchdog: a stalled turn is killed long before the absolute cap above, and
# the failure comment says which limit fired ('produced no output for Ns'
# vs 'exceeded Ns in total'). Global only — there is no per-project override.
turn_idle_timeout_seconds: 1200
```

A copy of this example lives at `config.yaml.example` in the repo root.

## Model override

A `Model: <value>` label picks the model for a ticket's primary agent. The
daemon passes `--model` to the selected coding agent on every turn for that
ticket, first turn and resumes alike. Subagents keep their own models.

The label is read from the issue first, then from the issue's Linear project,
so the precedence is **issue label > project label > the agent's own
default**. A `Model:` label on a project is therefore a default for every
issue in it, and an issue label overrides that default one ticket at a time.

The value is looked up case-insensitively in the top-level `models` map:

```yaml
models:
  Strong: anthropic/claude-opus-5
  Cheap: openai/gpt-5-mini
```

Each entry must be a full `provider/model` id. A bare model name is
rejected at startup, since OpenCode reads everything before the first `/`
as the provider.

A value that is not in the map is used verbatim, so a raw provider/model id
works with no config change:

- `Model: Strong` → `--model anthropic/claude-opus-5`
- `Model: anthropic/claude-sonnet-4-6` → `--model anthropic/claude-sonnet-4-6`

On Linear each alias is created for you at startup — once as an issue label
and once as a project label. In label-triggered configurations, this happens
alongside the trigger label, which is only ever an issue label. Linear keeps
issue labels and project labels in two unrelated namespaces (`IssueLabel` and
`ProjectLabel`), and neither can be applied where the other belongs. Nothing
records what was already created, so the
daemon re-checks both namespaces on every start — two API calls per
configured alias.

The labels must be flat and named exactly `Model: <alias>`. A Linear label
*group* named `Model` with children such as `Strong` does not work: the API
reports only the child's own name, which carries no `Model:` prefix. The
provisioned labels are flat, so this only comes up if you hand-create a
group.

On GitHub there is no project tier. GitHub Projects v2 has no project labels
at all — `ProjectV2` has no `labels` field and does not implement
`Labelable` — so a GitHub deployment has the per-issue label and the agent's
own default, with nothing in between. GitHub issue labels are also
per-repository, so create them yourself in each repo.

Nothing is persisted. The model is resolved fresh from the polled issue on
every turn, so changing or removing either label takes effect on the next
turn. The final comment's footer reports the effective model as
`· model: <id>`; it does not say which tier it came from. Model ids are not
validated: a bad value makes the selected agent exit at once, and the ticket
lands in Needs Input with the error.

## Secrets

Each repo can have one secrets file, in normal `.env` format (`KEY=value`
lines). Symphony looks for it at:

```
<workspace>/secrets/<host>/<owner>/<name>.env
```

For example, the repo `git@github.com:acme/api.git` uses
`<workspace>/secrets/github.com/acme/api.env`. The SSH and HTTPS forms of a
URL find the same file, and GitLab subgroups become extra directories. Set
`sandbox.secrets_dir` to keep the files somewhere other than
`<workspace>/secrets`.

The path comes from the repo URL, not the project name. Only the host is
lowercased; the owner and name keep their case, so `Acme/API.git` needs
`Acme/API.env`.

Before each sandbox launch (setup, every agent turn, and QA serve), Symphony
copies the file to `secrets.env` in the ticket's directory, outside the git
checkout, and sets `SYMPHONY_SECRETS_FILE` to its path. Symphony does not
load the values into the environment itself. Your scripts do that; see
[`.symphony/setup`](#symphonysetup). If there is no file, the variable is
not set. Changes take effect on the next launch, so you do not need to
restart the daemon.

The source directory is hidden from the sandbox, so one repo cannot see
another repo's secrets. However, the agent can read its own repo's secrets
file, and it can put what it reads into a ticket comment. Use development
credentials only.

## Manual QA

If you set `linear.qa_state` (or `github.qa_status`) and add an executable
`.symphony/serve` script to your repo, moving a ticket into that workflow
state launches the script inside the sandbox. Use it to run a dev server, a
worker, or anything else you want to exercise by hand. The web UI backend has
a fixed QA state, so the feature is always on there.

Only one serve runs across the whole daemon. Dropping a second ticket into
QA bumps the first back to the needs-input state and starts the new one.
Commenting on a ticket that is currently in QA pulls it back out into the
in-progress state: on the next poll tick the serve is killed, the agent
runs another turn on your comment, and the ticket lands in the needs-input
state. Move it back to QA to test again.

The script is given no time limit and the daemon does not interpret its
output. If it exits non-zero within ten seconds, or exits later for any
reason, the daemon posts a comment containing the exit code and a thousand
characters of stdout/stderr, and the ticket goes back to the needs-input
state. Clean exits within ten seconds are treated as a parent that has
daemonised a child, and are silent.

## Repo conventions

Three optional files in a repo change how Symphony treats it. All three
live under `.symphony/` at the repo root.

### `.symphony/setup`

An executable script run inside the sandbox once, right after each fresh
clone, before the agent starts. Use it to install dependencies, prepare
caches, or whatever else the project needs. Non-zero exit aborts the ticket
with an error comment. The script has a five-minute timeout.

If the repo has a [secrets file](#secrets), load it with:

```sh
[ -n "$SYMPHONY_SECRETS_FILE" ] && { set -a; . "$SYMPHONY_SECRETS_FILE"; set +a; }
```

### `.symphony/serve`

An executable script run inside the sandbox when the ticket enters the
configured `qa_state`. See [Manual QA](#manual-qa) for the details.

If the repo has a [secrets file](#secrets), load it with:

```sh
[ -n "$SYMPHONY_SECRETS_FILE" ] && { set -a; . "$SYMPHONY_SECRETS_FILE"; set +a; }
```

### `.symphony/config.yaml`

Optional per-project overrides for a small set of global settings.
Currently supported keys:

| Key                    | Type      | Default            | Notes                                          |
|------------------------|-----------|--------------------|------------------------------------------------|
| `auto_branch`          | bool      | inherits global    | Applied on first clone, not on resume.         |
| `turn_timeout_seconds` | int (> 0) | inherits global    | Re-read on every turn (initial and resume).    |

```yaml
# .symphony/config.yaml (committed in your project repo)
auto_branch: false
turn_timeout_seconds: 600
```

Unknown keys, invalid YAML or out-of-range values cause Symphony to post an
error comment on the ticket and block the run until you fix the file and
comment to retry. Per-project values win over the global config; missing
keys fall back to the global value.
