"""Tests for the Linear GraphQL API client.

Uses ``httpx.MockTransport`` to simulate Linear API responses — no real
network calls.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from symphony_lite.linear import (
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


# ---------------------------------------------------------------------------
# Fixtures – sample data
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_issue_raw() -> dict[str, Any]:
    return {
        "id": "abc-123",
        "identifier": "TEAM-42",
        "title": "Fix the thing",
        "state": {"name": "In Progress"},
        "labels": {"nodes": [{"name": "agent"}, {"name": "bug"}]},
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
            client.current_user_id()

    def test_auth_403_raises(self) -> None:
        transport = _make_transport(
            lambda req: _json_response(
                {"errors": [{"message": "Forbidden"}]}, 403
            )
        )
        client = _client(transport)
        with pytest.raises(LinearAuthError):
            client.current_user_id()

    def test_rate_limit_429_raises(self) -> None:
        transport = _make_transport(lambda req: _json_response({}, 429))
        client = _client(transport)
        with pytest.raises(LinearRateLimitError):
            client.current_user_id()

    def test_transient_500_raises(self) -> None:
        transport = _make_transport(lambda req: _json_response({}, 500))
        client = _client(transport)
        with pytest.raises(LinearTransientError):
            client.current_user_id()

    def test_transient_502_raises(self) -> None:
        transport = _make_transport(lambda req: _json_response({}, 502))
        client = _client(transport)
        with pytest.raises(LinearTransientError):
            client.current_user_id()

    def test_not_found_via_graphql_error(self) -> None:
        transport = _make_transport(
            lambda req: _json_response(
                {"errors": [{"message": "not found"}]}, 200
            )
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
# current_user_id
# ---------------------------------------------------------------------------


class TestCurrentUserId:
    def test_returns_viewer_id(self) -> None:
        transport = _make_transport(
            lambda req: _json_response({"data": {"viewer": {"id": "usr-abc"}}})
        )
        client = _client(transport)
        assert client.current_user_id() == "usr-abc"

    def test_caches_result(self) -> None:
        calls: list[int] = [0]

        def handler(req: httpx.Request) -> httpx.Response:
            calls[0] += 1
            return _json_response({"data": {"viewer": {"id": "usr-abc"}}})

        transport = _make_transport(handler)
        client = _client(transport)
        assert client.current_user_id() == "usr-abc"
        assert client.current_user_id() == "usr-abc"
        assert calls[0] == 1  # only one HTTP call made


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
                            "state": {"name": "In Progress"},
                            "labels": {"nodes": [{"name": "agent"}]},
                            "branchName": "tea-1-do-a",
                            "project": {"id": "p-1", "name": "Core"},
                        },
                        {
                            "id": "i-2",
                            "identifier": "TEA-2",
                            "title": "Do B",
                            "state": {"name": "In Progress"},
                            "labels": {"nodes": [{"name": "agent"}]},
                            "branchName": None,
                            "project": None,
                        },
                    ]
                }
            }
        }
        transport = _make_transport(lambda req: _json_response(raw))
        client = _client(transport)
        issues = client.list_triggered_issues("agent", ["In Progress"])

        assert len(issues) == 2
        assert issues[0].id == "i-1"
        assert issues[0].identifier == "TEA-1"
        assert issues[0].state == "In Progress"
        assert issues[0].labels == ["agent"]
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
                            "state": {"name": "In Progress"},
                            "labels": {"nodes": [{"name": "agent"}]},
                            "branchName": None,
                            "project": None,
                        },
                        {
                            "id": "i-2",
                            "identifier": "TEA-2",
                            "title": "B",
                            "state": {"name": "Backlog"},
                            "labels": {"nodes": [{"name": "agent"}]},
                            "branchName": None,
                            "project": None,
                        },
                    ]
                }
            }
        }
        transport = _make_transport(lambda req: _json_response(raw))
        client = _client(transport)
        issues = client.list_triggered_issues("agent", ["In Progress"])
        assert len(issues) == 1
        assert issues[0].id == "i-1"

    def test_empty_result(self) -> None:
        raw = {"data": {"issues": {"nodes": []}}}
        transport = _make_transport(lambda req: _json_response(raw))
        client = _client(transport)
        issues = client.list_triggered_issues("agent", ["In Progress"])
        assert issues == []


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
        assert issue.labels == ["agent", "bug"]
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


# ---------------------------------------------------------------------------
# get_project
# ---------------------------------------------------------------------------


class TestGetProject:
    def test_parses_project_with_links(self, sample_project_raw: dict[str, Any]) -> None:
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
    def _comments_issue_raw(self, comment_nodes: list[dict[str, Any]]) -> dict[str, Any]:
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
            self._make_comment("cmt-6", "Sixth"),   # newest
            self._make_comment("cmt-5", "Fifth"),
            self._make_comment("cmt-4", "Fourth"),
            self._make_comment("cmt-3", "Third"),    # last_seen
            self._make_comment("cmt-2", "Second"),
            self._make_comment("cmt-1", "First"),    # oldest
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
                return _json_response(
                    {"data": {"issueUpdate": {"success": True}}}
                )
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
            lambda req: _json_response(
                {"data": {"issue": {"team": None}}}, 200
            )
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
                return _json_response(
                    {"data": {"issueUpdate": {"success": False}}}
                )
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
            id="i", identifier="T-1", title="T", state="S",
            branchName="feat/x",
        )
        assert issue.branch_name == "feat/x"

    def test_defaults(self) -> None:
        issue = Issue(id="i", identifier="T-1", title="T", state="S")
        assert issue.labels == []
        assert issue.comments == []
        assert issue.branch_name is None
        assert issue.project is None
