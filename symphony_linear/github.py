"""Thin synchronous wrapper around GitHub's GraphQL API.

Follows the same error-mapping pattern as ``LinearClient``: HTTP and
GraphQL-level errors are mapped to the tracker-neutral ``TrackerError``
hierarchy defined in ``tracker.py``.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from symphony_linear.tracker import (
    TrackerAuthError,
    TrackerError,
    TrackerNotFoundError,
    TrackerRateLimitError,
    TrackerTransientError,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"

# ---------------------------------------------------------------------------
# Typed exceptions
# ---------------------------------------------------------------------------


class GitHubError(TrackerError):
    """Base exception for all GitHub API errors."""


class GitHubAuthError(TrackerAuthError, GitHubError):
    """Authentication failed (HTTP 401/403)."""


class GitHubRateLimitError(TrackerRateLimitError, GitHubError):
    """Rate limited (HTTP 429 or ``RATE_LIMITED`` GraphQL error code)."""


class GitHubTransientError(TrackerTransientError, GitHubError):
    """Transient server / network error (HTTP 5xx, timeouts)."""


class GitHubNotFoundError(TrackerNotFoundError, GitHubError):
    """Resource not found."""


# ---------------------------------------------------------------------------
# Response-parsing helpers
# ---------------------------------------------------------------------------


def _raise_for_status(status_code: int, response: httpx.Response | None = None) -> None:
    """Map HTTP status codes to typed exceptions.

    Called *before* we parse the response body so that transport-layer
    errors are caught early.  When the response object is available, the
    ``X-RateLimit-Remaining`` header is inspected to disambiguate 403
    responses (auth vs. rate limit).
    """
    if status_code == 403:
        if response is not None:
            remaining = response.headers.get("X-RateLimit-Remaining")
            if remaining == "0":
                raise GitHubRateLimitError("HTTP 403 – rate limit exhausted")
        raise GitHubAuthError(f"HTTP {status_code}")
    if status_code in (401,):
        raise GitHubAuthError(f"HTTP {status_code}")
    if status_code == 404:
        raise GitHubNotFoundError(f"HTTP {status_code}")
    if status_code == 429:
        raise GitHubRateLimitError("HTTP 429 – rate limited")
    if 500 <= status_code < 600:
        raise GitHubTransientError(f"HTTP {status_code}")


def _parse_graphql_errors(
    response_data: dict[str, Any],
    status_code: int,
) -> None:
    """Inspect a GraphQL response for application-level errors and raise the
    appropriate typed exception.

    GitHub returns ``errors`` alongside partial ``data`` when something
    goes wrong at the GraphQL layer.  We classify known error types;
    otherwise we raise a generic ``GitHubError`` with the first message.
    """
    errors: list[dict[str, Any]] = response_data.get("errors", [])
    if not errors:
        return

    for err in errors:
        msg = err.get("message", "")
        error_type = err.get("type", "")

        # Auth-related (only 401 reaches here; 403 is handled upstream).
        if status_code == 401 or "authentication" in msg.lower():
            raise GitHubAuthError(msg)

        # Not-found – either via type or message content.
        if error_type == "NOT_FOUND" or "not found" in msg.lower():
            raise GitHubNotFoundError(msg)

        # Rate limit.
        if error_type == "RATE_LIMITED" or "rate limit" in msg.lower():
            raise GitHubRateLimitError(msg)

    if errors:
        raise GitHubError(errors[0].get("message", "Unknown GraphQL error"))


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class GitHubClient:
    """Minimal synchronous GitHub GraphQL client.

    Args:
        token: GitHub personal-access token or installation token.
        client: Optional pre-configured ``httpx.Client`` for testing.
    """

    def __init__(self, token: str, *, client: httpx.Client | None = None) -> None:
        self._token = token
        # Note: we pass the full URL on each request rather than using
        # httpx's ``base_url``. With ``base_url=".../graphql"`` and a
        # relative path of ``""``, httpx appends a trailing slash
        # (``.../graphql/``) which GitHub responds to with HTTP 404.
        self._client = client or httpx.Client(
            headers={
                "Authorization": f"Bearer {token}",
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

        Raises typed ``GitHub*Error`` subclasses on HTTP or GraphQL-level
        errors.
        """
        logger.debug("GitHub GraphQL request: %s", _first_line(query))
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        try:
            response = self._client.post(GITHUB_GRAPHQL_URL, json=payload)
        except httpx.TimeoutException as exc:
            raise GitHubTransientError("Request timed out") from exc
        except httpx.NetworkError as exc:
            raise GitHubTransientError(f"Network error: {exc}") from exc

        _raise_for_status(response.status_code, response)

        body: dict[str, Any] = response.json() if response.content else {}
        _parse_graphql_errors(body, response.status_code)

        return body.get("data", {})

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def current_user_id(self) -> str:
        """Return the GitHub user id of the authenticated principal.

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _first_line(text: str) -> str:
    """Return the first non-empty line of *text* for log messages."""
    for line in text.strip().splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return text[:80]
