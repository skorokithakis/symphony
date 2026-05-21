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

    def test_auth_401_raises(self) -> None:
        transport = _make_transport(
            lambda req: _json_response(
                {"errors": [{"message": "Bad credentials"}]}, 401
            )
        )
        client = _client(transport)
        with pytest.raises(GitHubAuthError):
            client._query("query { viewer { id } }")

    def test_auth_403_raises(self) -> None:
        transport = _make_transport(
            lambda req: _json_response(
                {"errors": [{"message": "Resource not accessible"}]}, 403
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

    def test_transient_500_raises(self) -> None:
        transport = _make_transport(lambda req: _json_response({}, 500))
        client = _client(transport)
        with pytest.raises(GitHubTransientError):
            client._query("query { viewer { id } }")

    def test_transient_502_raises(self) -> None:
        transport = _make_transport(lambda req: _json_response({}, 502))
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
    def test_returns_data_payload(self) -> None:
        transport = _make_transport(
            lambda req: _json_response({"data": {"hello": "world"}})
        )
        client = _client(transport)
        result = client._query("query { hello }")
        assert result == {"hello": "world"}

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
