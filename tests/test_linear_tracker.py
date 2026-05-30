"""Tests for the LinearTracker adapter.

Verifies that LinearTracker implements the Tracker protocol and correctly
delegates to the underlying LinearClient.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from symphony_linear.config import _LinearConfig
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
from symphony_linear.linear_tracker import (
    LinearTracker,
    _LinearAuth,
)
from symphony_linear.state import StateManager
from symphony_linear.tracker import (
    AttachmentDownloadError,
    AttachmentTooLargeError,
    Tracker,
    TrackerAuthError,
    TrackerError,
    TrackerNotFoundError,
    TrackerRateLimitError,
    TrackerTransientError,
    TransitionTarget,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config() -> _LinearConfig:
    return _LinearConfig(
        api_key="test-key",
        trigger_label="Agent",
        in_progress_state="In Progress",
        needs_input_state="Needs Input",
        qa_state="QA",
    )


@pytest.fixture
def config_no_qa() -> _LinearConfig:
    return _LinearConfig(
        api_key="test-key",
        trigger_label="Agent",
        in_progress_state="In Progress",
        needs_input_state="Needs Input",
        qa_state=None,
    )


@pytest.fixture
def linear_mock() -> MagicMock:
    return MagicMock(spec=LinearClient)


@pytest.fixture
def tracker(linear_mock: MagicMock, config: _LinearConfig) -> LinearTracker:
    return LinearTracker(linear=linear_mock, config=config)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    """Verify that LinearTracker satisfies the Tracker protocol structurally."""

    def test_is_tracker_instance(self, tracker: LinearTracker) -> None:
        """LinearTracker should be recognised as a Tracker by isinstance."""
        assert isinstance(tracker, Tracker)

    def test_all_methods_present(self) -> None:
        """Every Tracker method must have a corresponding LinearTracker method."""
        protocol_methods = [
            name
            for name in dir(Tracker)
            if not name.startswith("_") and callable(getattr(Tracker, name, None))
        ]
        tracker_methods = [
            name
            for name in dir(LinearTracker)
            if not name.startswith("_") and callable(getattr(LinearTracker, name, None))
        ]
        missing = [m for m in protocol_methods if m not in tracker_methods]
        assert missing == [], f"LinearTracker missing methods: {missing}"


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class TestExceptionHierarchy:
    """Tracker-neutral exceptions must catch their Linear counterparts."""

    def test_tracker_error_catches_linear_error(self) -> None:
        with pytest.raises(TrackerError):
            raise LinearError("test")

    def test_tracker_auth_catches_linear_auth(self) -> None:
        with pytest.raises(TrackerAuthError):
            raise LinearAuthError("test")

    def test_tracker_rate_limit_catches_linear_rate_limit(self) -> None:
        with pytest.raises(TrackerRateLimitError):
            raise LinearRateLimitError("test")

    def test_tracker_transient_catches_linear_transient(self) -> None:
        with pytest.raises(TrackerTransientError):
            raise LinearTransientError("test")

    def test_tracker_not_found_catches_linear_not_found(self) -> None:
        with pytest.raises(TrackerNotFoundError):
            raise LinearNotFoundError("test")

    def test_linear_error_still_catches_linear_subclasses(self) -> None:
        """Existing catch blocks for LinearError must still work."""
        with pytest.raises(LinearError):
            raise LinearAuthError("test")
        with pytest.raises(LinearError):
            raise LinearNotFoundError("test")

    def test_standard_exception_still_catches_all(self) -> None:
        """Plain Exception must still catch everything."""
        with pytest.raises(Exception):
            raise LinearAuthError("test")


# ---------------------------------------------------------------------------
# Method delegation tests
# ---------------------------------------------------------------------------


class TestListTriggeredIssues:
    def test_delegates_with_active_states(
        self, tracker: LinearTracker, linear_mock: MagicMock, config: _LinearConfig
    ) -> None:
        linear_mock.list_triggered_issues.return_value = []
        result = tracker.list_triggered_issues()
        assert result == []
        linear_mock.list_triggered_issues.assert_called_once_with(
            label=config.trigger_label,
            active_states=["In Progress", "Needs Input", "QA"],
        )

    def test_excludes_qa_when_none(
        self, linear_mock: MagicMock, config_no_qa: _LinearConfig
    ) -> None:
        tracker = LinearTracker(linear_mock, config_no_qa)
        linear_mock.list_triggered_issues.return_value = []
        tracker.list_triggered_issues()
        linear_mock.list_triggered_issues.assert_called_once_with(
            label="Agent",
            active_states=["In Progress", "Needs Input"],
        )


class TestGetIssue:
    def test_delegates(self, tracker: LinearTracker, linear_mock: MagicMock) -> None:
        issue = Issue(
            id="i-1",
            identifier="T-1",
            title="Test",
            state="In Progress",
            updatedAt="2025-01-01T00:00:00Z",
        )
        linear_mock.get_issue.return_value = issue
        assert tracker.get_issue("i-1") is issue
        linear_mock.get_issue.assert_called_once_with("i-1")


class TestListCommentsSince:
    def test_delegates(self, tracker: LinearTracker, linear_mock: MagicMock) -> None:
        comments = [Comment(id="c-1", body="hello", createdAt="2025-01-01T00:00:00Z")]
        linear_mock.list_comments_since.return_value = comments
        assert tracker.list_comments_since("i-1", "c-0") == comments
        linear_mock.list_comments_since.assert_called_once_with("i-1", "c-0")

    def test_last_seen_none(
        self, tracker: LinearTracker, linear_mock: MagicMock
    ) -> None:
        linear_mock.list_comments_since.return_value = []
        tracker.list_comments_since("i-1", None)
        linear_mock.list_comments_since.assert_called_once_with("i-1", None)


class TestPostComment:
    def test_delegates(self, tracker: LinearTracker, linear_mock: MagicMock) -> None:
        comment = Comment(id="c-1", body="posted", createdAt="2025-01-01T00:00:00Z")
        linear_mock.post_comment.return_value = comment
        result = tracker.post_comment("i-1", "posted", "test")
        assert result is comment
        linear_mock.post_comment.assert_called_once_with(
            "i-1", "posted\n\n*Symphony · test*"
        )


class TestEditComment:
    def test_delegates(self, tracker: LinearTracker, linear_mock: MagicMock) -> None:
        tracker.edit_comment("c-1", "updated", "test")
        linear_mock.edit_comment.assert_called_once_with(
            "c-1", "updated\n\n*Symphony · test*"
        )


class TestTransitionTo:
    def test_in_progress(self, tracker: LinearTracker, linear_mock: MagicMock) -> None:
        tracker.transition_to("i-1", TransitionTarget.in_progress)
        linear_mock.transition_to_state.assert_called_once_with("i-1", "In Progress")

    def test_needs_input(self, tracker: LinearTracker, linear_mock: MagicMock) -> None:
        tracker.transition_to("i-1", TransitionTarget.needs_input)
        linear_mock.transition_to_state.assert_called_once_with("i-1", "Needs Input")

    def test_qa(self, tracker: LinearTracker, linear_mock: MagicMock) -> None:
        tracker.transition_to("i-1", TransitionTarget.qa)
        linear_mock.transition_to_state.assert_called_once_with("i-1", "QA")

    def test_qa_raises_when_not_configured(
        self, linear_mock: MagicMock, config_no_qa: _LinearConfig
    ) -> None:
        tracker = LinearTracker(linear_mock, config_no_qa)
        with pytest.raises(ValueError, match="No state mapping"):
            tracker.transition_to("i-1", TransitionTarget.qa)
        linear_mock.transition_to_state.assert_not_called()


class TestIsStillTriggered:
    def test_triggered(self, tracker: LinearTracker) -> None:
        issue = Issue(
            id="i-1",
            identifier="T-1",
            title="T",
            state="In Progress",
            labels=["Agent"],
            updatedAt="2025-01-01T00:00:00Z",
        )
        assert tracker.is_still_triggered(issue) is True

    def test_missing_label(self, tracker: LinearTracker) -> None:
        issue = Issue(
            id="i-1",
            identifier="T-1",
            title="T",
            state="In Progress",
            labels=[],
            updatedAt="2025-01-01T00:00:00Z",
        )
        assert tracker.is_still_triggered(issue) is False

    def test_wrong_state(self, tracker: LinearTracker) -> None:
        issue = Issue(
            id="i-1",
            identifier="T-1",
            title="T",
            state="Backlog",
            labels=["Agent"],
            updatedAt="2025-01-01T00:00:00Z",
        )
        assert tracker.is_still_triggered(issue) is False

    def test_archived(self, tracker: LinearTracker) -> None:
        from datetime import datetime, timezone

        issue = Issue(
            id="i-1",
            identifier="T-1",
            title="T",
            state="In Progress",
            labels=["Agent"],
            updatedAt="2025-01-01T00:00:00Z",
            archivedAt=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        assert tracker.is_still_triggered(issue) is False

    def test_qa_state_is_active(self, tracker: LinearTracker) -> None:
        issue = Issue(
            id="i-1",
            identifier="T-1",
            title="T",
            state="QA",
            labels=["Agent"],
            updatedAt="2025-01-01T00:00:00Z",
        )
        assert tracker.is_still_triggered(issue) is True

    def test_qa_state_not_active_when_unconfigured(
        self, config_no_qa: _LinearConfig
    ) -> None:
        linear_mock = MagicMock(spec=LinearClient)
        tracker = LinearTracker(linear_mock, config_no_qa)
        issue = Issue(
            id="i-1",
            identifier="T-1",
            title="T",
            state="QA",
            labels=["Agent"],
            updatedAt="2025-01-01T00:00:00Z",
        )
        # "QA" string is not one of the active states because qa_state is None,
        # so the ticket would NOT be matched in list_triggered_issues.  However,
        # is_still_triggered checks the literal state name — since no qa_state
        # is configured, "QA" is not in the active set.
        assert tracker.is_still_triggered(issue) is False


class TestRepoUrlFor:
    def test_returns_repo_link(
        self, tracker: LinearTracker, linear_mock: MagicMock
    ) -> None:
        project = Project(
            id="p-1",
            name="Backend",
            links=[ProjectLink(label="Repo", url="https://github.com/org/repo")],
        )
        linear_mock.get_project.return_value = project
        issue = Issue(
            id="i-1",
            identifier="T-1",
            title="T",
            state="In Progress",
            updatedAt="2025-01-01T00:00:00Z",
            project=Project(id="p-1", name="Backend"),
        )
        url = tracker.repo_url_for(issue)
        assert url == "git@github.com:org/repo.git"
        linear_mock.get_project.assert_called_once_with("p-1")

    def test_raises_when_no_project(self, tracker: LinearTracker) -> None:
        issue = Issue(
            id="i-1",
            identifier="T-1",
            title="T",
            state="In Progress",
            updatedAt="2025-01-01T00:00:00Z",
        )
        with pytest.raises(TrackerError, match="No project linked"):
            tracker.repo_url_for(issue)

    def test_raises_when_project_has_no_id(self, tracker: LinearTracker) -> None:
        issue = Issue(
            id="i-1",
            identifier="T-1",
            title="T",
            state="In Progress",
            updatedAt="2025-01-01T00:00:00Z",
            project=Project(id="", name="Empty"),
        )
        with pytest.raises(TrackerError, match="No project linked"):
            tracker.repo_url_for(issue)

    def test_raises_when_no_repo_link(
        self, tracker: LinearTracker, linear_mock: MagicMock
    ) -> None:
        project = Project(
            id="p-1",
            name="Backend",
            links=[ProjectLink(label="Docs", url="https://docs.example.com")],
        )
        linear_mock.get_project.return_value = project
        issue = Issue(
            id="i-1",
            identifier="T-1",
            title="T",
            state="In Progress",
            updatedAt="2025-01-01T00:00:00Z",
            project=Project(id="p-1", name="Backend"),
        )
        with pytest.raises(TrackerError, match="Repo.*link"):
            tracker.repo_url_for(issue)


class TestEnsureTriggerSetup:
    def test_calls_provision_trigger_label(
        self, tracker: LinearTracker, linear_mock: MagicMock, tmp_path: Any
    ) -> None:
        from unittest.mock import patch

        state = StateManager(tmp_path / "state.json")
        state.load()

        # provision_trigger_label is imported at module top-level in
        # linear_tracker.py; we patch the reference in that module so the
        # test doesn't require a real Linear connection.
        with patch(
            "symphony_linear.linear_tracker.provision_trigger_label"
        ) as mock_provision:
            tracker.ensure_trigger_setup(state)
            mock_provision.assert_called_once_with(linear_mock, state, "Agent")


class TestHumanTriggerDescription:
    def test_includes_label_name(self, tracker: LinearTracker) -> None:
        assert tracker.human_trigger_description() == "remove the `Agent` label"

    def test_custom_label(self, linear_mock: MagicMock) -> None:
        config = _LinearConfig(
            api_key="k",
            trigger_label="Symphony",
            in_progress_state="IP",
            needs_input_state="NI",
        )
        t = LinearTracker(linear_mock, config)
        assert t.human_trigger_description() == "remove the `Symphony` label"


# ---------------------------------------------------------------------------
# _maybe_rewrite_to_ssh
# ---------------------------------------------------------------------------


class TestMaybeRewriteToSsh:
    def test_https_github_converts(self) -> None:
        from symphony_linear.linear_tracker import _maybe_rewrite_to_ssh

        assert (
            _maybe_rewrite_to_ssh("https://github.com/org/repo")
            == "git@github.com:org/repo.git"
        )

    def test_https_github_trailing_slash(self) -> None:
        from symphony_linear.linear_tracker import _maybe_rewrite_to_ssh

        assert (
            _maybe_rewrite_to_ssh("https://github.com/org/repo/")
            == "git@github.com:org/repo.git"
        )

    def test_https_dot_git_passthrough(self) -> None:
        from symphony_linear.linear_tracker import _maybe_rewrite_to_ssh

        url = "https://github.com/org/repo.git"
        assert _maybe_rewrite_to_ssh(url) == url

    def test_git_protocol_passthrough(self) -> None:
        from symphony_linear.linear_tracker import _maybe_rewrite_to_ssh

        url = "git@github.com:org/repo.git"
        assert _maybe_rewrite_to_ssh(url) == url

    def test_non_github_passthrough(self) -> None:
        from symphony_linear.linear_tracker import _maybe_rewrite_to_ssh

        url = "https://gitlab.com/org/repo"
        assert _maybe_rewrite_to_ssh(url) == url

    def test_http_passthrough(self) -> None:
        from symphony_linear.linear_tracker import _maybe_rewrite_to_ssh

        url = "http://github.com/org/repo"
        assert _maybe_rewrite_to_ssh(url) == url


# ---------------------------------------------------------------------------
# QA helpers
# ---------------------------------------------------------------------------


class TestIsInQa:
    """Tests for is_in_qa."""

    def test_true_when_in_qa(self, tracker: LinearTracker) -> None:
        issue = Issue(
            id="i-1",
            identifier="T-1",
            title="T",
            state="QA",
            updatedAt="2025-01-01T00:00:00Z",
        )
        assert tracker.is_in_qa(issue) is True

    def test_false_when_not_qa(self, tracker: LinearTracker) -> None:
        issue = Issue(
            id="i-1",
            identifier="T-1",
            title="T",
            state="In Progress",
            updatedAt="2025-01-01T00:00:00Z",
        )
        assert tracker.is_in_qa(issue) is False

    def test_false_when_qa_not_configured(self, config_no_qa: _LinearConfig) -> None:
        linear_mock = MagicMock(spec=LinearClient)
        tracker = LinearTracker(linear_mock, config_no_qa)
        issue = Issue(
            id="i-1",
            identifier="T-1",
            title="T",
            state="QA",
            updatedAt="2025-01-01T00:00:00Z",
        )
        assert tracker.is_in_qa(issue) is False


class TestQaEnabled:
    def test_true_when_configured(self, tracker: LinearTracker) -> None:
        assert tracker.qa_enabled is True

    def test_false_when_not_configured(self, config_no_qa: _LinearConfig) -> None:
        linear_mock = MagicMock(spec=LinearClient)
        tracker = LinearTracker(linear_mock, config_no_qa)
        assert tracker.qa_enabled is False


class TestTransitionNameFor:
    def test_in_progress(self, tracker: LinearTracker) -> None:
        assert (
            tracker.transition_name_for(TransitionTarget.in_progress) == "In Progress"
        )

    def test_needs_input(self, tracker: LinearTracker) -> None:
        assert (
            tracker.transition_name_for(TransitionTarget.needs_input) == "Needs Input"
        )

    def test_qa(self, tracker: LinearTracker) -> None:
        assert tracker.transition_name_for(TransitionTarget.qa) == "QA"

    def test_qa_raises_when_not_configured(self, config_no_qa: _LinearConfig) -> None:
        linear_mock = MagicMock(spec=LinearClient)
        tracker = LinearTracker(linear_mock, config_no_qa)
        with pytest.raises(ValueError, match="qa_state is not configured"):
            tracker.transition_name_for(TransitionTarget.qa)


# ---------------------------------------------------------------------------
# download_attachment
# ---------------------------------------------------------------------------


class TestDownloadAttachment:
    """Tests for download_attachment on LinearTracker."""

    def test_success_returns_bytes_and_content_type(
        self, tracker: LinearTracker
    ) -> None:
        from unittest.mock import patch

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/plain; charset=utf-8"}
        mock_response.iter_bytes.return_value = [b"hello", b" world"]

        with patch("symphony_linear.linear_tracker.httpx.stream") as mock_stream:
            mock_stream.return_value.__enter__.return_value = mock_response
            data, ct = tracker.download_attachment("https://uploads.linear.app/f.txt")

        assert data == b"hello world"
        assert ct == "text/plain"
        mock_stream.assert_called_once()
        call_kwargs = mock_stream.call_args.kwargs
        assert isinstance(call_kwargs["auth"], _LinearAuth)
        assert call_kwargs["auth"]._api_key == "test-key"
        assert call_kwargs["follow_redirects"] is True
        assert call_kwargs["timeout"] == 30.0

    def test_missing_content_type_returns_none(self, tracker: LinearTracker) -> None:
        from unittest.mock import patch

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}  # no content-type
        mock_response.iter_bytes.return_value = [b"data"]

        with patch("symphony_linear.linear_tracker.httpx.stream") as mock_stream:
            mock_stream.return_value.__enter__.return_value = mock_response
            data, ct = tracker.download_attachment("https://public.linear.app/f.bin")

        assert data == b"data"
        assert ct is None

    def test_http_404_raises_attachment_download_error(
        self, tracker: LinearTracker
    ) -> None:
        from unittest.mock import patch

        mock_response = MagicMock()
        mock_response.status_code = 404

        with patch("symphony_linear.linear_tracker.httpx.stream") as mock_stream:
            mock_stream.return_value.__enter__.return_value = mock_response
            with pytest.raises(AttachmentDownloadError, match="HTTP 404"):
                tracker.download_attachment("https://uploads.linear.app/missing.txt")

    def test_content_length_exceeds_limit_raises_too_large(
        self, tracker: LinearTracker
    ) -> None:
        from unittest.mock import patch

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-length": str(20 * 1024 * 1024)}

        with patch("symphony_linear.linear_tracker.httpx.stream") as mock_stream:
            mock_stream.return_value.__enter__.return_value = mock_response
            with pytest.raises(AttachmentTooLargeError, match="bytes"):
                tracker.download_attachment("https://uploads.linear.app/huge.bin")

    def test_oversize_response_body_raises_too_large(
        self, tracker: LinearTracker
    ) -> None:
        from unittest.mock import patch

        big_chunk = b"x" * (11 * 1024 * 1024)  # > 10 MB

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}
        mock_response.iter_bytes.return_value = [big_chunk]

        with patch("symphony_linear.linear_tracker.httpx.stream") as mock_stream:
            mock_stream.return_value.__enter__.return_value = mock_response
            with pytest.raises(AttachmentTooLargeError, match="exceeds"):
                tracker.download_attachment("https://uploads.linear.app/huge.bin")

    def test_network_error_raises_attachment_download_error(
        self, tracker: LinearTracker
    ) -> None:
        from unittest.mock import patch

        import httpx

        with patch(
            "symphony_linear.linear_tracker.httpx.stream",
            side_effect=httpx.NetworkError("connection reset"),
        ):
            with pytest.raises(AttachmentDownloadError, match="Download failed"):
                tracker.download_attachment("https://uploads.linear.app/f.txt")

    def test_timeout_raises_attachment_download_error(
        self, tracker: LinearTracker
    ) -> None:
        from unittest.mock import patch

        import httpx

        with patch(
            "symphony_linear.linear_tracker.httpx.stream",
            side_effect=httpx.TimeoutException("timed out"),
        ):
            with pytest.raises(AttachmentDownloadError, match="Download failed"):
                tracker.download_attachment("https://uploads.linear.app/f.txt")

    # ------------------------------------------------------------------
    # SSRF defence — host allowlist validation before any I/O
    # ------------------------------------------------------------------

    def test_allowlisted_host_downloads(self, tracker: LinearTracker) -> None:
        from unittest.mock import patch

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/plain"}
        mock_response.iter_bytes.return_value = [b"data"]

        with patch("symphony_linear.linear_tracker.httpx.stream") as mock_stream:
            mock_stream.return_value.__enter__.return_value = mock_response
            data, ct = tracker.download_attachment(
                "https://uploads.linear.app/file.png"
            )

        assert data == b"data"
        mock_stream.assert_called_once()

    def test_non_allowlisted_host_raises_before_http(
        self, tracker: LinearTracker
    ) -> None:
        from unittest.mock import patch

        with patch("symphony_linear.linear_tracker.httpx.stream") as mock_stream:
            with pytest.raises(AttachmentDownloadError, match="host not on allowlist"):
                tracker.download_attachment("https://evil.example.com/x.png")

        mock_stream.assert_not_called()

    def test_http_allowlisted_host_raises_before_http(
        self, tracker: LinearTracker
    ) -> None:
        from unittest.mock import patch

        with patch("symphony_linear.linear_tracker.httpx.stream") as mock_stream:
            with pytest.raises(AttachmentDownloadError, match="host not on allowlist"):
                tracker.download_attachment("http://uploads.linear.app/x.png")

        mock_stream.assert_not_called()

    def test_ip_literal_raises_before_http(self, tracker: LinearTracker) -> None:
        from unittest.mock import patch

        with patch("symphony_linear.linear_tracker.httpx.stream") as mock_stream:
            with pytest.raises(AttachmentDownloadError, match="host not on allowlist"):
                tracker.download_attachment("https://169.254.169.254/x.png")

        mock_stream.assert_not_called()


# ---------------------------------------------------------------------------
# _LinearAuth (host-based auth header restriction)
# ---------------------------------------------------------------------------


class TestLinearAuth:
    """Tests for the _LinearAuth custom httpx.Auth class."""

    def test_allowlisted_host_gets_auth_header(self) -> None:
        import httpx

        auth = _LinearAuth("my-api-key")
        req = httpx.Request("GET", "https://uploads.linear.app/some-file.png")
        flow = auth.auth_flow(req)
        result = next(flow)
        assert result.headers["Authorization"] == "my-api-key"

    def test_allowlisted_host_public(self) -> None:
        import httpx

        auth = _LinearAuth("my-api-key")
        req = httpx.Request("GET", "https://public.linear.app/image.png")
        flow = auth.auth_flow(req)
        result = next(flow)
        assert result.headers["Authorization"] == "my-api-key"

    def test_non_allowlisted_host_gets_no_auth_header(self) -> None:
        import httpx

        auth = _LinearAuth("my-api-key")
        req = httpx.Request("GET", "https://evil.example.com/log")
        flow = auth.auth_flow(req)
        result = next(flow)
        assert "Authorization" not in result.headers

    def test_http_allowlisted_host_denied(self) -> None:
        """An HTTP (not HTTPS) request to an allowlisted host does NOT
        receive the auth header — prevents plaintext token leaks."""
        import httpx

        auth = _LinearAuth("my-api-key")
        req = httpx.Request("GET", "http://uploads.linear.app/some-file.png")
        flow = auth.auth_flow(req)
        result = next(flow)
        assert "Authorization" not in result.headers

    def test_cross_origin_redirect_strips_auth(self) -> None:
        """Verify that httpx itself strips the Authorization header on
        cross-origin redirects (not our Auth class — httpx does this in
        ``_redirect_headers``).  This is a real integration test using
        ``MockTransport`` with ``follow_redirects=True``.

        Rationale (path A, see ticket S6-zalxc):
        httpx ≥ 0.20 strips the ``Authorization`` header on cross-origin
        redirects automatically (see ``_redirect_headers`` in
        ``httpx/_client.py``).  We rely on that behaviour and keep our
        ``_LinearAuth`` class as defence-in-depth for the first request.
        This test confirms that when ``uploads.linear.app`` redirects to
        an external host the second hop carries no credentials.
        """
        import httpx

        target_seen = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal target_seen
            if request.url.host == "uploads.linear.app":
                return httpx.Response(
                    302,
                    headers={"Location": "https://evil.example.com/exfiltrate"},
                )
            # This is the redirect target — must NOT carry Authorization.
            target_seen = True
            assert "Authorization" not in request.headers
            return httpx.Response(200, content=b"ok")

        transport = httpx.MockTransport(handler)
        auth = _LinearAuth("my-api-key")

        with httpx.Client(
            transport=transport,
            auth=auth,
            follow_redirects=True,
            base_url="https://uploads.linear.app",
        ) as client:
            resp = client.get("/some-file.png")

        assert resp.status_code == 200
        assert target_seen, "redirect target was never requested"
