# GitHub

This is the one-off plumbing to connect Symphony to a GitHub project. You do
it once per project. The daemon uses the GitHub Projects v2 (beta) API; the
older Projects v1 is not supported.

## Token

The daemon needs a token with read/write access to Issues, Projects, and
read access to repository Contents.  This token can belong to your own
GitHub account or to a dedicated bot account — the daemon recognises its
own comments by the `*Symphony · …*` footer it adds to each one, so a
separate bot user is optional.
Add the token owner as a collaborator (with at least read access) to
every repository the daemon should clone.

- **Classic personal access token.** Enable the `repo` and `project`
  scopes.
- **Fine-grained personal access token.** Grant read/write on Issues,
  read/write on Projects, and read on Contents for the repositories.
- **GitHub App** with equivalent permissions.

Supply the token as `GITHUB_TOKEN` in the environment, or put it directly
under `github.token` in your config. When the config field is empty or
absent the daemon falls back to the `GITHUB_TOKEN` environment variable.

## Create a project

Create a GitHub Projects v2 project on your user or organization account.
Note its reference in the format `orgs/<org>/projects/<number>` or
`users/<user>/projects/<number>`. That string goes into `github.project`
in your config — for example:

```yaml
github:
  project: users/my-username/projects/1
  # or: orgs/my-org/projects/1
```

## Configure Status

Every GitHub project starts with a built-in single-select field called
`Status`. The daemon expects this field to exist (it does by default on
every new project) and will auto-create any missing options on it at
startup — just set the option names you want in `github.in_progress_status`,
`github.needs_input_status`, and optionally `github.qa_status`, and the
daemon handles the rest.

If your project uses a custom Status field with a different name, set
`github.status_field` to that name.

## The Symphony trigger field

On startup the daemon auto-creates a single-select field called `Symphony`
on the project (configurable via `github.trigger_field`). It comes with
one option, `on`.

- To **trigger** an issue: set its `Symphony` field to `on` and its Status
  to the in-progress option. The daemon picks it up on the next poll and
  starts working. An issue with the field set but no active Status is not
  picked up.
- To **untick** an issue: set the field to empty, or remove the item from
  the project entirely.

Untriggering or moving the issue out of an active Status causes Symphony
to cancel any in-flight subprocesses, delete the workspace, and remove
the issue from its internal state on the next poll tick — just like
removing the trigger label on Linear. If the workspace has uncommitted
or unpushed work, Symphony refuses to delete it: it posts a comment and
moves the ticket back to Needs Input, and only deletes the workspace if
you move the ticket out a second time (or once the workspace is clean).

## Multi-repo projects

GitHub Projects v2 can contain items from any repository that the project
references. Each issue remembers its own repository; Symphony clones from
that repo's URL. By default it uses the SSH URL (`git@github.com:...`).

## Clone protocol

Set `github.clone_protocol` to `https` to clone via HTTPS instead of SSH.
This is useful when your organisation enforces MFA and SSH is not practical.

```yaml
github:
  token: ${GITHUB_TOKEN}
  project: orgs/my-org/projects/1
  clone_protocol: https
```

HTTPS cloning requires the operator to have git credentials set up on the
host (e.g. via `gh auth login` or a credential helper), because clone runs
outside the sandbox with the daemon user's identity.

The default is `ssh`; omit the field to keep the current behaviour.

## Identifier convention

Symphony uses `<owner>-<repo>-<number>-<hash>` as the identifier for GitHub
issues — for example `my-org-my-repo-42-bcc89b7f`. This appears in workspace
directory names and metadata comments, and is used to derive the branch
name when `auto_branch` is enabled.

The trailing hash is a short digest of `owner/repo`. It is there because
flattening the slash would otherwise let two different repositories collide:
`foo-bar/baz` and `foo/bar-baz` both shorten to `foo-bar-baz`, and since a
project can span repositories, both issues could be live at once and would
then share a workspace directory.
