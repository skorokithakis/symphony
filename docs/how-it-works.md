# How it works

Every `poll_interval_seconds`, Symphony queries the configured tracker for
tickets that carry the trigger signal and live in one of the active workflow
states. New tickets enter the **initial pipeline**:

1. Find the ticket's git URL: the `Repo` link on its Linear project, the
   repository a GitHub issue belongs to, or the repository field on a web UI
   ticket.
2. Clone or update the repo into
   `<workspace>/<sanitised-identifier>/repo`.
3. Switch to the ticket's branch, creating one if needed. If
   `auto_branch: false` is set, the workspace stays on whatever `git clone`
   produced (typically the remote default branch).
4. Run `.symphony/setup` inside the sandbox, if your repo has one.
5. Launch the configured coding agent inside the sandbox with the ticket's
   title and description as the prompt.
6. Post the agent's final message as a comment, plus a small metadata
   comment with the workspace path and agent session id.
7. Transition the ticket to the configured needs-input state.

Tickets you've already replied to enter the **resume pipeline** instead:
the daemon picks up the new human comments, resumes the configured agent's
session, and posts the result.

Up to five turns run in parallel across different tickets, with per-ticket
serialisation so a single ticket never has two turns in flight. The agent
and you only ever communicate through tracker comments; the daemon has no
other channel.

## Sandbox

Each coding-agent turn runs inside a bubblewrap sandbox. The ticket's workspace
is mounted read-write. So are `~/.cache`, `~/.local/share` and the coding
agents' state directories (`~/.opencode`, `~/.omp` and `~/.pi`), which hold
tool caches and agent sessions. The rest of the host root is read-only. Credential
directories such as `~/.ssh`, `~/.gnupg`, `~/.aws`, the Docker socket and a
handful of others are concealed by overlaying empty tmpfs or `/dev/null`.
The network namespace is shared so the agent can reach the internet, but
user, PID, IPC and UTS namespaces are isolated. Environment is wiped down;
its `PATH` comes from a caller-supplied value when present, otherwise from
`SYMPHONY_SANDBOX_PATH` or the daemon's `PATH`.

The daemon's own workspace directory is masked as well, apart from the
ticket being worked on. Without that, `config.yaml` and `state.json` sit two
levels above the agent's working directory and would be plain reads, and so
would every other ticket's checkout. This masking is computed rather than
configured, so a file you later add to that directory is hidden by default.

`/tmp` inside the sandbox is not the host's shared `/tmp`: it is bound to
the ticket's own `tmp/` directory on disk, so scratch files are per-ticket,
survive the sandbox process, and are deleted along with the ticket's
directory when the ticket is cleaned up.

Git operations run outside the sandbox using the daemon's own credentials,
so cloning private repositories works without your SSH private key files
being readable by the agent. Note the narrowness of that: HTTPS credential
stores such as `~/.config/gh` stay readable on purpose, because the agent is
meant to use the `gh` binary and check repositories out. Pushing is left to
you, after reviewing.

Be clear about how strong that last part is. By default it is a convention,
not a barrier. The sandbox runs as your own user, and a read-only mount does
not stop a process connecting to a socket, so your SSH agent is still
reachable from inside. Hiding `~/.ssh` stops the key files being read; it
does not stop the agent asking the SSH agent to sign for it.

To make it a barrier, hide those paths too. Add `~/.config/gh`, any git
credential file you use (for example `~/.git-credentials`) and the SSH
agent's socket (the path in `$SSH_AUTH_SOCK`, for example
`/run/user/1000/gcr/ssh`) to `sandbox.hide_paths`. A list you set replaces
the default one, so keep the default entries in it. This closes those
particular paths: `gh` finds no login and the agent socket is not there. A
path that does not exist when a turn starts is not hidden, and a `dir_map`
entry is applied after the hides and can expose a path again. The daemon
still clones for the agent, because git runs outside the sandbox.

Even then, the sandbox shares your network and runs as your user. Treat it
as protection against an agent that blunders, not against one that is trying
to get out.

## Limitations

- **Linux only.** The sandbox is bubblewrap, which does not exist on macOS
  or Windows.
- **Nothing stops a `git push` by default.** Pushing is meant to be a
  deliberate human step, but that is a convention. The sandbox hides your SSH
  private keys, while the `gh` credentials and the SSH agent stay reachable.
  Add them to `sandbox.hide_paths` to close that path; see
  [Sandbox](#sandbox).
- **No mid-turn steering.** You cannot interrupt or redirect a turn while
  it is running. Comments you post mid-turn are not read: the daemon posts
  a short notice saying so, and you can comment again once the turn
  finishes.
- **No live view of the agent's work.** The web UI shows the ticket's state
  and how long it has been working, not the agent's steps, tool calls or
  streamed output. You see the written reply when the turn ends.
- **No email or webhook notifications.** The web UI marks unread tickets in the
  list, puts a `(N) Symphony` count in the browser tab title, and pushes a
  browser notification for a ticket that reaches Needs Input once you allow it.
  There is no email or webhook notification and nothing is sent for other
  events (replies, QA, Done).
- **No search and no named projects.** The web UI groups tickets by state.
  Saved repositories have a name, but there are no projects to group
  tickets under.
- **No auto-retry.** A failed turn is not run again by itself. The ticket
  goes back to the needs-input state with an error comment. Use the composer's
  Retry button, or comment on the ticket, to try again.
- **Single workspace per ticket.** A ticket's workspace is reused across
  turns; the agent works in the same clone every time.
- **Trigger-only enrolment.** The Linear trigger condition (a label by default,
  or active state plus a project Repo link with `linear.trigger_label: null`)
  or trigger field (GitHub) is the only way to enrol a ticket. In the web UI,
  Start is the only way. There is no manual nudge, slash command, or webhook.
- **One tracker backend per daemon.** Web UI tickets are not visible in
  Linear or GitHub, and Linear or GitHub tickets are not visible in the web
  UI. Running both means two daemons with separate workspace directories.
- **No migration.** There is no import from Linear or GitHub; web UI tickets
  stay in their own store.
- **No priority.** Tickets are picked in whatever order the tracker returns
  them. There is no queue.
- **One QA serve at a time.** A single `.symphony/serve` runs globally with
  no port allocation. Your script is responsible for binding to whichever
  port you (or your reverse proxy) expect.
- **Linear free plan caps.** Free Linear workspaces are capped at 10
  members and 250 issues; the bot counts against the member cap.
