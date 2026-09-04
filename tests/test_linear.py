"""Tests for the Linear GraphQL API client.

Uses ``httpx.MockTransport`` to simulate Linear API responses — no real
network calls.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from symphony_linear.linear import (
    Comment,
    Issue,
    LinearAuthError,
    LinearClient,
    LinearError,
    LinearNotFoundError,
    LinearRateLimitError,
    LinearTransientError,
    Project,
    ProjectLink,
)


# ---------------------------------------------------------------------------
# Helpers – build canned responses
# ---------------------------------------------------------------------------


def _make_transport(
    handler: Any,
) -> httpx.MockTransport:
    """Wrap a handler callable in an httpx MockTransport.

    The handler receives an ``httpx.Request`` and must return an
    ``httpx.Response``.
    """
    return httpx.MockTransport(handler)


def _client(transport: httpx.MockTransport) -> LinearClient:
    """Create a LinearClient wired to *transport*."""
    return LinearClient(
        api_key="test-api-key",
        client=httpx.Client(transport=transport, base_url="http://mock"),
    )


def _json_response(data: dict[str, Any], status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=data)


def _capture_query(call: Any, response: dict[str, Any]) -> str:
    """Run *call* against a client whose transport records the GraphQL query.

    Returns the query string of the first request the call issued.
    """
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["query"])
        return _json_response(response)

    call(_client(_make_transport(handler)))
    return sent[0]


# ---------------------------------------------------------------------------
# Fixtures – sample data
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_issue_raw() -> dict[str, Any]:
    return {
        "id": "abc-123",
        "identifier": "TEAM-42",
        "title": "Fix the thing",
        "updatedAt": "2025-06-01T00:00:00Z",
        "state": {"name": "In Progress"},
        "labels": {"nodes": [{"name": "Agent"}, {"name": "bug"}]},
        "branchName": "team-42-fix-thing",
        "project": {"id": "proj-1", "name": "Backend"},
        "comments": {
            "nodes": [
                {
                    "id": "cmt-1",
                    "body": "First comment",
                    "createdAt": "2025-01-01T00:00:00Z",
                    "user": {"id": "usr-bot"},
                },
                {
                    "id": "cmt-2",
                    "body": "Second comment",
                    "createdAt": "2025-01-02T00:00:00Z",
                    "user": {"id": "usr-human"},
                },
            ]
        },
    }


@pytest.fixture
def sample_project_raw() -> dict[str, Any]:
    return {
        "id": "proj-1",
        "name": "Backend",
        "externalLinks": {
            "nodes": [
                {"label": "GitHub", "url": "https://github.com/org/repo"},
                {"label": "Docs", "url": "https://docs.example.com"},
            ]
        },
    }


# ---------------------------------------------------------------------------
# Exception tests
# ---------------------------------------------------------------------------


class TestExceptionMapping:
    """Verify HTTP status codes map to the correct typed exceptions."""

    def test_auth_401_raises(self) -> None:
        transport = _make_transport(
            lambda req: _json_response({"errors": [{"message": "Unauthorized"}]}, 401)
        )
        client = _client(transport)
        with pytest.raises(LinearAuthError):
            client.get_issue("test")

    def test_auth_403_raises(self) -> None:
        transport = _make_transport(
            lambda req: _json_response({"errors": [{"message": "Forbidden"}]}, 403)
        )
        client = _client(transport)
        with pytest.raises(LinearAuthError):
            client.get_issue("test")

    def test_rate_limit_429_raises(self) -> None:
        transport = _make_transport(lambda req: _json_response({}, 429))
        client = _client(transport)
        with pytest.raises(LinearRateLimitError):
            client.get_issue("test")

    def test_transient_500_raises(self) -> None:
        transport = _make_transport(lambda req: _json_response({}, 500))
        client = _client(transport)
        with pytest.raises(LinearTransientError):
            client.get_issue("test")

    def test_transient_502_raises(self) -> None:
        transport = _make_transport(lambda req: _json_response({}, 502))
        client = _client(transport)
        with pytest.raises(LinearTransientError):
            client.get_issue("test")

    def test_not_found_via_graphql_error(self) -> None:
        transport = _make_transport(
            lambda req: _json_response({"errors": [{"message": "not found"}]}, 200)
        )
        client = _client(transport)
        with pytest.raises(LinearNotFoundError):
            client.get_issue("nonexistent")

    def test_not_found_via_null_data(self) -> None:
        transport = _make_transport(
            lambda req: _json_response({"data": {"issue": None}}, 200)
        )
        client = _client(transport)
        with pytest.raises(LinearNotFoundError):
            client.get_issue("nonexistent")


# ---------------------------------------------------------------------------
# list_triggered_issues
# ---------------------------------------------------------------------------


class TestListTriggeredIssues:
    def test_returns_matching_issues(self) -> None:
        raw = {
            "data": {
                "issues": {
                    "nodes": [
                        {
                            "id": "i-1",
                            "identifier": "TEA-1",
                            "title": "Do A",
                            "updatedAt": "2025-06-01T00:00:00Z",
                            "state": {"name": "In Progress"},
                            "labels": {"nodes": [{"name": "Agent"}]},
                            "branchName": "tea-1-do-a",
                            "project": {"id": "p-1", "name": "Core"},
                        },
                        {
                            "id": "i-2",
                            "identifier": "TEA-2",
                            "title": "Do B",
                            "updatedAt": "2025-06-01T00:00:00Z",
                            "state": {"name": "In Progress"},
                            "labels": {"nodes": [{"name": "Agent"}]},
                            "branchName": None,
                            "project": None,
                        },
                    ]
                }
            }
        }
        transport = _make_transport(lambda req: _json_response(raw))
        client = _client(transport)
        issues = client.list_triggered_issues("Agent", ["In Progress"])

        assert len(issues) == 2
        assert issues[0].id == "i-1"
        assert issues[0].identifier == "TEA-1"
        assert issues[0].state == "In Progress"
        assert issues[0].labels == ["Agent"]
        assert issues[0].branch_name == "tea-1-do-a"
        assert issues[0].project is not None
        assert issues[0].project.id == "p-1"
        assert issues[1].project is None

    def test_filters_by_state_client_side(self) -> None:
        raw = {
            "data": {
                "issues": {
                    "nodes": [
                        {
                            "id": "i-1",
                            "identifier": "TEA-1",
                            "title": "A",
                            "updatedAt": "2025-06-01T00:00:00Z",
                            "state": {"name": "In Progress"},
                            "labels": {"nodes": [{"name": "Agent"}]},
                            "branchName": None,
                            "project": None,
                        },
                        {
                            "id": "i-2",
                            "identifier": "TEA-2",
                            "title": "B",
                            "updatedAt": "2025-06-01T00:00:00Z",
                            "state": {"name": "Backlog"},
                            "labels": {"nodes": [{"name": "Agent"}]},
                            "branchName": None,
                            "project": None,
                        },
                    ]
                }
            }
        }
        transport = _make_transport(lambda req: _json_response(raw))
        client = _client(transport)
        issues = client.list_triggered_issues("Agent", ["In Progress"])
        assert len(issues) == 1
        assert issues[0].id == "i-1"

    def test_none_label_filters_by_state_and_queries_projects(self) -> None:
        def node(
            issue_id: str, state: str, links: list[dict[str, str]] | None
        ) -> dict[str, Any]:
            return {
                "id": issue_id,
                "identifier": issue_id.upper(),
                "title": "T",
                "updatedAt": "2025-06-01T00:00:00Z",
                "state": {"name": state},
                "labels": {"nodes": []},
                "branchName": None,
                "project": (
                    {
                        "id": f"p-{issue_id}",
                        "name": "Project",
                        "externalLinks": {"nodes": links},
                    }
                    if links is not None
                    else None
                ),
            }

        raw = {
            "data": {
                "issues": {
                    "nodes": [
                        node(
                            "i-1",
                            "In Progress",
                            [
                                {
                                    "label": " Repo ",
                                    "url": "https://github.com/org/repo",
                                }
                            ],
                        ),
                        node(
                            "i-2",
                            "In Progress",
                            [{"label": "Docs", "url": "https://docs.example.com"}],
                        ),
                        node(
                            "i-3",
                            "Backlog",
                            [{"label": "Repo", "url": "https://github.com/org/other"}],
                        ),
                        node("i-4", "In Progress", None),
                    ]
                }
            }
        }
        payloads: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payloads.append(json.loads(request.content))
            return _json_response(raw)

        issues = _client(_make_transport(handler)).list_triggered_issues(
            None, ["In Progress"]
        )

        assert [issue.id for issue in issues] == ["i-1", "i-2", "i-4"]
        assert issues[0].project is not None
        assert issues[0].project.links == [
            ProjectLink(label=" Repo ", url="https://github.com/org/repo")
        ]
        assert issues[1].project is not None
        assert issues[1].project.links == [
            ProjectLink(label="Docs", url="https://docs.example.com")
        ]
        assert issues[2].project is None
        assert payloads[0]["variables"] == {"states": ["In Progress"]}
        query = payloads[0]["query"]
        assert "state: { name: { in: $states } }" in query
        assert "project: { null: false }" in query
        assert "labels: { name: { eq: $label } }" not in query

    def test_empty_result(self) -> None:
        raw: dict[str, Any] = {"data": {"issues": {"nodes": []}}}
        transport = _make_transport(lambda req: _json_response(raw))
        client = _client(transport)
        issues = client.list_triggered_issues("Agent", ["In Progress"])
        assert issues == []

    def test_parses_project_labels(self) -> None:
        raw = {
            "data": {
                "issues": {
                    "nodes": [
                        {
                            "id": "i-1",
                            "identifier": "TEA-1",
                            "title": "Do A",
                            "updatedAt": "2025-06-01T00:00:00Z",
                            "state": {"name": "In Progress"},
                            "labels": {"nodes": [{"name": "Agent"}]},
                            "branchName": None,
                            "project": {
                                "id": "p-1",
                                "name": "Core",
                                "labels": {
                                    "nodes": [
                                        {"name": "Model: opus"},
                                        {"name": "tier-1"},
                                    ]
                                },
                            },
                        }
                    ]
                }
            }
        }
        transport = _make_transport(lambda req: _json_response(raw))
        client = _client(transport)
        issues = client.list_triggered_issues("Agent", ["In Progress"])

        assert issues[0].project is not None
        assert issues[0].project.labels == ["Model: opus", "tier-1"]

    def test_missing_or_null_project_labels_parse_as_empty(self) -> None:
        def node(issue_id: str, project: dict[str, Any]) -> dict[str, Any]:
            return {
                "id": issue_id,
                "identifier": issue_id.upper(),
                "title": "T",
                "updatedAt": "2025-06-01T00:00:00Z",
                "state": {"name": "In Progress"},
                "labels": {"nodes": [{"name": "Agent"}]},
                "branchName": None,
                "project": project,
            }

        raw = {
            "data": {
                "issues": {
                    "nodes": [
                        node("i-1", {"id": "p-1", "name": "NoKey"}),
                        node("i-2", {"id": "p-2", "name": "Null", "labels": None}),
                        node(
                            "i-3",
                            {
                                "id": "p-3",
                                "name": "NullNodes",
                                "labels": {"nodes": None},
                            },
                        ),
                    ]
                }
            }
        }
        transport = _make_transport(lambda req: _json_response(raw))
        client = _client(transport)
        issues = client.list_triggered_issues("Agent", ["In Progress"])

        assert len(issues) == 3
        for issue in issues:
            assert issue.project is not None
            assert issue.project.labels == []

    def test_query_requests_project_labels(self) -> None:
        query = _capture_query(
            lambda c: c.list_triggered_issues("Agent", ["In Progress"]),
            {"data": {"issues": {"nodes": []}}},
        )
        assert "labels(first: 50) { nodes { name } }" in query
        assert "externalLinks { nodes { label url } }" in query


# ---------------------------------------------------------------------------
# get_issue
# ---------------------------------------------------------------------------


class TestGetIssue:
    def test_parses_full_issue(self, sample_issue_raw: dict[str, Any]) -> None:
        raw = {"data": {"issue": sample_issue_raw}}
        transport = _make_transport(lambda req: _json_response(raw))
        client = _client(transport)
        issue = client.get_issue("abc-123")

        assert isinstance(issue, Issue)
        assert issue.id == "abc-123"
        assert issue.identifier == "TEAM-42"
        assert issue.title == "Fix the thing"
        assert issue.state == "In Progress"
        assert issue.labels == ["Agent", "bug"]
        assert issue.branch_name == "team-42-fix-thing"
        assert issue.project is not None
        assert issue.project.id == "proj-1"
        assert issue.project.name == "Backend"
        assert len(issue.comments) == 2
        assert issue.comments[0].id == "cmt-1"
        assert issue.comments[0].body == "First comment"
        assert issue.comments[0].user_id == "usr-bot"

    def test_not_found_raises(self) -> None:
        transport = _make_transport(
            lambda req: _json_response({"data": {"issue": None}}, 200)
        )
        client = _client(transport)
        with pytest.raises(LinearNotFoundError, match="not found"):
            client.get_issue("bad-id")

    def test_parses_archived_at(self) -> None:
        raw: dict[str, Any] = {
            "id": "abc-123",
            "identifier": "TEAM-42",
            "title": "Fix the thing",
            "updatedAt": "2025-06-01T00:00:00Z",
            "archivedAt": "2025-01-01T00:00:00.000Z",
            "state": {"name": "Done"},
            "labels": {"nodes": []},
            "branchName": None,
            "project": None,
            "comments": {"nodes": []},
        }
        transport = _make_transport(
            lambda req: _json_response({"data": {"issue": raw}})
        )
        client = _client(transport)
        issue = client.get_issue("abc-123")

        assert issue.archived_at is not None
        assert issue.archived_at.year == 2025

    def test_parses_project_labels(self, sample_issue_raw: dict[str, Any]) -> None:
        sample_issue_raw["project"]["labels"] = {"nodes": [{"name": "Model: sonnet"}]}
        transport = _make_transport(
            lambda req: _json_response({"data": {"issue": sample_issue_raw}})
        )
        client = _client(transport)
        issue = client.get_issue("abc-123")

        assert issue.project is not None
        assert issue.project.labels == ["Model: sonnet"]

    def test_project_labels_default_to_empty(
        self, sample_issue_raw: dict[str, Any]
    ) -> None:
        # The fixture's nested project carries no ``labels`` key at all.
        transport = _make_transport(
            lambda req: _json_response({"data": {"issue": sample_issue_raw}})
        )
        client = _client(transport)
        issue = client.get_issue("abc-123")

        assert issue.project is not None
        assert issue.project.labels == []

    def test_issue_without_project_parses(
        self, sample_issue_raw: dict[str, Any]
    ) -> None:
        sample_issue_raw["project"] = None
        transport = _make_transport(
            lambda req: _json_response({"data": {"issue": sample_issue_raw}})
        )
        client = _client(transport)
        issue = client.get_issue("abc-123")

        assert issue.project is None

    def test_query_requests_project_labels(self) -> None:
        query = _capture_query(
            lambda c: c.get_issue("abc-123"),
            {
                "data": {
                    "issue": {
                        "id": "abc-123",
                        "identifier": "TEAM-42",
                        "title": "T",
                        "updatedAt": "2025-06-01T00:00:00Z",
                        "state": {"name": "In Progress"},
                        "labels": {"nodes": []},
                        "branchName": None,
                        "project": None,
                        "comments": {"nodes": []},
                    }
                }
            },
        )
        assert "labels(first: 50) { nodes { name } }" in query
        assert "externalLinks { nodes { label url } }" in query


# ---------------------------------------------------------------------------
# get_project
# ---------------------------------------------------------------------------


class TestGetProject:
    def test_parses_project_with_links(
        self, sample_project_raw: dict[str, Any]
    ) -> None:
        raw = {"data": {"project": sample_project_raw}}
        transport = _make_transport(lambda req: _json_response(raw))
        client = _client(transport)
        project = client.get_project("proj-1")

        assert isinstance(project, Project)
        assert project.id == "proj-1"
        assert project.name == "Backend"
        assert len(project.links) == 2
        assert project.links[0] == ProjectLink(
            label="GitHub", url="https://github.com/org/repo"
        )
        assert project.links[1] == ProjectLink(
            label="Docs", url="https://docs.example.com"
        )

    def test_project_without_links(self) -> None:
        raw = {
            "data": {
                "project": {
                    "id": "proj-2",
                    "name": "Empty",
                    "externalLinks": {"nodes": []},
                }
            }
        }
        transport = _make_transport(lambda req: _json_response(raw))
        client = _client(transport)
        project = client.get_project("proj-2")
        assert project.links == []

    def test_parses_labels(self, sample_project_raw: dict[str, Any]) -> None:
        sample_project_raw["labels"] = {
            "nodes": [{"name": "Model: opus"}, {"name": "Model: sonnet"}]
        }
        transport = _make_transport(
            lambda req: _json_response({"data": {"project": sample_project_raw}})
        )
        client = _client(transport)
        project = client.get_project("proj-1")

        assert project.labels == ["Model: opus", "Model: sonnet"]

    def test_project_without_labels_key(
        self, sample_project_raw: dict[str, Any]
    ) -> None:
        assert "labels" not in sample_project_raw
        transport = _make_transport(
            lambda req: _json_response({"data": {"project": sample_project_raw}})
        )
        client = _client(transport)
        project = client.get_project("proj-1")

        assert project.labels == []

    def test_project_with_empty_labels_nodes(self) -> None:
        raw = {
            "data": {
                "project": {
                    "id": "proj-3",
                    "name": "NoLabels",
                    "externalLinks": {"nodes": []},
                    "labels": {"nodes": []},
                }
            }
        }
        transport = _make_transport(lambda req: _json_response(raw))
        client = _client(transport)
        project = client.get_project("proj-3")

        assert project.labels == []

    def test_query_requests_labels(self) -> None:
        query = _capture_query(
            lambda c: c.get_project("proj-1"),
            {"data": {"project": {"id": "proj-1", "name": "Backend"}}},
        )
        assert "labels(first: 50) { nodes { name } }" in query

    def test_not_found_raises(self) -> None:
        transport = _make_transport(
            lambda req: _json_response({"data": {"project": None}}, 200)
        )
        client = _client(transport)
        with pytest.raises(LinearNotFoundError):
            client.get_project("bad-id")


# ---------------------------------------------------------------------------
# list_comments_since
# ---------------------------------------------------------------------------


class TestListCommentsSince:
    def _comments_issue_raw(
        self, comment_nodes: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return {
            "data": {
                "issue": {
                    "id": "abc-123",
                    "comments": {"nodes": comment_nodes},
                }
            }
        }

    def _make_comment(
        self, id_: str, body: str, user_id: str | None = None
    ) -> dict[str, Any]:
        c: dict[str, Any] = {
            "id": id_,
            "body": body,
            "createdAt": f"2025-01-0{id_[-1]}T00:00:00Z",
        }
        if user_id:
            c["user"] = {"id": user_id}
        return c

    def test_returns_all_when_comment_id_is_none(self) -> None:
        # Simulate Linear's descending (newest-first) response order.
        nodes = [
            self._make_comment("cmt-2", "Second", "usr-human"),
            self._make_comment("cmt-1", "First"),
        ]
        raw = self._comments_issue_raw(nodes)
        transport = _make_transport(lambda req: _json_response(raw))
        client = _client(transport)
        comments = client.list_comments_since("abc-123", None)
        assert len(comments) == 2
        assert comments[0].id == "cmt-1"
        assert comments[1].id == "cmt-2"

    def test_returns_after_given_comment_id(self) -> None:
        # Simulate Linear's descending (newest-first) response order.
        nodes = [
            self._make_comment("cmt-3", "Third"),
            self._make_comment("cmt-2", "Second"),
            self._make_comment("cmt-1", "First"),
        ]
        raw = self._comments_issue_raw(nodes)
        transport = _make_transport(lambda req: _json_response(raw))
        client = _client(transport)
        comments = client.list_comments_since("abc-123", "cmt-2")
        assert len(comments) == 1
        assert comments[0].id == "cmt-3"

    def test_returns_empty_when_last_comment_matches(self) -> None:
        # Simulate Linear's descending (newest-first) response order.
        nodes = [
            self._make_comment("cmt-2", "Second"),
            self._make_comment("cmt-1", "First"),
        ]
        raw = self._comments_issue_raw(nodes)
        transport = _make_transport(lambda req: _json_response(raw))
        client = _client(transport)
        comments = client.list_comments_since("abc-123", "cmt-2")
        assert comments == []

    def test_not_found_returns_empty_when_id_missing(self) -> None:
        """When the reference comment_id isn't found, return an empty list."""
        # Simulate Linear's descending (newest-first) response order.
        nodes = [
            self._make_comment("cmt-2", "Second"),
            self._make_comment("cmt-1", "First"),
        ]
        raw = self._comments_issue_raw(nodes)
        transport = _make_transport(lambda req: _json_response(raw))
        client = _client(transport)
        comments = client.list_comments_since("abc-123", "deleted-cmt")
        assert comments == []

    def test_realistic_linear_order_returns_newer_only(self) -> None:
        """Simulate the real Linear response (newest-first) and verify that
        list_comments_since returns only comments *newer* than last_seen,
        in ascending (oldest-first) chronological order."""
        # Linear returns comments newest-first.  last_seen is cmt-3.
        nodes = [
            self._make_comment("cmt-6", "Sixth"),  # newest
            self._make_comment("cmt-5", "Fifth"),
            self._make_comment("cmt-4", "Fourth"),
            self._make_comment("cmt-3", "Third"),  # last_seen
            self._make_comment("cmt-2", "Second"),
            self._make_comment("cmt-1", "First"),  # oldest
        ]
        raw = self._comments_issue_raw(nodes)
        transport = _make_transport(lambda req: _json_response(raw))
        client = _client(transport)

        comments = client.list_comments_since("abc-123", "cmt-3")
        # Should return only cmt-4, cmt-5, cmt-6 in ascending order.
        assert len(comments) == 3
        assert comments[0].id == "cmt-4"
        assert comments[1].id == "cmt-5"
        assert comments[2].id == "cmt-6"

        # Verify chronological order by createdAt.
        for i in range(len(comments) - 1):
            assert comments[i].created_at <= comments[i + 1].created_at, (
                f"Comments not in chronological order: "
                f"{comments[i].created_at} > {comments[i + 1].created_at}"
            )

    def test_issue_not_found_raises(self) -> None:
        transport = _make_transport(
            lambda req: _json_response({"data": {"issue": None}}, 200)
        )
        client = _client(transport)
        with pytest.raises(LinearNotFoundError):
            client.list_comments_since("bad-id", None)


# ---------------------------------------------------------------------------
# post_comment
# ---------------------------------------------------------------------------


class TestPostComment:
    def test_creates_and_returns_comment(self) -> None:
        raw = {
            "data": {
                "commentCreate": {
                    "success": True,
                    "comment": {
                        "id": "cmt-new",
                        "body": "hello",
                        "createdAt": "2025-06-01T00:00:00Z",
                        "user": {"id": "usr-bot"},
                    },
                }
            }
        }
        transport = _make_transport(lambda req: _json_response(raw))
        client = _client(transport)
        comment = client.post_comment("abc-123", "hello")
        assert isinstance(comment, Comment)
        assert comment.id == "cmt-new"
        assert comment.body == "hello"
        assert comment.user_id == "usr-bot"

    def test_failure_raises(self) -> None:
        raw = {
            "data": {
                "commentCreate": {
                    "success": False,
                    "comment": None,
                }
            }
        }
        transport = _make_transport(lambda req: _json_response(raw))
        client = _client(transport)
        with pytest.raises(LinearError, match="success=false"):
            client.post_comment("abc-123", "hello")


# ---------------------------------------------------------------------------
# edit_comment
# ---------------------------------------------------------------------------


class TestEditComment:
    def test_updates_successfully(self) -> None:
        raw = {"data": {"commentUpdate": {"success": True}}}
        transport = _make_transport(lambda req: _json_response(raw))
        client = _client(transport)
        # Should not raise
        client.edit_comment("cmt-1", "updated body")

    def test_failure_raises(self) -> None:
        raw = {"data": {"commentUpdate": {"success": False}}}
        transport = _make_transport(lambda req: _json_response(raw))
        client = _client(transport)
        with pytest.raises(LinearError, match="success=false"):
            client.edit_comment("cmt-1", "bad")


# ---------------------------------------------------------------------------
# transition_to_state
# ---------------------------------------------------------------------------


class TestTransitionToState:
    def test_successful_transition(self) -> None:
        """Simulate a two-step transition: lookup states, then mutate."""

        requests_seen: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            requests_seen.append(req)
            body = req.read().decode()
            if "states" in body:
                # Step 1: lookup
                return _json_response(
                    {
                        "data": {
                            "issue": {
                                "team": {
                                    "states": {
                                        "nodes": [
                                            {"id": "st-backlog", "name": "Backlog"},
                                            {"id": "st-ip", "name": "In Progress"},
                                            {"id": "st-done", "name": "Done"},
                                        ]
                                    }
                                }
                            }
                        }
                    }
                )
            elif "issueUpdate" in body:
                return _json_response({"data": {"issueUpdate": {"success": True}}})
            return _json_response({}, 500)

        transport = _make_transport(handler)
        client = _client(transport)
        # Should not raise
        client.transition_to_state("abc-123", "Done")
        assert len(requests_seen) == 2

    def test_unknown_state_raises_valueerror(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return _json_response(
                {
                    "data": {
                        "issue": {
                            "team": {
                                "states": {
                                    "nodes": [
                                        {"id": "st-1", "name": "Backlog"},
                                    ]
                                }
                            }
                        }
                    }
                }
            )

        transport = _make_transport(handler)
        client = _client(transport)
        with pytest.raises(ValueError, match="State 'InvalidState' not found"):
            client.transition_to_state("abc-123", "InvalidState")

    def test_issue_not_found_raises(self) -> None:
        transport = _make_transport(
            lambda req: _json_response({"data": {"issue": None}}, 200)
        )
        client = _client(transport)
        with pytest.raises(LinearNotFoundError):
            client.transition_to_state("bad-id", "Done")

    def test_no_team_raises(self) -> None:
        transport = _make_transport(
            lambda req: _json_response({"data": {"issue": {"team": None}}}, 200)
        )
        client = _client(transport)
        with pytest.raises(LinearError, match="has no team"):
            client.transition_to_state("abc-123", "Done")

    def test_mutation_failure_raises(self) -> None:
        requests_seen: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            requests_seen.append(req)
            body = req.read().decode()
            if "states" in body:
                return _json_response(
                    {
                        "data": {
                            "issue": {
                                "team": {
                                    "states": {
                                        "nodes": [
                                            {"id": "st-done", "name": "Done"},
                                        ]
                                    }
                                }
                            }
                        }
                    }
                )
            elif "issueUpdate" in body:
                return _json_response({"data": {"issueUpdate": {"success": False}}})
            return _json_response({}, 500)

        transport = _make_transport(handler)
        client = _client(transport)
        with pytest.raises(LinearError, match="Failed to transition"):
            client.transition_to_state("abc-123", "Done")


# ---------------------------------------------------------------------------
# Comment model (field aliases)
# ---------------------------------------------------------------------------


class TestCommentModel:
    def test_created_at_alias(self) -> None:
        c = Comment(id="x", body="b", createdAt="2025-01-01T00:00:00Z")
        assert c.created_at == "2025-01-01T00:00:00Z"

    def test_user_id_defaults_to_none(self) -> None:
        c = Comment(id="x", body="b", createdAt="2025-01-01T00:00:00Z")
        assert c.user_id is None


# ---------------------------------------------------------------------------
# Issue model (field aliases)
# ---------------------------------------------------------------------------


class TestIssueModel:
    def test_branch_name_alias(self) -> None:
        issue = Issue(
            id="i",
            identifier="T-1",
            title="T",
            state="S",
            branchName="feat/x",
            updatedAt="2025-06-01T00:00:00Z",
        )
        assert issue.branch_name == "feat/x"

    def test_defaults(self) -> None:
        issue = Issue(
            id="i",
            identifier="T-1",
            title="T",
            state="S",
            updatedAt="2025-06-01T00:00:00Z",
        )
        assert issue.labels == []
        assert issue.comments == []
        assert issue.branch_name is None
        assert issue.project is None


# ---------------------------------------------------------------------------
# find_workspace_label
# ---------------------------------------------------------------------------


class TestFindWorkspaceLabel:
    def test_returns_id_when_label_exists(self) -> None:
        raw = {"data": {"issueLabels": {"nodes": [{"id": "lbl-agent"}]}}}
        transport = _make_transport(lambda req: _json_response(raw))
        client = _client(transport)
        assert client.find_workspace_label("Agent") == "lbl-agent"

    def test_returns_none_when_no_matches(self) -> None:
        raw: dict[str, Any] = {"data": {"issueLabels": {"nodes": []}}}
        transport = _make_transport(lambda req: _json_response(raw))
        client = _client(transport)
        assert client.find_workspace_label("Agent") is None

    def test_returns_none_when_key_missing(self) -> None:
        """Gracefully handle a response without the issueLabels key."""
        raw: dict[str, Any] = {"data": {}}
        transport = _make_transport(lambda req: _json_response(raw))
        client = _client(transport)
        assert client.find_workspace_label("Agent") is None


# ---------------------------------------------------------------------------
# create_workspace_label
# ---------------------------------------------------------------------------


class TestCreateWorkspaceLabel:
    def test_creates_and_returns_label_id(self) -> None:
        raw = {
            "data": {
                "issueLabelCreate": {
                    "success": True,
                    "issueLabel": {"id": "lbl-new"},
                }
            }
        }
        transport = _make_transport(lambda req: _json_response(raw))
        client = _client(transport)
        assert client.create_workspace_label("Agent") == "lbl-new"

    def test_failure_raises_linear_error(self) -> None:
        raw = {
            "data": {
                "issueLabelCreate": {
                    "success": False,
                    "issueLabel": None,
                }
            }
        }
        transport = _make_transport(lambda req: _json_response(raw))
        client = _client(transport)
        with pytest.raises(LinearError, match="Failed to create workspace label"):
            client.create_workspace_label("Agent")


# ---------------------------------------------------------------------------
# find_project_label
# ---------------------------------------------------------------------------


class TestFindProjectLabel:
    def test_returns_id_when_label_exists(self) -> None:
        raw = {"data": {"projectLabels": {"nodes": [{"id": "lbl-model"}]}}}
        transport = _make_transport(lambda req: _json_response(raw))
        client = _client(transport)
        assert client.find_project_label("Model: Strong") == "lbl-model"

    def test_returns_none_when_no_matches(self) -> None:
        raw: dict[str, Any] = {"data": {"projectLabels": {"nodes": []}}}
        transport = _make_transport(lambda req: _json_response(raw))
        client = _client(transport)
        assert client.find_project_label("Model: Strong") is None

    def test_returns_none_when_key_missing(self) -> None:
        """Gracefully handle a response without the projectLabels key."""
        raw: dict[str, Any] = {"data": {}}
        transport = _make_transport(lambda req: _json_response(raw))
        client = _client(transport)
        assert client.find_project_label("Model: Strong") is None

    def test_queries_project_label_namespace_by_name(self) -> None:
        """The lookup hits projectLabels, not issueLabels, with the name filter."""
        seen: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen.update(json.loads(req.content))
            return _json_response({"data": {"projectLabels": {"nodes": []}}})

        client = _client(_make_transport(handler))
        client.find_project_label("Model: Strong")

        assert "projectLabels(" in seen["query"]
        assert "issueLabels" not in seen["query"]
        assert seen["variables"] == {"name": "Model: Strong"}


# ---------------------------------------------------------------------------
# create_project_label
# ---------------------------------------------------------------------------


class TestCreateProjectLabel:
    def test_creates_and_returns_label_id(self) -> None:
        raw = {
            "data": {
                "projectLabelCreate": {
                    "success": True,
                    "projectLabel": {"id": "lbl-new"},
                }
            }
        }
        transport = _make_transport(lambda req: _json_response(raw))
        client = _client(transport)
        assert client.create_project_label("Model: Strong") == "lbl-new"

    def test_failure_raises_linear_error(self) -> None:
        raw = {
            "data": {
                "projectLabelCreate": {
                    "success": False,
                    "projectLabel": None,
                }
            }
        }
        transport = _make_transport(lambda req: _json_response(raw))
        client = _client(transport)
        with pytest.raises(LinearError, match="Failed to create project label"):
            client.create_project_label("Model: Strong")

    def test_sends_only_the_name_in_the_input(self) -> None:
        """No teamId (workspace-wide) and no group fields are sent."""
        seen: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen.update(json.loads(req.content))
            return _json_response(
                {
                    "data": {
                        "projectLabelCreate": {
                            "success": True,
                            "projectLabel": {"id": "lbl-new"},
                        }
                    }
                }
            )

        client = _client(_make_transport(handler))
        client.create_project_label("Model: Strong")

        assert "projectLabelCreate(" in seen["query"]
        assert seen["variables"] == {"input": {"name": "Model: Strong"}}
