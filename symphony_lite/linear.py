"""Thin wrapper around Linear's GraphQL API.

Synchronous HTTP calls via ``httpx``.  The orchestrator runs per-ticket work
in threads so blocking is fine.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"

# ---------------------------------------------------------------------------
# Typed exceptions
# ---------------------------------------------------------------------------


class LinearError(Exception):
    """Base exception for all Linear API errors."""


class LinearAuthError(LinearError):
    """Authentication failed (HTTP 401/403)."""


class LinearRateLimitError(LinearError):
    """Rate limited (HTTP 429)."""


class LinearTransientError(LinearError):
    """Transient server / network error (HTTP 5xx, timeouts)."""


class LinearNotFoundError(LinearError):
    """Resource not found (HTTP 404 or GraphQL-level not-found)."""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class Comment(BaseModel):
    """A Linear comment."""

    id: str
    body: str
    created_at: str = Field(alias="createdAt")
    user_id: str | None = None


class ProjectLink(BaseModel):
    """A link associated with a Linear project."""

    label: str
    url: str


class Project(BaseModel):
    """A Linear project with its links."""

    id: str
    name: str
    links: list[ProjectLink] = Field(default_factory=list)


class Issue(BaseModel):
    """A Linear issue (ticket)."""

    id: str
    identifier: str
    title: str
    description: str | None = None
    state: str  # state *name*
    labels: list[str] = Field(default_factory=list)
    branch_name: str | None = Field(None, alias="branchName")
    project: Project | None = None
    comments: list[Comment] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers – response parsing & error mapping
# ---------------------------------------------------------------------------


def _parse_graphql_errors(
    response_data: dict[str, Any],
    status_code: int,
) -> None:
    """Inspect a GraphQL response for application-level errors and raise the
    appropriate typed exception.

    Linear sometimes returns ``errors`` alongside partial ``data``.  We raise
    on the first error that maps to a known exception; otherwise we let the
    caller handle the partial result.
    """
    errors: list[dict[str, Any]] = response_data.get("errors", [])
    if not errors:
        return

    # Walk through errors looking for one we can type.
    for err in errors:
        msg = err.get("message", "")
        extensions: dict[str, Any] = err.get("extensions", {})
        code = extensions.get("code", "")

        # Auth-related
        if status_code in (401, 403) or "authentication" in msg.lower():
            raise LinearAuthError(msg)
        if "not found" in msg.lower() or status_code == 404:
            raise LinearNotFoundError(msg)

        # Check for specific Linear error codes.
        if code == "RATELIMITED":
            raise LinearRateLimitError(msg)

    # If we have errors but couldn't categorise them, raise a generic one
    # using the first error message.
    if errors:
        raise LinearError(errors[0].get("message", "Unknown GraphQL error"))


def _raise_for_status(status_code: int) -> None:
    """Map HTTP status codes to typed exceptions.

    This is called *before* we parse the response body so that transport-layer
    errors are caught early.
    """
    if status_code in (401, 403):
        raise LinearAuthError(f"HTTP {status_code}")
    if status_code == 429:
        raise LinearRateLimitError("HTTP 429 – rate limited")
    if 500 <= status_code < 600:
        raise LinearTransientError(f"HTTP {status_code}")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class LinearClient:
    """Minimal synchronous Linear GraphQL client.

    Args:
        api_key: Linear API key (passed in by the caller, e.g. from config).
        client: Optional pre-configured ``httpx.Client`` for testing.
    """

    def __init__(self, api_key: str, *, client: httpx.Client | None = None) -> None:
        self._api_key = api_key
        self._client = client or httpx.Client(
            base_url=LINEAR_GRAPHQL_URL,
            headers={
                "Authorization": api_key,
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        self._cached_user_id: str | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _query(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a GraphQL query/mutation and return the ``data`` payload.

        Raises typed exceptions on HTTP or GraphQL-level errors.
        """
        logger.debug("Linear GraphQL request: %s", _first_line(query))
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        try:
            response = self._client.post("", json=payload)
        except httpx.TimeoutException as exc:
            raise LinearTransientError("Request timed out") from exc
        except httpx.NetworkError as exc:
            raise LinearTransientError(f"Network error: {exc}") from exc

        _raise_for_status(response.status_code)

        body: dict[str, Any] = response.json() if response.content else {}
        _parse_graphql_errors(body, response.status_code)

        return body.get("data", {})

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def current_user_id(self) -> str:
        """Return the Linear user id of the authenticated API key holder.

        The result is cached in-memory for the lifetime of the client.
        """
        if self._cached_user_id is not None:
            return self._cached_user_id

        query = """
        query {
          viewer {
            id
          }
        }
        """
        data = self._query(query)
        self._cached_user_id = data["viewer"]["id"]
        return self._cached_user_id

    def list_triggered_issues(
        self,
        label: str,
        active_states: list[str],
    ) -> list[Issue]:
        """Return issues that have *label* and are in one of *active_states*.

        State filtering is done client-side for simplicity and to avoid
        relying on filter operators that may vary across Linear API versions.
        """
        query = """
        query($label: String!) {
          issues(
            filter: { labels: { name: { eq: $label } } }
            first: 50
          ) {
            nodes {
              id
              identifier
              title
              state { name }
              labels { nodes { name } }
              branchName
              project { id name }
            }
          }
        }
        """
        data = self._query(query, {"label": label})
        nodes: list[dict[str, Any]] = data.get("issues", {}).get("nodes", [])

        issues: list[Issue] = []
        for raw in nodes:
            state_name = raw.get("state", {}).get("name", "")
            if state_name not in active_states:
                continue
            issues.append(_parse_issue_summary(raw))
        return issues

    def get_issue(self, issue_id: str) -> Issue:
        """Return a full issue including description, state, labels, project, and comments."""
        query = """
        query($id: String!) {
          issue(id: $id) {
            id
            identifier
            title
            description
            state { name }
            labels { nodes { name } }
            branchName
            project { id name }
            comments(first: 50, orderBy: createdAt) {
              nodes {
                id
                body
                createdAt
                user { id }
              }
            }
          }
        }
        """
        data = self._query(query, {"id": issue_id})
        raw = data.get("issue")
        if raw is None:
            raise LinearNotFoundError(f"Issue not found: {issue_id}")
        return _parse_issue_full(raw)

    def get_project(self, project_id: str) -> Project:
        """Return a project including its links."""
        query = """
        query($id: String!) {
          project(id: $id) {
            id
            name
            externalLinks { nodes { label url } }
          }
        }
        """
        data = self._query(query, {"id": project_id})
        raw = data.get("project")
        if raw is None:
            raise LinearNotFoundError(f"Project not found: {project_id}")
        return _parse_project(raw)

    def list_comments_since(
        self,
        issue_id: str,
        comment_id: str | None,
    ) -> list[Comment]:
        """Return comments on *issue_id* posted after *comment_id*.

        If *comment_id* is ``None``, all comments are returned.  If
        *comment_id* is provided but not found in the issue's comment list,
        an empty list is returned (to avoid replaying stale comments).

        Comments are returned in chronological order (oldest first).
        """
        query = """
        query($id: String!) {
          issue(id: $id) {
            comments(first: 100, orderBy: createdAt) {
              nodes {
                id
                body
                createdAt
                user { id }
              }
            }
          }
        }
        """
        data = self._query(query, {"id": issue_id})
        raw = data.get("issue")
        if raw is None:
            raise LinearNotFoundError(f"Issue not found: {issue_id}")

        all_comments = [
            Comment(
                id=c["id"],
                body=c["body"],
                createdAt=c["createdAt"],
                user_id=c.get("user", {}).get("id") if c.get("user") else None,
            )
            for c in raw.get("comments", {}).get("nodes", [])
        ]

        # Linear's comments connection defaults to descending (newest first).
        # We specify orderBy: createdAt for explicitness, but still reverse
        # here to guarantee chronological (oldest-first) regardless of the
        # actual delivery order.
        all_comments.reverse()

        if comment_id is None:
            return all_comments

        # Find the position of *comment_id* and return everything after it.
        for i, c in enumerate(all_comments):
            if c.id == comment_id:
                return all_comments[i + 1 :]

        # If the comment_id wasn't found, it may have been deleted or we're
        # tracking a stale reference.  Return an empty list to avoid replaying
        # old comments as fresh input to the agent.
        logger.warning(
            "Reference comment %s not found on issue %s – returning empty list",
            comment_id,
            issue_id,
        )
        return []

    def post_comment(self, issue_id: str, body: str) -> Comment:
        """Post a new comment on *issue_id* and return it."""
        mutation = """
        mutation($input: CommentCreateInput!) {
          commentCreate(input: $input) {
            success
            comment {
              id
              body
              createdAt
              user { id }
            }
          }
        }
        """
        data = self._query(
            mutation,
            {"input": {"issueId": issue_id, "body": body}},
        )
        payload = data["commentCreate"]
        if not payload.get("success"):
            raise LinearError("commentCreate returned success=false")
        raw = payload["comment"]
        return Comment(
            id=raw["id"],
            body=raw["body"],
            createdAt=raw["createdAt"],
            user_id=raw.get("user", {}).get("id") if raw.get("user") else None,
        )

    def edit_comment(self, comment_id: str, body: str) -> None:
        """Update the body of an existing comment."""
        mutation = """
        mutation($id: String!, $input: CommentUpdateInput!) {
          commentUpdate(id: $id, input: $input) {
            success
          }
        }
        """
        data = self._query(
            mutation,
            {"id": comment_id, "input": {"body": body}},
        )
        payload = data["commentUpdate"]
        if not payload.get("success"):
            raise LinearError("commentUpdate returned success=false")

    def transition_to_state(self, issue_id: str, state_name: str) -> None:
        """Transition *issue_id* to the workflow state named *state_name*.

        Looks up the state id on the issue's team.  Raises ``ValueError`` if
        no state with the given name exists.
        """
        # 1. Look up the workflow state id by name.
        state_id = self._resolve_state_id(issue_id, state_name)

        # 2. Perform the transition.
        mutation = """
        mutation($id: String!, $input: IssueUpdateInput!) {
          issueUpdate(id: $id, input: $input) {
            success
          }
        }
        """
        data = self._query(
            mutation,
            {"id": issue_id, "input": {"stateId": state_id}},
        )
        payload = data["issueUpdate"]
        if not payload.get("success"):
            raise LinearError(
                f"Failed to transition issue {issue_id} to state '{state_name}'"
            )

    def _resolve_state_id(self, issue_id: str, state_name: str) -> str:
        """Query the issue's team workflow states and return the id for *state_name*.

        Raises ``ValueError`` if the state is not found.
        """
        query = """
        query($id: String!) {
          issue(id: $id) {
            team {
              states {
                nodes {
                  id
                  name
                }
              }
            }
          }
        }
        """
        data = self._query(query, {"id": issue_id})
        issue_data = data.get("issue")
        if issue_data is None:
            raise LinearNotFoundError(f"Issue not found: {issue_id}")

        team = issue_data.get("team")
        if team is None:
            raise LinearError(f"Issue {issue_id} has no team")

        states: list[dict[str, str]] = team.get("states", {}).get("nodes", [])
        for s in states:
            if s.get("name") == state_name:
                return s["id"]

        available = [s.get("name", "?") for s in states]
        raise ValueError(
            f"State '{state_name}' not found for issue {issue_id}. "
            f"Available states: {', '.join(available)}"
        )


# ---------------------------------------------------------------------------
# Response parsing helpers (package-private)
# ---------------------------------------------------------------------------


def _parse_issue_summary(raw: dict[str, Any]) -> Issue:
    """Parse an issue from a ``list_triggered_issues`` response node."""
    return Issue(
        id=raw["id"],
        identifier=raw["identifier"],
        title=raw["title"],
        description=raw.get("description"),
        state=raw.get("state", {}).get("name", ""),
        labels=[n["name"] for n in raw.get("labels", {}).get("nodes", [])],
        branchName=raw.get("branchName"),
        project=(
            Project(id=raw["project"]["id"], name=raw["project"]["name"])
            if raw.get("project")
            else None
        ),
    )


def _parse_issue_full(raw: dict[str, Any]) -> Issue:
    """Parse an issue from a ``get_issue`` response node (includes comments and description)."""
    return Issue(
        id=raw["id"],
        identifier=raw["identifier"],
        title=raw["title"],
        description=raw.get("description"),
        state=raw.get("state", {}).get("name", ""),
        labels=[n["name"] for n in raw.get("labels", {}).get("nodes", [])],
        branchName=raw.get("branchName"),
        project=(
            Project(id=raw["project"]["id"], name=raw["project"]["name"])
            if raw.get("project")
            else None
        ),
        comments=[
            Comment(
                id=c["id"],
                body=c["body"],
                createdAt=c["createdAt"],
                user_id=c.get("user", {}).get("id") if c.get("user") else None,
            )
            for c in raw.get("comments", {}).get("nodes", [])
        ],
    )


def _parse_project(raw: dict[str, Any]) -> Project:
    """Parse a project from a ``get_project`` response node."""
    return Project(
        id=raw["id"],
        name=raw["name"],
        links=[
            ProjectLink(label=link["label"], url=link["url"])
            for link in raw.get("externalLinks", {}).get("nodes", [])
        ],
    )


def _first_line(text: str) -> str:
    """Return the first non-empty line of *text* for log messages."""
    for line in text.strip().splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return text[:80]
