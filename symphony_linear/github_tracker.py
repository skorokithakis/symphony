"""GitHub Projects v2 backend adapter for the Tracker protocol.

Wraps ``GitHubClient`` and implements the ``Tracker`` protocol so the
orchestrator can operate against GitHub Projects v2 + Issues without
knowing it is talking to GitHub.

State (field ids, option ids, project node id, item-id map) lives on the
instance and is resolved lazily on first use.  Nothing is persisted to
disk — resolution is repeated on every daemon startup.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any

from symphony_linear.github import (
    GitHubClient,
    GitHubError,
    GitHubNotFoundError,
)
from symphony_linear.linear import Comment, Issue
from symphony_linear.state import StateManager
from symphony_linear.tracker import (
    TrackerError,
    TransitionTarget,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Project-ref parsing
# ---------------------------------------------------------------------------

_PROJECT_REF_RE = re.compile(r"^(orgs|users)/([^/]+)/projects/(\d+)$")


def _parse_project_ref(ref: str) -> tuple[str, str, int]:
    """Parse a project ref string into (owner_type, owner_name, number).

    Raises ``ValueError`` if the ref does not match the expected format
    ``orgs/<org>/projects/<n>`` or ``users/<user>/projects/<n>``.
    """
    match = _PROJECT_REF_RE.match(ref)
    if not match:
        raise ValueError(
            f"Invalid project ref: {ref!r}. "
            f"Expected format: orgs/<org>/projects/<number> or "
            f"users/<user>/projects/<number>"
        )
    owner_type, owner_name, number = match.groups()
    return owner_type, owner_name, int(number)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class GitHubTrackerConfig:
    """Configuration for a ``GitHubTracker`` instance.

    Attributes:
        token: GitHub personal-access or installation token.
        project_ref: Project reference string, e.g. ``orgs/my-org/projects/1``.
        in_progress_status: Name of the Status field option for in-progress.
        needs_input_status: Name of the Status field option for needs-input.
        qa_status: Name of the QA status option, or ``None`` if not used.
        trigger_field: Name of the single-select field that triggers the agent.
            Default is ``Symphony``.
        status_field: Name of the Status field on the project.  Default is
            ``Status``.
    """

    token: str
    project_ref: str
    in_progress_status: str
    needs_input_status: str
    qa_status: str | None = None
    trigger_field: str = "Symphony"
    status_field: str = "Status"


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


class GitHubTracker:
    """Issue-tracker adapter backed by GitHub Projects v2 + Issues.

    Query logic lives here (not in ``GitHubClient``) because it depends on
    per-project state that is only known after startup resolution.
    """

    def __init__(self, client: GitHubClient, config: GitHubTrackerConfig) -> None:
        self._client = client
        self._config = config

        # Resolved lazily / eagerly.
        self._project_node_id: str | None = None
        self._status_field_id: str | None = None
        self._status_option_ids: dict[str, str] = field(default_factory=dict)
        self._trigger_field_id: str | None = None
        self._trigger_option_id: str | None = None
        self._trigger_option_name: str = "on"

        # In-memory issue-id → project-item-id map.  Built fresh on each
        # list_triggered_issues / _refresh_item_map and atomically swapped.
        self._item_id_map: dict[str, str] = {}
        self._item_map_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Startup resolution
    # ------------------------------------------------------------------

    def resolve(self) -> None:
        """Resolve the project, Status field, and trigger field.

        Must be called once before the tracker is used (typically from
        ``ensure_trigger_setup`` or from ``cli.py`` wiring).  Caches the
        project node id, status-field id, status-option ids, trigger-field
        id, and trigger-option id on the instance.
        """
        self._resolve_project()
        self._resolve_status_field()
        self._resolve_trigger_field()

    def _resolve_project(self) -> None:
        """Parse *project_ref* and look up the ``ProjectV2`` node id."""
        owner_type, owner_name, number = _parse_project_ref(self._config.project_ref)

        query = """\
        query($login: String!, $number: Int!) {
          %s(login: $login) {
            projectV2(number: $number) {
              id
              title
            }
          }
        }
        """ % ("organization" if owner_type == "orgs" else "user")

        data = self._client._query(
            query,
            {
                "login": owner_name,
                "number": number,
            },
        )

        parent = data.get("organization" if owner_type == "orgs" else "user", {})
        project = parent.get("projectV2")
        if project is None:
            raise GitHubNotFoundError(f"Project not found: {self._config.project_ref}")

        self._project_node_id = project["id"]
        logger.info(
            "Resolved project %r → %s",
            project.get("title", "unknown"),
            self._project_node_id,
        )

    def _resolve_status_field(self) -> None:
        """Look up the Status field on the project and cache its option ids.

        Missing required options are auto-created via the
        ``updateProjectV2Field`` mutation rather than raising an error.
        """
        if self._project_node_id is None:
            raise RuntimeError("Project not resolved")

        fields = self._list_single_select_fields()

        # Locate the Status field.
        status = _find_field(fields, self._config.status_field)
        if status is None:
            available = [f.get("name", "?") for f in fields]
            raise ValueError(
                f"Status field {self._config.status_field!r} not found on "
                f"project {self._project_node_id!r}. "
                f"Available single-select fields: {', '.join(map(repr, available))}"
            )

        self._status_field_id = status["id"]
        existing_options: list[dict[str, Any]] = status.get("options", [])
        options: dict[str, str] = {}
        for opt in existing_options:
            options[opt["name"]] = opt["id"]

        # Determine which required options are missing.
        required = {
            self._config.in_progress_status,
            self._config.needs_input_status,
        }
        if self._config.qa_status is not None:
            required.add(self._config.qa_status)

        missing = required - set(options.keys())
        if missing:
            # Build color mapping for the missing options.
            color_map: dict[str, str] = {
                self._config.in_progress_status: "YELLOW",
                self._config.needs_input_status: "ORANGE",
            }
            if self._config.qa_status is not None:
                color_map[self._config.qa_status] = "PURPLE"

            name_to_color: dict[str, str] = {}
            for name in missing:
                name_to_color[name] = color_map[name]

            updated_options = self._add_status_options(existing_options, name_to_color)

            # Rebuild the options dict from the updated option list.
            options = {}
            for opt in updated_options:
                options[opt["name"]] = opt["id"]

            # Guard against a mutation response (or fallback re-query) that
            # still doesn't include the required options we asked for.
            still_missing = required - set(options.keys())
            if still_missing:
                raise ValueError(
                    f"Status option(s) still missing after auto-creation "
                    f"on project {self._project_node_id!r}: "
                    f"{', '.join(map(repr, sorted(still_missing)))}. "
                    f"Available after mutation: "
                    f"{', '.join(map(repr, sorted(options.keys())))}"
                )

        self._status_option_ids = options
        logger.info(
            "Resolved Status field %s with %d options",
            self._status_field_id,
            len(options),
        )

    def _add_status_options(
        self,
        existing_options: list[dict[str, Any]],
        new_options: dict[str, str],
    ) -> list[dict[str, Any]]:
        """Create missing Status field options via the updateProjectV2Field mutation.

        Preserves all existing options (id, name, color, description) and
        adds one new entry per key in *new_options* with the mapped color
        and an empty description.  Returns the full updated option list
        from the mutation response, or re-queries the field list if the
        response doesn't include options.
        """
        if self._status_field_id is None:
            raise RuntimeError("Status field not resolved")

        # Build the singleSelectOptions list for the mutation.
        select_options: list[dict[str, Any]] = []
        for opt in existing_options:
            select_options.append(
                {
                    "id": opt["id"],
                    "name": opt["name"],
                    "color": opt["color"],
                    "description": opt.get("description", ""),
                }
            )

        created_names: list[str] = []
        for name, color in new_options.items():
            select_options.append(
                {
                    "name": name,
                    "color": color,
                    "description": "",
                }
            )
            created_names.append(name)

        logger.info(
            "Auto-creating missing Status options: %s",
            ", ".join(repr(n) for n in created_names),
        )

        mutation = """\
        mutation($input: UpdateProjectV2FieldInput!) {
          updateProjectV2Field(input: $input) {
            projectV2Field {
              ... on ProjectV2SingleSelectField {
                options { id name color description }
              }
            }
          }
        }
        """

        data = self._client._query(
            mutation,
            {
                "input": {
                    "fieldId": self._status_field_id,
                    "singleSelectOptions": select_options,
                },
            },
        )

        field = data.get("updateProjectV2Field", {}).get("projectV2Field")
        if field is not None:
            opts = field.get("options")
            if opts:
                logger.info(
                    "Created %d Status option(s): %s",
                    len(created_names),
                    ", ".join(repr(n) for n in created_names),
                )
                return opts

        # Mutation response didn't include options — re-query the field.
        logger.debug("Mutation response missing options, re-querying field")
        fields = self._list_single_select_fields()
        status = _find_field(fields, self._config.status_field)
        if status is None:
            raise ValueError(
                f"Status field {self._config.status_field!r} disappeared after update"
            )
        return status.get("options", [])

    def _resolve_trigger_field(self) -> None:
        """Find or create the trigger single-select field on the project.

        When the field exists, the option named ``on`` is resolved and its
        id cached.  If no such option exists this is a configuration error
        — raise immediately so the operator can fix it.
        """
        if self._project_node_id is None:
            raise RuntimeError("Project not resolved")

        fields = self._list_single_select_fields()
        existing = _find_field(fields, self._config.trigger_field)

        if existing is not None:
            opts = existing.get("options", [])
            if not opts:
                raise GitHubNotFoundError(
                    f"Trigger field {self._config.trigger_field!r} exists "
                    f"but has no options"
                )
            # Find the option named "on" explicitly rather than picking the first.
            on_opt = None
            for opt in opts:
                if opt.get("name") == self._trigger_option_name:
                    on_opt = opt
                    break
            if on_opt is None:
                raise ValueError(
                    f"Trigger field {self._config.trigger_field!r} exists "
                    f"but has no option named {self._trigger_option_name!r}. "
                    f"Available options: {', '.join(repr(o.get('name')) for o in opts)}"
                )
            self._trigger_field_id = existing["id"]
            self._trigger_option_id = on_opt["id"]
            logger.info(
                "Reusing trigger field %s → option %r (id=%s)",
                self._trigger_field_id,
                on_opt.get("name"),
                self._trigger_option_id,
            )
            return

        # The field doesn't exist — create it.
        self._create_trigger_field()

    def _list_single_select_fields(self) -> list[dict[str, Any]]:
        """Return single-select fields (with their option ids) for the project.

        Paginates through the fields connection in case a project has more
        than 50 fields (unlikely, but defensive).
        """
        if self._project_node_id is None:
            return []

        query = """\
        query($projectId: ID!, $cursor: String) {
          node(id: $projectId) {
            ... on ProjectV2 {
              fields(first: 50, after: $cursor) {
                pageInfo { hasNextPage endCursor }
                nodes {
                  ... on ProjectV2SingleSelectField {
                    id
                    name
                    options { id name color description }
                  }
                }
              }
            }
          }
        }
        """

        all_fields: list[dict[str, Any]] = []
        cursor: str | None = None
        has_next_page = True

        while has_next_page:
            variables: dict[str, Any] = {"projectId": self._project_node_id}
            if cursor:
                variables["cursor"] = cursor
            data = self._client._query(query, variables)
            field_conn = data.get("node", {}).get("fields", {})
            all_fields.extend(field_conn.get("nodes", []))
            page_info = field_conn.get("pageInfo", {})
            has_next_page = page_info.get("hasNextPage", False)
            cursor = page_info.get("endCursor")

        return [f for f in all_fields if f.get("name") is not None]

    def _create_trigger_field(self) -> None:
        """Create the trigger single-select field with one option ``on``."""
        mutation = """\
        mutation($input: CreateProjectV2FieldInput!) {
          createProjectV2Field(input: $input) {
            projectV2Field {
              ... on ProjectV2SingleSelectField {
                id
                options { id name }
              }
            }
          }
        }
        """
        data = self._client._query(
            mutation,
            {
                "input": {
                    "projectId": self._project_node_id,
                    "dataType": "SINGLE_SELECT",
                    "name": self._config.trigger_field,
                    "singleSelectOptions": [
                        # ``description`` is non-null in the
                        # ``CreateProjectV2FieldInput`` schema even though
                        # ``UpdateProjectV2FieldInput`` tolerates its
                        # absence — pass an empty string.
                        {"name": "on", "color": "GREEN", "description": ""},
                    ],
                },
            },
        )

        field = data.get("createProjectV2Field", {}).get("projectV2Field")
        if field is None:
            raise GitHubError("Failed to create trigger field")

        self._trigger_field_id = field["id"]
        opts = field.get("options", [])
        if not opts:
            raise GitHubError("Created trigger field but it has no options")
        self._trigger_option_id = opts[0]["id"]
        logger.info(
            "Created trigger field %s → option %r (id=%s)",
            self._trigger_field_id,
            opts[0].get("name"),
            self._trigger_option_id,
        )

    # ------------------------------------------------------------------
    # Tracker protocol — Trigger setup
    # ------------------------------------------------------------------

    def ensure_trigger_setup(self, state: StateManager) -> None:
        """Idempotently ensure the project is resolved and the trigger field
        exists on the project.

        If the field already exists it is reused; otherwise it is created
        as a ``SINGLE_SELECT`` with one option named ``on``.
        """
        if self._project_node_id is None:
            self.resolve()

    # ------------------------------------------------------------------
    # Tracker protocol — Read operations
    # ------------------------------------------------------------------

    def current_user_id(self) -> str:
        """Return the GitHub user id of the authenticated principal."""
        return self._client.current_user_id()

    def list_triggered_issues(self) -> list[Issue]:
        """Return all currently triggered GitHub issues in the project.

        An item is triggered when all of these hold:
        - its content is an ``Issue`` (not a PR / draft)
        - the issue is ``OPEN``
        - the trigger single-select field is set to the ``'on'`` option
        - the Status field matches one of the configured active statuses

        Pagination is handled; the in-memory ``issue_id → item_id`` map
        is refreshed atomically during this call.
        """
        if self._project_node_id is None:
            raise RuntimeError("Project not resolved; call resolve() first")

        active_statuses = [
            self._config.in_progress_status,
            self._config.needs_input_status,
        ]
        if self._config.qa_status is not None:
            active_statuses.append(self._config.qa_status)
        active_statuses_lower = {s.lower() for s in active_statuses}

        issues: list[Issue] = []
        new_map: dict[str, str] = {}

        cursor: str | None = None
        has_next_page = True

        while has_next_page:
            page = self._query_project_items(cursor)
            items = page.get("items", {})

            for item in items.get("nodes", []):
                content = item.get("content")
                if content is None:
                    continue

                # Only consider Issue items.
                if content.get("__typename") != "Issue":
                    continue

                # Skip closed issues.
                if content.get("state") != "OPEN":
                    continue

                item_id = item["id"]
                issue_id = content["id"]

                # Read field values.
                trigger_on = False
                status_name = ""

                for fv in item.get("fieldValues", {}).get("nodes", []):
                    field = fv.get("field") or {}
                    field_name = field.get("name", "")

                    if field_name == self._config.trigger_field:
                        # Compare by option id, not a hardcoded string.
                        trigger_on = fv.get("optionId") == self._trigger_option_id
                    elif field_name == self._config.status_field:
                        status_name = fv.get("name", "")

                if not trigger_on:
                    continue
                if status_name.lower() not in active_statuses_lower:
                    continue

                repo = content.get("repository") or {}
                ssh_url = repo.get("sshUrl")
                name_with_owner = repo.get("nameWithOwner")

                issue = Issue(
                    id=issue_id,
                    identifier=(
                        # README documents the identifier as <owner>-<repo>-<number>
                        # (flat, no slash). GitHub returns nameWithOwner as
                        # "owner/repo", so we flatten the slash to a hyphen
                        # before appending the issue number — otherwise the
                        # slash leaks into workspace paths and branch names.
                        f"{name_with_owner.replace('/', '-')}-{content['number']}"
                        if name_with_owner
                        else str(content["number"])
                    ),
                    title=content.get("title", ""),
                    state=status_name,
                    labels=[],
                    branchName=None,
                    project=None,
                    updatedAt=content.get("updatedAt", ""),
                    tracker_data={
                        "ssh_url": ssh_url,
                        "project_item_id": item_id,
                    },
                )
                issues.append(issue)
                new_map[issue_id] = item_id

            page_info = items.get("pageInfo", {})
            has_next_page = page_info.get("hasNextPage", False)
            cursor = page_info.get("endCursor")

        # Atomically swap the item-id map so readers never see a
        # partially-built dict.
        with self._item_map_lock:
            self._item_id_map = new_map

        return issues

    def _query_project_items(self, cursor: str | None = None) -> dict[str, Any]:
        """Execute one page of the project-items query."""
        # TODO: fieldValues(first: 50) is not paginated.  50 fields is
        # enough for typical Projects v2 setups; defer pagination until
        # someone hits the limit in practice.
        query = """\
        query($projectId: ID!, $cursor: String) {
          node(id: $projectId) {
            ... on ProjectV2 {
              items(first: 100, after: $cursor) {
                pageInfo { hasNextPage endCursor }
                nodes {
                  id
                  content {
                     __typename
                    ... on Issue {
                      id
                      number
                      title
                      state
                      updatedAt
                      repository {
                        sshUrl
                        nameWithOwner
                      }
                    }
                  }
                  fieldValues(first: 50) {
                    nodes {
                      ... on ProjectV2ItemFieldSingleSelectValue {
                        field {
                          ... on ProjectV2SingleSelectField {
                            name
                          }
                        }
                        name
                        optionId
                      }
                    }
                  }
                }
              }
            }
          }
        }
        """
        variables: dict[str, Any] = {"projectId": self._project_node_id}
        if cursor:
            variables["cursor"] = cursor
        data = self._client._query(query, variables)
        return data.get("node", {})

    def get_issue(self, id: str) -> Issue:
        """Return a single issue by its GitHub node id.

        The ``state`` field is populated from the project's Status field.
        If the issue is not in the project's item-id map, a fresh list
        query re-populates the map before giving up.
        """
        # 1. Fetch the issue's core data (including paginated comments).
        raw = self._fetch_issue_raw(id)

        repo = raw.get("repository") or {}
        name_with_owner = repo.get("nameWithOwner")
        ssh_url = repo.get("sshUrl")

        # 2. Find the project item to get its Status field value.
        status_name = ""
        with self._item_map_lock:
            item_id = self._item_id_map.get(id)
        if item_id is None:
            self._refresh_item_map()
            with self._item_map_lock:
                item_id = self._item_id_map.get(id)

        if item_id is not None:
            status_name = _extract_field_value(
                self._query_item_field_values(item_id),
                self._config.status_field,
            )

        comments = self._comments_from_raw(raw)

        return Issue(
            id=id,
            identifier=(
                # See note in list_triggered_issues — flatten the slash in
                # nameWithOwner to keep the identifier matching the documented
                # <owner>-<repo>-<number> shape.
                f"{name_with_owner.replace('/', '-')}-{raw['number']}"
                if name_with_owner
                else str(raw["number"])
            ),
            title=raw.get("title", ""),
            description=raw.get("body"),
            state=status_name,
            labels=[],
            branchName=None,
            project=None,
            updatedAt=raw.get("updatedAt", ""),
            tracker_data={
                "ssh_url": ssh_url,
                "project_item_id": item_id,
            },
            comments=comments,
        )

    def list_comments_since(self, id: str, last_seen: str | None) -> list[Comment]:
        """Return comments on *id* posted after *last_seen* (chronological).

        Comments are returned oldest-first.  When *last_seen* is ``None``
        all comments are returned.  If *last_seen* is not found in the
        comment list an empty list is returned (to avoid replaying stale
        comments after deletion).
        """
        raw = self._fetch_issue_raw(id)
        all_comments = self._comments_from_raw(raw)

        if last_seen is None:
            return all_comments

        for i, c in enumerate(all_comments):
            if c.id == last_seen:
                return all_comments[i + 1 :]

        logger.warning(
            "Reference comment %s not found on issue %s – returning empty list",
            last_seen,
            id,
        )
        return []

    # ------------------------------------------------------------------
    # Internal — comment / issue fetching
    # ------------------------------------------------------------------

    def _fetch_issue_raw(self, id: str) -> dict[str, Any]:
        """Fetch core issue data (without comments) for a single issue node."""
        query = """\
        query($id: ID!) {
          node(id: $id) {
            __typename
            ... on Issue {
              id
              number
              title
              body
              state
              updatedAt
              repository {
                sshUrl
                nameWithOwner
              }
            }
          }
        }
        """
        data = self._client._query(query, {"id": id})
        raw = data.get("node")
        if raw is None or raw.get("__typename") != "Issue":
            raise GitHubNotFoundError(f"Issue not found: {id}")
        return raw

    def _fetch_all_comments(self, id: str) -> list[dict[str, Any]]:
        """Paginate through all comments on *id*, returning raw nodes.

        Returns comments sorted oldest-first by ``createdAt``, with fetch
        order as the stable tie-breaker when timestamps are identical.
        """
        all_nodes: list[dict[str, Any]] = []
        cursor: str | None = None
        has_next_page = True

        while has_next_page:
            query = """\
            query($id: ID!, $cursor: String) {
              node(id: $id) {
                ... on Issue {
                  comments(first: 100, after: $cursor) {
                    pageInfo { hasNextPage endCursor }
                    nodes {
                      id
                      body
                      createdAt
                      author {
                        ... on User { id }
                        ... on Bot { id }
                        ... on Organization { id }
                      }
                    }
                  }
                }
              }
            }
            """
            variables: dict[str, Any] = {"id": id}
            if cursor:
                variables["cursor"] = cursor
            data = self._client._query(query, variables)
            comment_conn = data.get("node", {}).get("comments", {})
            all_nodes.extend(comment_conn.get("nodes", []))
            page_info = comment_conn.get("pageInfo", {})
            has_next_page = page_info.get("hasNextPage", False)
            cursor = page_info.get("endCursor")

        # Sort oldest-first by createdAt.  The fetch order within each page
        # acts as the stable tie-breaker for comments with identical timestamps.
        all_nodes.sort(key=lambda n: n.get("createdAt", ""))

        return all_nodes

    def _comments_from_raw(self, raw: dict[str, Any]) -> list[Comment]:
        """Build ``Comment`` objects from the comments attached to *raw*.

        *raw* is a full issue node dict that may include an inline
        ``comments.nodes`` list (from ``get_issue``) or may not — in the
        latter case a paginated fetch is performed.
        """
        comment_nodes = raw.get("comments", {}).get("nodes", [])
        if not comment_nodes:
            # The raw issue fetch doesn't include comments; paginate
            # them separately.
            comment_nodes = self._fetch_all_comments(raw["id"])

        return [
            Comment(
                id=c["id"],
                body=c["body"],
                createdAt=c["createdAt"],
                user_id=(c.get("author", {}).get("id") if c.get("author") else None),
            )
            for c in comment_nodes
        ]

    # ------------------------------------------------------------------
    # Tracker protocol — Write operations
    # ------------------------------------------------------------------

    def post_comment(self, id: str, body: str) -> Comment:
        """Post a new comment on issue *id* and return it."""
        mutation = """\
        mutation($input: AddCommentInput!) {
          addComment(input: $input) {
            clientMutationId
            commentEdge {
              node {
                id
                body
                createdAt
                author {
                  ... on User { id }
                  ... on Bot { id }
                  ... on Organization { id }
                }
              }
            }
          }
        }
        """
        data = self._client._query(
            mutation,
            {
                "input": {"subjectId": id, "body": body},
            },
        )

        raw = data.get("addComment", {}).get("commentEdge", {}).get("node")
        if raw is None:
            raise GitHubError("addComment returned no comment")

        return Comment(
            id=raw["id"],
            body=raw["body"],
            createdAt=raw["createdAt"],
            user_id=(raw.get("author", {}).get("id") if raw.get("author") else None),
        )

    def edit_comment(self, id: str, body: str) -> None:
        """Replace the body of an existing comment."""
        mutation = """\
        mutation($input: UpdateIssueCommentInput!) {
          updateIssueComment(input: $input) {
            clientMutationId
          }
        }
        """
        self._client._query(
            mutation,
            {
                "input": {"id": id, "body": body},
            },
        )

    def transition_to(self, id: str, target: TransitionTarget) -> None:
        """Move an issue to the project Status option represented by *target*.

        Raises ``ValueError`` when the target has no configured mapping
        (e.g. ``TransitionTarget.qa`` without a ``qa_status``).
        """
        status_name = _target_to_status_name(
            target,
            self._config.in_progress_status,
            self._config.needs_input_status,
            self._config.qa_status,
        )

        option_id = self._status_option_ids.get(status_name)
        if option_id is None:
            available = list(self._status_option_ids.keys())
            raise ValueError(
                f"Status option {status_name!r} not found in cached option ids. "
                f"Available: {', '.join(map(repr, available))}"
            )

        if self._project_node_id is None or self._status_field_id is None:
            raise RuntimeError("Project or Status field not resolved")

        with self._item_map_lock:
            item_id = self._item_id_map.get(id)
        if item_id is None:
            self._refresh_item_map()
            with self._item_map_lock:
                item_id = self._item_id_map.get(id)

        if item_id is None:
            raise GitHubNotFoundError(f"No project item found for issue {id}")

        mutation = """\
        mutation($input: UpdateProjectV2ItemFieldValueInput!) {
          updateProjectV2ItemFieldValue(input: $input) {
            clientMutationId
          }
        }
        """
        self._client._query(
            mutation,
            {
                "input": {
                    "projectId": self._project_node_id,
                    "itemId": item_id,
                    "fieldId": self._status_field_id,
                    "value": {"singleSelectOptionId": option_id},
                },
            },
        )

    # ------------------------------------------------------------------
    # Tracker protocol — State checks
    # ------------------------------------------------------------------

    def is_still_triggered(self, issue: Issue) -> bool:
        """Return ``True`` if *issue* should remain tracked by the daemon.

        An issue is still triggered when:
        - the trigger field is set to the ``'on'`` option (compared by id)
        - the Status field matches one of the active statuses
        - the underlying GitHub issue is still ``OPEN``

        Uses the ``project_item_id`` stashed in ``issue.tracker_data`` to
        do a single, cheap GraphQL query (no pagination).  Returns
        ``False`` when the item id is missing (issue was never listed or
        was removed from the project).
        """
        td = issue.tracker_data or {}
        item_id = td.get("project_item_id")
        if item_id is None:
            return False

        item = self._query_item_field_values(item_id)
        if item is None:
            return False

        content = item.get("content") or {}
        if content.get("state") != "OPEN":
            return False

        trigger_on = False
        status_name = ""

        for fv in item.get("fieldValues", {}).get("nodes", []):
            field = fv.get("field") or {}
            field_name = field.get("name", "")
            if field_name == self._config.trigger_field:
                trigger_on = fv.get("optionId") == self._trigger_option_id
            elif field_name == self._config.status_field:
                status_name = fv.get("name", "")

        if not trigger_on:
            return False

        active_statuses = {
            self._config.in_progress_status,
            self._config.needs_input_status,
        }
        if self._config.qa_status is not None:
            active_statuses.add(self._config.qa_status)

        return status_name in active_statuses

    def repo_url_for(self, issue: Issue) -> str:
        """Return the SSH clone URL for the repository linked to *issue*.

        The URL is populated at list time from GitHub's ``sshUrl`` field
        and stashed in ``issue.tracker_data``.

        Raises ``TrackerError`` when no ``ssh_url`` is available (the
        issue was not listed from a project item or the repository was
        deleted after listing).
        """
        td = issue.tracker_data or {}
        ssh_url = td.get("ssh_url")
        if ssh_url is None:
            raise TrackerError(
                "No repository linked to this issue. "
                "Ensure the issue belongs to a GitHub repository and "
                "is present on the project board."
            )
        return ssh_url

    def human_trigger_description(self) -> str:
        """Return a user-facing phrase describing how to stop the agent."""
        return (
            f"set the `{self._config.trigger_field}` field to off "
            f"(or remove the item from the project)"
        )

    # ------------------------------------------------------------------
    # QA helpers
    # ------------------------------------------------------------------

    def is_in_qa(self, issue: Issue) -> bool:
        if self._config.qa_status is None:
            return False
        return issue.state == self._config.qa_status

    @property
    def qa_enabled(self) -> bool:
        return self._config.qa_status is not None

    def transition_name_for(self, target: TransitionTarget) -> str:
        return _target_to_status_name(
            target,
            self._config.in_progress_status,
            self._config.needs_input_status,
            self._config.qa_status,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _refresh_item_map(self) -> None:
        """Re-populate ``_item_id_map`` by listing all project items.

        Walks all pages so that ``get_issue`` and ``transition_to`` can
        find item ids even after a daemon restart (the map is in-memory
        only).  Builds a fresh dict locally and atomically swaps to avoid
        readers observing a half-built map.
        """
        new_map: dict[str, str] = {}
        cursor: str | None = None
        has_next_page = True

        while has_next_page:
            page = self._query_project_items(cursor)
            items = page.get("items", {})

            for item in items.get("nodes", []):
                content = item.get("content")
                if content is None or content.get("__typename") != "Issue":
                    continue
                new_map[content["id"]] = item["id"]

            page_info = items.get("pageInfo", {})
            has_next_page = page_info.get("hasNextPage", False)
            cursor = page_info.get("endCursor")

        with self._item_map_lock:
            self._item_id_map = new_map

    def _query_item_field_values(self, item_id: str) -> dict[str, Any] | None:
        """Query a single ``ProjectV2Item`` by its node id.

        Returns the item dict with ``content`` and ``fieldValues``, or
        ``None`` if the item does not exist.
        """
        # TODO: fieldValues(first: 50) is not paginated — see note in
        # _query_project_items.
        query = """\
        query($itemId: ID!) {
          node(id: $itemId) {
            ... on ProjectV2Item {
              content {
                ... on Issue { state }
              }
              fieldValues(first: 50) {
                nodes {
                  ... on ProjectV2ItemFieldSingleSelectValue {
                    field {
                      ... on ProjectV2SingleSelectField { name }
                    }
                    name
                    optionId
                  }
                }
              }
            }
          }
        }
        """
        data = self._client._query(query, {"itemId": item_id})
        return data.get("node")


# ---------------------------------------------------------------------------
# Package-private helpers
# ---------------------------------------------------------------------------


def _find_field(
    fields: list[dict[str, Any]],
    name: str,
) -> dict[str, Any] | None:
    """Return the first field dict whose ``name`` matches (case-sensitive)."""
    for f in fields:
        if f.get("name") == name:
            return f
    return None


def _extract_field_value(
    item: dict[str, Any] | None,
    field_name: str,
) -> str:
    """Extract the named single-select value from a project item dict."""
    if item is None:
        return ""
    for fv in item.get("fieldValues", {}).get("nodes", []):
        field = fv.get("field") or {}
        if field.get("name") == field_name:
            return fv.get("name", "")
    return ""


def _target_to_status_name(
    target: TransitionTarget,
    in_progress: str,
    needs_input: str,
    qa: str | None,
) -> str:
    """Map a ``TransitionTarget`` to the configured Status option name."""
    if target == TransitionTarget.in_progress:
        return in_progress
    if target == TransitionTarget.needs_input:
        return needs_input
    if target == TransitionTarget.qa:
        if qa is None:
            raise ValueError("Cannot transition to QA: no qa_status is configured")
        return qa
    raise ValueError(f"Unknown transition target: {target}")
