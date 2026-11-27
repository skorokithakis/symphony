"""Tests for the GitHub GraphQL API client.

Uses ``httpx.MockTransport`` to simulate API responses — no real
network calls.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from symphony_linear.github import (
    GitHubAuthError,
    GitHubClient,
    GitHubError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubTransientError,
)


# ---------------------------------------------------------------------------
# Helpers – build canned responses
# ---------------------------------------------------------------------------


def _make_transport(
    handler: Any,
) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _client(transport: httpx.MockTransport) -> GitHubClient:
    return GitHubClient(
        token="test-token",
        client=httpx.Client(transport=transport, base_url="http://mock"),
    )


def _json_response(data: dict[str, Any], status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=data)


# ---------------------------------------------------------------------------
# Exception mapping tests
# ---------------------------------------------------------------------------


class TestExceptionMapping:
    """Verify HTTP status codes and GraphQL errors map to the correct typed
    exceptions."""

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_status_raises(self, status: int) -> None:
        transport = _make_transport(
            lambda req: _json_response(
                {"errors": [{"message": "Bad credentials"}]}, status
            )
        )
        client = _client(transport)
        with pytest.raises(GitHubAuthError):
            client._query("query { viewer { id } }")

    def test_rate_limit_403_via_header_raises(self) -> None:
        transport = _make_transport(
            lambda req: httpx.Response(
                403,
                json={"errors": [{"message": "Rate limit"}]},
                headers={"X-RateLimit-Remaining": "0"},
            )
        )
        client = _client(transport)
        with pytest.raises(GitHubRateLimitError):
            client._query("query { viewer { id } }")

    def test_not_found_404_raises(self) -> None:
        transport = _make_transport(lambda req: _json_response({}, 404))
        client = _client(transport)
        with pytest.raises(GitHubNotFoundError):
            client._query("query { viewer { id } }")

    def test_rate_limit_429_raises(self) -> None:
        transport = _make_transport(lambda req: _json_response({}, 429))
        client = _client(transport)
        with pytest.raises(GitHubRateLimitError):
            client._query("query { viewer { id } }")

    def test_rate_limit_via_graphql_error(self) -> None:
        transport = _make_transport(
            lambda req: _json_response(
                {
                    "errors": [
                        {
                            "message": "API rate limit exceeded",
                            "type": "RATE_LIMITED",
                        }
                    ]
                },
                200,
            )
        )
        client = _client(transport)
        with pytest.raises(GitHubRateLimitError):
            client._query("query { viewer { id } }")

    @pytest.mark.parametrize("status", [500, 502])
    def test_transient_status_raises(self, status: int) -> None:
        transport = _make_transport(lambda req: _json_response({}, status))
        client = _client(transport)
        with pytest.raises(GitHubTransientError):
            client._query("query { viewer { id } }")

    def test_not_found_via_graphql_error(self) -> None:
        transport = _make_transport(
            lambda req: _json_response(
                {
                    "errors": [
                        {"message": "Could not resolve to a node", "type": "NOT_FOUND"}
                    ]
                },
                200,
            )
        )
        client = _client(transport)
        with pytest.raises(GitHubNotFoundError):
            client._query('query { node(id: "x") { id } }')

    def test_not_found_via_message(self) -> None:
        transport = _make_transport(
            lambda req: _json_response(
                {"errors": [{"message": "not found somewhere"}]}, 200
            )
        )
        client = _client(transport)
        with pytest.raises(GitHubNotFoundError):
            client._query('query { node(id: "x") { id } }')

    def test_generic_graphql_error(self) -> None:
        transport = _make_transport(
            lambda req: _json_response(
                {"errors": [{"message": "Something went wrong"}]}, 200
            )
        )
        client = _client(transport)
        with pytest.raises(GitHubError, match="Something went wrong"):
            client._query("query { x }")

    def test_timeout_raises_transient(self) -> None:
        # The httpx mock transport might not actually trigger TimeoutException.
        # We test the catch clause via a direct raise instead.
        import httpx as _httpx

        client2 = GitHubClient(
            token="t",
            client=_httpx.Client(
                transport=_httpx.MockTransport(
                    lambda req: (_ for _ in ()).throw(
                        _httpx.TimeoutException("timed out")
                    )
                ),
                base_url="http://mock",
            ),
        )
        with pytest.raises(GitHubTransientError, match="timed out"):
            client2._query("query { viewer { id } }")


# ---------------------------------------------------------------------------
# _query helper
# ---------------------------------------------------------------------------


class TestQuery:
    def test_passes_variables(self) -> None:
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["body"] = req.read().decode()
            return _json_response({"data": {"ok": True}})

        transport = _make_transport(handler)
        client = _client(transport)
        client._query("query($x: Int!) { stuff }", {"x": 42})
        assert '"variables":{"x":42}' in captured["body"]

    def test_empty_response_body(self) -> None:
        transport = _make_transport(lambda req: httpx.Response(200, content=b""))
        client = _client(transport)
        result = client._query("query { x }")
        assert result == {}


# ---------------------------------------------------------------------------
# Path-scoped error tolerance
# ---------------------------------------------------------------------------


class TestToleratePathErrors:
    """Verify the ``tolerate_path_errors`` kwarg on ``_query`` correctly
    allows path-scoped GraphQL errors through when they are the only kind
    present."""

    @pytest.mark.parametrize(
        "messages",
        [
            ["Resource not accessible by personal access token"],
            ["First forbidden", "Second forbidden"],
        ],
    )
    def test_path_only_returns_data_and_logs_every_error(
        self, caplog, messages: list[str]
    ) -> None:
        """Every path-scoped error is logged (not just the first), data returned."""
        response = {
            "data": {"node": {"items": {"nodes": [{"id": "PVTI_1"}]}}},
            "errors": [
                {
                    "type": "FORBIDDEN",
                    "path": ["node", "items", "nodes", index, "content"],
                    "message": message,
                }
                for index, message in enumerate(messages)
            ],
        }
        transport = _make_transport(lambda req: _json_response(response))
        client = _client(transport)

        import logging

        with caplog.at_level(logging.WARNING):
            result = client._query("query { ... }", tolerate_path_errors=True)

        assert result == response["data"]
        assert len(caplog.records) == len(messages)
        assert all(record.levelno == logging.WARNING for record in caplog.records)
        assert all("path-scoped error" in record.message for record in caplog.records)
        for message in messages:
            assert message in caplog.text

    def test_mixed_path_and_root_errors_raises(self) -> None:
        """At least one error has no path → full error handling (raise)."""
        response = {
            "data": {"node": None},
            "errors": [
                {
                    "type": "FORBIDDEN",
                    "path": ["node", "items", "nodes", 0, "content"],
                    "message": "Resource not accessible by personal access token",
                },
                {
                    "type": "SOME_ERROR",
                    "message": "Something went wrong at the root",
                },
            ],
        }
        transport = _make_transport(lambda req: _json_response(response))
        client = _client(transport)
        with pytest.raises(GitHubError):
            client._query("query { ... }", tolerate_path_errors=True)

    def test_path_error_with_flag_off_raises(self) -> None:
        """Path-scoped errors still raise when the flag is False (default)."""
        response = {
            "data": {"node": None},
            "errors": [
                {
                    "type": "FORBIDDEN",
                    "path": ["node", "items", "nodes", 0, "content"],
                    "message": "Resource not accessible by personal access token",
                }
            ],
        }
        transport = _make_transport(lambda req: _json_response(response))
        client = _client(transport)
        with pytest.raises(GitHubError):
            client._query("query { ... }")

    def test_root_auth_error_with_flag_on_raises_auth(self) -> None:
        """A root-level auth error raises GitHubAuthError even with the
        flag on (because it has no path)."""
        response = {
            "errors": [
                {
                    "type": "AUTHENTICATION",
                    "message": "Authentication failed",
                }
            ],
        }
        transport = _make_transport(lambda req: _json_response(response, 200))
        client = _client(transport)
        with pytest.raises(GitHubAuthError):
            client._query("query { ... }", tolerate_path_errors=True)
