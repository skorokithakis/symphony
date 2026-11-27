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
def config_no_label() -> _LinearConfig:
    return _LinearConfig(
        api_key="test-key",
        trigger_label=None,
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
        assert isinstance(tracker, Tracker)


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class TestExceptionHierarchy:
    """Tracker-neutral exceptions must catch their Linear counterparts."""

    @pytest.mark.parametrize(
        ("linear_error", "tracker_error"),
        [
            (LinearError, TrackerError),
            (LinearAuthError, TrackerAuthError),
            (LinearRateLimitError, TrackerRateLimitError),
            (LinearTransientError, TrackerTransientError),
            (LinearNotFoundError, TrackerNotFoundError),
        ],
    )
    def test_tracker_error_catches_linear_error(
        self, linear_error: type[Exception], tracker_error: type[Exception]
    ) -> None:
        with pytest.raises(tracker_error):
            raise linear_error("test")

    def test_linear_error_still_catches_linear_subclasses(self) -> None:
        """Existing catch blocks for LinearError must still work."""
        with pytest.raises(LinearError):
            raise LinearAuthError("test")
        with pytest.raises(LinearError):
            raise LinearNotFoundError("test")


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

    def test_none_label_filters_issues_without_repo_link(
        self,
        linear_mock: MagicMock,
        config_no_label: _LinearConfig,
    ) -> None:
        tracker = LinearTracker(linear_mock, config_no_label)
        repo_issue = Issue(
            id="i-1",
            identifier="T-1",
            title="Repo",
            state="In Progress",
            updatedAt="2025-01-01T00:00:00Z",
            project=Project(
                id="p-1",
                name="Repo project",
                links=[ProjectLink(label=" Repo ", url="https://github.com/org/repo")],
            ),
        )
        no_repo_issue = Issue(
            id="i-2",
            identifier="T-2",
            title="No repo",
            state="In Progress",
            labels=["Agent"],
            updatedAt="2025-01-01T00:00:00Z",
            project=Project(
                id="p-2",
                name="Docs project",
                links=[ProjectLink(label="Docs", url="https://docs.example.com")],
            ),
        )
        linear_mock.list_triggered_issues.return_value = [repo_issue, no_repo_issue]

        assert tracker.list_triggered_issues() == [repo_issue]
        linear_mock.list_triggered_issues.assert_called_once_with(
            label=None,
            active_states=["In Progress", "Needs Input"],
        )


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
    @pytest.mark.parametrize(
        ("target", "state_name"),
        [
            (TransitionTarget.in_progress, "In Progress"),
            (TransitionTarget.needs_input, "Needs Input"),
            (TransitionTarget.qa, "QA"),
        ],
    )
    def test_maps_target_to_configured_state(
        self,
        tracker: LinearTracker,
        linear_mock: MagicMock,
        target: TransitionTarget,
        state_name: str,
    ) -> None:
        tracker.transition_to("i-1", target)
        linear_mock.transition_to_state.assert_called_once_with("i-1", state_name)

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

    def test_repo_link_triggers_when_label_is_none(
        self,
        linear_mock: MagicMock,
        config_no_label: _LinearConfig,
    ) -> None:
        tracker = LinearTracker(linear_mock, config_no_label)
        issue = Issue(
            id="i-1",
            identifier="T-1",
            title="T",
            state="In Progress",
            updatedAt="2025-01-01T00:00:00Z",
            project=Project(
                id="p-1",
                name="Backend",
                links=[ProjectLink(label=" repo ", url="https://github.com/org/repo")],
            ),
        )

        assert tracker.is_still_triggered(issue) is True

    def test_non_repo_link_does_not_trigger_when_label_is_none(
        self,
        linear_mock: MagicMock,
        config_no_label: _LinearConfig,
    ) -> None:
        tracker = LinearTracker(linear_mock, config_no_label)
        issue = Issue(
            id="i-1",
            identifier="T-1",
            title="T",
            state="In Progress",
            labels=["Agent"],
            updatedAt="2025-01-01T00:00:00Z",
            project=Project(
                id="p-1",
                name="Backend",
                links=[ProjectLink(label="Docs", url="https://docs.example.com")],
            ),
        )

        assert tracker.is_still_triggered(issue) is False

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
    @pytest.mark.parametrize(
        "model_labels", [["Model: Strong", "Model: Cheap"], []], ids=["models", "none"]
    )
    def test_calls_provision_trigger_label(
        self,
        tracker: LinearTracker,
        linear_mock: MagicMock,
        tmp_path: Any,
        model_labels: list[str],
    ) -> None:
        from unittest.mock import patch

        state = StateManager(tmp_path / "state.json")
        state.load()

        # provision_trigger_label is imported at module top-level in
        # linear_tracker.py; we patch the reference in that module so the
        # test doesn't require a real Linear connection.
        with (
            patch(
                "symphony_linear.linear_tracker.provision_trigger_label"
            ) as mock_provision,
            patch(
                "symphony_linear.linear_tracker.provision_model_labels"
            ) as mock_models,
        ):
            tracker.ensure_trigger_setup(state, model_labels)
            mock_provision.assert_called_once_with(linear_mock, state, "Agent")
            mock_models.assert_called_once_with(linear_mock, model_labels)

    def test_skips_trigger_label_when_none(
        self,
        linear_mock: MagicMock,
        config_no_label: _LinearConfig,
        tmp_path: Any,
    ) -> None:
        from unittest.mock import patch

        tracker = LinearTracker(linear_mock, config_no_label)
        state = StateManager(tmp_path / "state.json")
        state.load()

        with (
            patch(
                "symphony_linear.linear_tracker.provision_trigger_label"
            ) as mock_provision,
            patch(
                "symphony_linear.linear_tracker.provision_model_labels"
            ) as mock_models,
        ):
            tracker.ensure_trigger_setup(state, ["Model: Strong"])

        mock_provision.assert_not_called()
        mock_models.assert_called_once_with(linear_mock, ["Model: Strong"])


# ---------------------------------------------------------------------------
# _maybe_rewrite_to_ssh
# ---------------------------------------------------------------------------


class TestMaybeRewriteToSsh:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://github.com/org/repo", "git@github.com:org/repo.git"),
            ("https://github.com/org/repo/", "git@github.com:org/repo.git"),
            ("https://github.com/org/repo.git", "https://github.com/org/repo.git"),
            ("git@github.com:org/repo.git", "git@github.com:org/repo.git"),
            ("https://gitlab.com/org/repo", "https://gitlab.com/org/repo"),
            ("http://github.com/org/repo", "http://github.com/org/repo"),
        ],
    )
    def test_rewrites_only_https_github_urls(self, url: str, expected: str) -> None:
        from symphony_linear.linear_tracker import _maybe_rewrite_to_ssh

        assert _maybe_rewrite_to_ssh(url) == expected


# ---------------------------------------------------------------------------
# QA helpers
# ---------------------------------------------------------------------------


class TestIsInQa:
    def _issue(self, state: str) -> Issue:
        return Issue(
            id="i-1",
            identifier="T-1",
            title="T",
            state=state,
            updatedAt="2025-01-01T00:00:00Z",
        )

    @pytest.mark.parametrize(
        ("state", "expected"), [("QA", True), ("In Progress", False)]
    )
    def test_reflects_the_issue_state(
        self, tracker: LinearTracker, state: str, expected: bool
    ) -> None:
        assert tracker.is_in_qa(self._issue(state)) is expected

    def test_false_when_qa_not_configured(self, config_no_qa: _LinearConfig) -> None:
        linear_mock = MagicMock(spec=LinearClient)
        tracker = LinearTracker(linear_mock, config_no_qa)
        assert tracker.is_in_qa(self._issue("QA")) is False


class TestQaEnabled:
    @pytest.mark.parametrize(("qa_state", "expected"), [("QA", True), (None, False)])
    def test_follows_the_configured_qa_state(
        self, linear_mock: MagicMock, qa_state: str | None, expected: bool
    ) -> None:
        config = _LinearConfig(
            api_key="test-key",
            trigger_label="Agent",
            in_progress_state="In Progress",
            needs_input_state="Needs Input",
            qa_state=qa_state,
        )
        assert LinearTracker(linear_mock, config).qa_enabled is expected


class TestTransitionNameFor:
    @pytest.mark.parametrize(
        ("target", "expected"),
        [
            (TransitionTarget.in_progress, "In Progress"),
            (TransitionTarget.needs_input, "Needs Input"),
            (TransitionTarget.qa, "QA"),
        ],
    )
    def test_names(
        self, tracker: LinearTracker, target: TransitionTarget, expected: str
    ) -> None:
        assert tracker.transition_name_for(target) == expected

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

    @pytest.mark.parametrize(
        "error_type", ["NetworkError", "TimeoutException"], ids=["network", "timeout"]
    )
    def test_network_error_raises_attachment_download_error(
        self, tracker: LinearTracker, error_type: str
    ) -> None:
        from unittest.mock import patch

        import httpx

        error = getattr(httpx, error_type)("failed")
        with patch("symphony_linear.linear_tracker.httpx.stream", side_effect=error):
            with pytest.raises(AttachmentDownloadError, match="Download failed"):
                tracker.download_attachment("https://uploads.linear.app/f.txt")

    # ------------------------------------------------------------------
    # SSRF defence — host allowlist validation before any I/O
    # ------------------------------------------------------------------

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
# is_upload_url
# ---------------------------------------------------------------------------


class TestIsUploadUrl:
    """Tests for LinearTracker.is_upload_url."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://uploads.linear.app/abc/file.zip",
            "https://public.linear.app/abc/file.bin",
        ],
    )
    def test_upload_hosts(self, tracker: LinearTracker, url: str) -> None:
        assert tracker.is_upload_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://linear.app/team/issue/ABC-1",
            "https://evil.example.com/file.zip",
            "http://uploads.linear.app/abc/file.zip",
            "https://169.254.169.254/x.png",
            "https://github.com/org/repo/pull/1",
            "",
            "not a url",
        ],
    )
    def test_non_upload_urls(self, tracker: LinearTracker, url: str) -> None:
        assert tracker.is_upload_url(url) is False


# ---------------------------------------------------------------------------
# _LinearAuth (host-based auth header restriction)
# ---------------------------------------------------------------------------


class TestLinearAuth:
    """Tests for the _LinearAuth custom httpx.Auth class."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://uploads.linear.app/some-file.png",
            "https://public.linear.app/image.png",
        ],
    )
    def test_allowlisted_host_gets_auth_header(self, url: str) -> None:
        import httpx

        auth = _LinearAuth("my-api-key")
        req = httpx.Request("GET", url)
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
