"""Tests for the GitHubTracker adapter.

Verifies that GitHubTracker implements the Tracker protocol and correctly
delegates to the underlying GitHubClient with the right GraphQL queries.
Uses ``unittest.mock.MagicMock`` — no real network calls.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from symphony_linear.github import (
    GitHubClient,
    GitHubError,
    GitHubNotFoundError,
)
from symphony_linear.github_tracker import (
    GitHubTracker,
    GitHubTrackerConfig,
    _GitHubAuth,
    _parse_project_ref,
)
from symphony_linear.linear import Issue
from symphony_linear.state import StateManager
from symphony_linear.tracker import (
    AttachmentDownloadError,
    AttachmentTooLargeError,
    Tracker,
    TrackerError,
    TrackerNotFoundError,
    TransitionTarget,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config() -> GitHubTrackerConfig:
    return GitHubTrackerConfig(
        token="ghp_test",
        project_ref="orgs/my-org/projects/1",
        in_progress_status="In Progress",
        needs_input_status="Needs Input",
        qa_status="QA",
        trigger_field="Symphony",
        status_field="Status",
    )


@pytest.fixture
def config_no_qa() -> GitHubTrackerConfig:
    return GitHubTrackerConfig(
        token="ghp_test",
        project_ref="orgs/my-org/projects/1",
        in_progress_status="In Progress",
        needs_input_status="Needs Input",
        qa_status=None,
        trigger_field="Symphony",
        status_field="Status",
    )


@pytest.fixture
def client_mock() -> MagicMock:
    return MagicMock(spec=GitHubClient)


@pytest.fixture
def tracker(client_mock: MagicMock, config: GitHubTrackerConfig) -> GitHubTracker:
    t = GitHubTracker(client=client_mock, config=config)
    # Pre-populate resolved state so tests don't need to call resolve().
    t._project_node_id = "PVT_project1"
    t._status_field_id = "PVTSSF_status"
    t._status_option_ids = {
        "In Progress": "opt_ip",
        "Needs Input": "opt_ni",
        "QA": "opt_qa",
        "Done": "opt_done",
    }
    t._trigger_field_id = "PVTSSF_symphony"
    t._trigger_option_id = "opt_on"
    return t


def _resolved_tracker(
    client_mock: MagicMock, config: GitHubTrackerConfig
) -> GitHubTracker:
    """Return a tracker with pre-populated resolved state."""
    t = GitHubTracker(client=client_mock, config=config)
    t._project_node_id = "PVT_project1"
    t._status_field_id = "PVTSSF_status"
    t._status_option_ids = {
        "In Progress": "opt_ip",
        "Needs Input": "opt_ni",
        "QA": "opt_qa",
        "Done": "opt_done",
    }
    t._trigger_field_id = "PVTSSF_symphony"
    t._trigger_option_id = "opt_on"
    return t


# ---------------------------------------------------------------------------
# Helpers – build canned responses
# ---------------------------------------------------------------------------


def _issue_item(
    *,
    item_id: str = "PVTI_1",
    issue_id: str = "I_1",
    number: int = 42,
    title: str = "Test issue",
    state: str = "OPEN",
    status_name: str = "In Progress",
    trigger_on: bool = True,
    repo_name: str = "my-org/my-repo",
    repo_ssh_url: str = "git@github.com:my-org/my-repo.git",
    repo_https_url: str = "https://github.com/my-org/my-repo",
    updated_at: str = "2025-01-01T00:00:00Z",
    labels: list[str] | None = None,
) -> dict[str, Any]:
    """Build a project-item node dict.

    ``labels=None`` (the default) omits the ``labels`` key entirely so
    tests can exercise the tolerant missing-key extraction path.
    """

    field_values: list[dict[str, Any]] = []
    if trigger_on:
        field_values.append(
            {
                "field": {"name": "Symphony"},
                "name": "on",
                "optionId": "opt_on",
            }
        )
    else:
        field_values.append(
            {
                "field": {"name": "Symphony"},
                "name": None,
                "optionId": None,
            }
        )
    field_values.append(
        {
            "field": {"name": "Status"},
            "name": status_name,
            "optionId": f"opt_{status_name.lower().replace(' ', '_')}",
        }
    )

    content: dict[str, Any] = {
        "__typename": "Issue",
        "id": issue_id,
        "number": number,
        "title": title,
        "state": state,
        "updatedAt": updated_at,
        "repository": {
            "sshUrl": repo_ssh_url,
            "url": repo_https_url,
            "nameWithOwner": repo_name,
        },
    }
    if labels is not None:
        content["labels"] = {"nodes": [{"name": name} for name in labels]}

    return {
        "id": item_id,
        "content": content,
        "fieldValues": {"nodes": field_values},
    }


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_is_tracker_instance(
        self, client_mock: MagicMock, config: GitHubTrackerConfig
    ) -> None:
        assert isinstance(GitHubTracker(client_mock, config), Tracker)


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class TestExceptionHierarchy:
    def test_tracker_error_catches_github_error(self) -> None:
        with pytest.raises(TrackerError):
            raise GitHubError("test")

    def test_tracker_not_found_catches_github_not_found(self) -> None:
        with pytest.raises(TrackerNotFoundError):
            raise GitHubNotFoundError("test")


# ---------------------------------------------------------------------------
# Project ref parsing
# ---------------------------------------------------------------------------


class TestParseProjectRef:
    @pytest.mark.parametrize(
        ("ref", "expected"),
        [
            ("orgs/my-org/projects/5", ("orgs", "my-org", 5)),
            ("users/alice/projects/10", ("users", "alice", 10)),
        ],
    )
    def test_parses_owner_and_number(
        self, ref: str, expected: tuple[str, str, int]
    ) -> None:
        assert _parse_project_ref(ref) == expected

    @pytest.mark.parametrize(
        "ref",
        ["garbage", "orgs/my-org/projects", "teams/my-org/projects/1"],
    )
    def test_invalid_ref_raises(self, ref: str) -> None:
        with pytest.raises(ValueError, match="Invalid project ref"):
            _parse_project_ref(ref)


# ---------------------------------------------------------------------------
# resolve() — project resolution
# ---------------------------------------------------------------------------


class TestResolveProject:
    @pytest.mark.parametrize(
        ("ref", "response_key", "query_fragment"),
        [
            ("orgs/my-org/projects/1", "organization", "organization(login:"),
            ("users/alice/projects/2", "user", "user(login:"),
        ],
    )
    def test_resolves_project(
        self,
        client_mock: MagicMock,
        config: GitHubTrackerConfig,
        ref: str,
        response_key: str,
        query_fragment: str,
    ) -> None:
        config.project_ref = ref
        tracker = GitHubTracker(client_mock, config)
        client_mock._query.return_value = {
            response_key: {
                "projectV2": {"id": "PVT_abc", "title": "My Project"},
            }
        }

        tracker._resolve_project()

        assert tracker._project_node_id == "PVT_abc"
        call_args = client_mock._query.call_args
        assert query_fragment in call_args[0][0]

    def test_missing_project_raises(
        self, client_mock: MagicMock, config: GitHubTrackerConfig
    ) -> None:
        tracker = GitHubTracker(client_mock, config)
        client_mock._query.return_value = {"organization": {"projectV2": None}}
        with pytest.raises(GitHubNotFoundError, match="Project not found"):
            tracker._resolve_project()


# ---------------------------------------------------------------------------
# Status field resolution
# ---------------------------------------------------------------------------


class TestResolveStatusField:
    def test_finds_status_field_and_options(
        self, client_mock: MagicMock, config: GitHubTrackerConfig
    ) -> None:
        tracker = GitHubTracker(client_mock, config)
        tracker._project_node_id = "PVT_p1"
        client_mock._query.return_value = {
            "node": {
                "fields": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {
                            "id": "PVTSSF_status",
                            "name": "Status",
                            "options": [
                                {"id": "opt_ip", "name": "In Progress"},
                                {"id": "opt_ni", "name": "Needs Input"},
                                {"id": "opt_qa", "name": "QA"},
                                {"id": "opt_done", "name": "Done"},
                            ],
                        },
                        {
                            "id": "PVTSSF_other",
                            "name": "Priority",
                            "options": [{"id": "opt_high", "name": "High"}],
                        },
                    ],
                }
            }
        }
        tracker._resolve_status_field()
        assert tracker._status_field_id == "PVTSSF_status"
        assert tracker._status_option_ids["In Progress"] == "opt_ip"
        assert tracker._status_option_ids["Needs Input"] == "opt_ni"
        assert tracker._status_option_ids["QA"] == "opt_qa"

    def test_missing_status_field_raises(
        self, client_mock: MagicMock, config: GitHubTrackerConfig
    ) -> None:
        tracker = GitHubTracker(client_mock, config)
        tracker._project_node_id = "PVT_p1"
        client_mock._query.return_value = {
            "node": {
                "fields": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        {
                            "id": "PVTSSF_other",
                            "name": "Priority",
                            "options": [],
                        },
                    ],
                }
            }
        }
        with pytest.raises(ValueError, match="Status field"):
            tracker._resolve_status_field()

    def test_auto_creates_missing_status_options(
        self, client_mock: MagicMock, config: GitHubTrackerConfig
    ) -> None:
        tracker = GitHubTracker(client_mock, config)
        tracker._project_node_id = "PVT_p1"

        # The Status field has "In Progress" and an unrelated "Done";
        # "Needs Input" and "QA" are missing and should be auto-created.
        existing_options = [
            {
                "id": "opt_ip",
                "name": "In Progress",
                "color": "YELLOW",
                "description": "Work is underway",
            },
            {
                "id": "opt_done",
                "name": "Done",
                "color": "GREEN",
                "description": "Completed work",
            },
        ]

        mutation_variables: dict[str, Any] | None = None

        def query_side_effect(
            query_text: str, variables: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            nonlocal mutation_variables
            if "fields(" in query_text:
                return {
                    "node": {
                        "fields": {
                            "pageInfo": {"hasNextPage": False},
                            "nodes": [
                                {
                                    "id": "PVTSSF_status",
                                    "name": "Status",
                                    "options": existing_options,
                                },
                            ],
                        },
                    },
                }
            # Mutation — capture variables and return updated options.
            mutation_variables = variables
            return {
                "updateProjectV2Field": {
                    "projectV2Field": {
                        "options": [
                            {
                                "id": "opt_ip",
                                "name": "In Progress",
                                "color": "YELLOW",
                                "description": "Work is underway",
                            },
                            {
                                "id": "opt_done",
                                "name": "Done",
                                "color": "GREEN",
                                "description": "Completed work",
                            },
                            {
                                "id": "opt_ni_new",
                                "name": "Needs Input",
                                "color": "ORANGE",
                                "description": "",
                            },
                            {
                                "id": "opt_qa_new",
                                "name": "QA",
                                "color": "PURPLE",
                                "description": "",
                            },
                        ],
                    },
                },
            }

        client_mock._query.side_effect = query_side_effect

        tracker._resolve_status_field()
        assert tracker._status_field_id == "PVTSSF_status"
        assert tracker._status_option_ids == {
            "In Progress": "opt_ip",
            "Done": "opt_done",
            "Needs Input": "opt_ni_new",
            "QA": "opt_qa_new",
        }

        # Verify the mutation payload preserves existing options and adds
        # new ones with the correct colors and no id.
        assert mutation_variables is not None
        input_data = mutation_variables["input"]
        assert input_data["fieldId"] == "PVTSSF_status"

        select_options: list[dict[str, Any]] = input_data["singleSelectOptions"]

        # Existing options must appear unchanged.
        existing_by_name = {o["name"]: o for o in select_options if "id" in o}
        assert existing_by_name == {
            "In Progress": {
                "id": "opt_ip",
                "name": "In Progress",
                "color": "YELLOW",
                "description": "Work is underway",
            },
            "Done": {
                "id": "opt_done",
                "name": "Done",
                "color": "GREEN",
                "description": "Completed work",
            },
        }

        # New options must omit id and use the correct hardcoded colors.
        new_by_name = {o["name"]: o for o in select_options if "id" not in o}
        assert new_by_name == {
            "Needs Input": {
                "name": "Needs Input",
                "color": "ORANGE",
                "description": "",
            },
            "QA": {
                "name": "QA",
                "color": "PURPLE",
                "description": "",
            },
        }

        # Sanity: total count.
        assert len(select_options) == 4

    def test_qa_option_not_required_when_unset(
        self, client_mock: MagicMock, config_no_qa: GitHubTrackerConfig
    ) -> None:
        tracker = GitHubTracker(client_mock, config_no_qa)
        tracker._project_node_id = "PVT_p1"
        client_mock._query.return_value = {
            "node": {
                "fields": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        {
                            "id": "PVTSSF_status",
                            "name": "Status",
                            "options": [
                                {"id": "opt_ip", "name": "In Progress"},
                                {"id": "opt_ni", "name": "Needs Input"},
                            ],
                        },
                    ],
                }
            }
        }
        # Should not raise — QA is not required.
        tracker._resolve_status_field()

    def test_paginates_fields(
        self, client_mock: MagicMock, config: GitHubTrackerConfig
    ) -> None:
        tracker = GitHubTracker(client_mock, config)
        tracker._project_node_id = "PVT_p1"

        # Two pages of fields.
        def side_effect(
            query: str, variables: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            cursor = (variables or {}).get("cursor")
            if cursor is None:
                return {
                    "node": {
                        "fields": {
                            "pageInfo": {"hasNextPage": True, "endCursor": "c2"},
                            "nodes": [
                                {
                                    "id": "f1",
                                    "name": "Priority",
                                    "options": [],
                                },
                            ],
                        }
                    }
                }
            else:
                return {
                    "node": {
                        "fields": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {
                                    "id": "PVTSSF_status",
                                    "name": "Status",
                                    "options": [
                                        {"id": "opt_ip", "name": "In Progress"},
                                        {"id": "opt_ni", "name": "Needs Input"},
                                        {"id": "opt_qa", "name": "QA"},
                                    ],
                                },
                            ],
                        }
                    }
                }

        client_mock._query.side_effect = side_effect
        tracker._resolve_status_field()
        assert tracker._status_field_id == "PVTSSF_status"
        # Two calls: one for each page.
        assert client_mock._query.call_count == 2


# ---------------------------------------------------------------------------
# Trigger field resolution / ensure_trigger_setup
# ---------------------------------------------------------------------------


class TestTriggerField:
    def test_reuses_existing_field(
        self, client_mock: MagicMock, config: GitHubTrackerConfig
    ) -> None:
        tracker = GitHubTracker(client_mock, config)
        tracker._project_node_id = "PVT_p1"
        client_mock._query.return_value = {
            "node": {
                "fields": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        {"id": "PVTSSF_status", "name": "Status", "options": []},
                        {
                            "id": "PVTSSF_sym",
                            "name": "Symphony",
                            "options": [{"id": "opt_on", "name": "on"}],
                        },
                    ],
                }
            }
        }
        tracker._resolve_trigger_field()
        assert tracker._trigger_field_id == "PVTSSF_sym"
        assert tracker._trigger_option_id == "opt_on"
        # Should NOT have created a new field.
        assert client_mock._query.call_count == 1

    def test_creates_missing_field(
        self, client_mock: MagicMock, config: GitHubTrackerConfig
    ) -> None:
        tracker = GitHubTracker(client_mock, config)
        tracker._project_node_id = "PVT_p1"

        # First call: list fields (trigger field missing).
        # Second call: create field mutation.
        call_count = [0]

        def side_effect(
            query: str, variables: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            call_count[0] += 1
            if "createProjectV2Field" in query:
                return {
                    "createProjectV2Field": {
                        "projectV2Field": {
                            "id": "PVTSSF_new",
                            "options": [{"id": "opt_on_new", "name": "on"}],
                        }
                    }
                }
            return {
                "node": {
                    "fields": {
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [
                            {"id": "PVTSSF_status", "name": "Status", "options": []},
                        ],
                    }
                }
            }

        client_mock._query.side_effect = side_effect
        tracker._resolve_trigger_field()
        assert tracker._trigger_field_id == "PVTSSF_new"
        assert tracker._trigger_option_id == "opt_on_new"
        assert call_count[0] == 2  # list + create

    def test_existing_field_no_options_raises(
        self, client_mock: MagicMock, config: GitHubTrackerConfig
    ) -> None:
        tracker = GitHubTracker(client_mock, config)
        tracker._project_node_id = "PVT_p1"
        client_mock._query.return_value = {
            "node": {
                "fields": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        {"id": "PVTSSF_sym", "name": "Symphony", "options": []},
                    ],
                }
            }
        }
        with pytest.raises(GitHubNotFoundError, match="has no options"):
            tracker._resolve_trigger_field()

    def test_ensure_trigger_setup_calls_resolve(
        self, client_mock: MagicMock, config: GitHubTrackerConfig, tmp_path: Any
    ) -> None:
        tracker = GitHubTracker(client_mock, config)
        # Simulate a full resolve.
        # First call: resolve project.
        # Second call: list fields (status + trigger).

        call_count = [0]

        def side_effect(
            query: str, variables: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            call_count[0] += 1
            if "organization" in query:
                return {
                    "organization": {
                        "projectV2": {"id": "PVT_p1", "title": "My Project"},
                    }
                }
            return {
                "node": {
                    "fields": {
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [
                            {
                                "id": "PVTSSF_status",
                                "name": "Status",
                                "options": [
                                    {"id": "opt_ip", "name": "In Progress"},
                                    {"id": "opt_ni", "name": "Needs Input"},
                                    {"id": "opt_qa", "name": "QA"},
                                ],
                            },
                            {
                                "id": "PVTSSF_sym",
                                "name": "Symphony",
                                "options": [{"id": "opt_on", "name": "on"}],
                            },
                        ],
                    }
                }
            }

        client_mock._query.side_effect = side_effect

        state = StateManager(tmp_path / "state.json")
        state.load()

        # Model labels are passed but ignored: GitHub labels are
        # per-repository and one project spans many repos.
        tracker.ensure_trigger_setup(state, ["Model: Strong", "Model: Cheap"])

        assert tracker._project_node_id == "PVT_p1"
        assert tracker._trigger_field_id == "PVTSSF_sym"
        assert tracker._trigger_option_id == "opt_on"

    def test_ensure_trigger_setup_ignores_model_labels_when_resolved(
        self, client_mock: MagicMock, config: GitHubTrackerConfig, tmp_path: Any
    ) -> None:
        tracker = GitHubTracker(client_mock, config)
        tracker._project_node_id = "PVT_p1"

        state = StateManager(tmp_path / "state.json")
        state.load()

        tracker.ensure_trigger_setup(state, ["Model: Strong"])

        # No queries at all: model labels are ignored and the project is
        # already resolved.
        client_mock._query.assert_not_called()


# ---------------------------------------------------------------------------
# list_triggered_issues
# ---------------------------------------------------------------------------


class TestListTriggeredIssues:
    def test_basic_listing(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        client_mock._query.return_value = {
            "node": {
                "items": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        _issue_item(
                            item_id="PVTI_1",
                            issue_id="I_1",
                            number=42,
                            title="Fix bug",
                            status_name="In Progress",
                        ),
                    ],
                }
            }
        }
        issues = tracker.list_triggered_issues()
        assert len(issues) == 1
        assert issues[0].id == "I_1"
        assert issues[0].identifier == "my-org-my-repo-42-bcc89b7f"
        assert issues[0].title == "Fix bug"
        assert issues[0].state == "In Progress"
        # No labels key in the payload — extraction must tolerate it.
        assert issues[0].labels == []
        assert issues[0].tracker_data == {
            "clone_url": "git@github.com:my-org/my-repo.git",
            "project_item_id": "PVTI_1",
        }

        # The project-items query must select the labels connection.
        call = client_mock._query.call_args
        assert call is not None
        assert "labels(first: 50)" in call[0][0]

    @pytest.mark.parametrize(
        ("labels", "expected"),
        [
            ([], []),
            (["Model: strong", "bug"], ["Model: strong", "bug"]),
        ],
        ids=["empty", "values"],
    )
    def test_surfaces_labels(
        self,
        tracker: GitHubTracker,
        client_mock: MagicMock,
        labels: list[str],
        expected: list[str],
    ) -> None:
        item = _issue_item(issue_id="I_1", labels=labels)
        client_mock._query.return_value = {
            "node": {
                "items": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [item],
                }
            }
        }

        issues = tracker.list_triggered_issues()

        assert len(issues) == 1
        assert issues[0].labels == expected

    def test_tolerates_null_labels(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        # The labels connection is nullable — an explicit null (not just a
        # missing key) must be tolerated.
        item = _issue_item(issue_id="I_1")
        item["content"]["labels"] = None
        client_mock._query.return_value = {
            "node": {
                "items": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [item],
                }
            }
        }

        issues = tracker.list_triggered_issues()

        assert len(issues) == 1
        assert issues[0].labels == []

    @pytest.mark.parametrize(
        "item",
        [
            {"state": "CLOSED"},
            {"trigger_on": False},
            {"status_name": "Done"},
        ],
        ids=["closed", "not-triggered", "inactive-status"],
    )
    def test_filters_items_that_are_not_triggered(
        self, tracker: GitHubTracker, client_mock: MagicMock, item: dict[str, Any]
    ) -> None:
        client_mock._query.return_value = {
            "node": {
                "items": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        _issue_item(issue_id="I_1", **item),
                        _issue_item(issue_id="I_2"),
                    ],
                }
            }
        }
        issues = tracker.list_triggered_issues()
        assert len(issues) == 1
        assert issues[0].id == "I_2"

    def test_skips_non_issue_content(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        item = _issue_item(item_id="PVTI_pr", issue_id="PR_1")
        item["content"]["__typename"] = "PullRequest"
        client_mock._query.return_value = {
            "node": {
                "items": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        item,
                        _issue_item(item_id="PVTI_iss", issue_id="I_1"),
                    ],
                }
            }
        }
        issues = tracker.list_triggered_issues()
        assert len(issues) == 1
        assert issues[0].id == "I_1"

    def test_paginates(self, tracker: GitHubTracker, client_mock: MagicMock) -> None:
        call_count = [0]

        def side_effect(
            query: str, variables: dict[str, Any] | None = None, **kwargs: Any
        ) -> dict[str, Any]:
            call_count[0] += 1
            cursor = (variables or {}).get("cursor")
            if cursor is None:
                return {
                    "node": {
                        "items": {
                            "pageInfo": {"hasNextPage": True, "endCursor": "c2"},
                            "nodes": [
                                _issue_item(item_id="PVTI_1", issue_id="I_1"),
                            ],
                        }
                    }
                }
            else:
                return {
                    "node": {
                        "items": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                _issue_item(item_id="PVTI_2", issue_id="I_2"),
                            ],
                        }
                    }
                }

        client_mock._query.side_effect = side_effect
        issues = tracker.list_triggered_issues()
        assert len(issues) == 2
        assert call_count[0] == 2

    def test_handles_missing_repository(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        item = _issue_item(item_id="PVTI_1", issue_id="I_1")
        item["content"]["repository"] = None
        client_mock._query.return_value = {
            "node": {
                "items": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [item],
                }
            }
        }
        issues = tracker.list_triggered_issues()
        assert len(issues) == 1
        assert issues[0].identifier == "42"  # no nameWithOwner
        assert issues[0].tracker_data is not None
        assert issues[0].tracker_data["clone_url"] is None

    def test_colliding_flattened_repo_names_get_distinct_identifiers(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        """``foo-bar/baz`` and ``foo/bar-baz`` both flatten to ``foo-bar-baz``;
        the identifier must disambiguate them or the two issues would share a
        workspace directory."""
        client_mock._query.return_value = {
            "node": {
                "items": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        _issue_item(
                            item_id="PVTI_1",
                            issue_id="I_1",
                            number=1,
                            repo_name="foo-bar/baz",
                        ),
                        _issue_item(
                            item_id="PVTI_2",
                            issue_id="I_2",
                            number=1,
                            repo_name="foo/bar-baz",
                        ),
                    ],
                }
            }
        }
        issues = tracker.list_triggered_issues()

        identifiers = [issue.identifier for issue in issues]
        assert len(set(identifiers)) == 2
        # The readable flattened form is preserved as a prefix.
        assert identifiers[0].startswith("foo-bar-baz-1")
        assert identifiers[1].startswith("foo-bar-baz-1")

    @pytest.mark.parametrize("qa_configured", [True, False])
    def test_qa_is_listed_only_when_configured(
        self,
        client_mock: MagicMock,
        config: GitHubTrackerConfig,
        config_no_qa: GitHubTrackerConfig,
        qa_configured: bool,
    ) -> None:
        tracker = _resolved_tracker(
            client_mock, config if qa_configured else config_no_qa
        )
        client_mock._query.return_value = {
            "node": {
                "items": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        _issue_item(issue_id="I_1", status_name="QA"),
                    ],
                }
            }
        }
        issues = tracker.list_triggered_issues()
        assert len(issues) == (1 if qa_configured else 0)

    def test_populates_item_id_map(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        client_mock._query.return_value = {
            "node": {
                "items": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        _issue_item(item_id="PVTI_x", issue_id="I_x"),
                        _issue_item(item_id="PVTI_y", issue_id="I_y"),
                    ],
                }
            }
        }
        tracker.list_triggered_issues()
        with tracker._item_map_lock:
            assert tracker._item_id_map == {"I_x": "PVTI_x", "I_y": "PVTI_y"}

    def test_raises_when_project_not_resolved(
        self, client_mock: MagicMock, config: GitHubTrackerConfig
    ) -> None:
        tracker = GitHubTracker(client_mock, config)
        with pytest.raises(RuntimeError, match="Project not resolved"):
            tracker.list_triggered_issues()


# ---------------------------------------------------------------------------
# get_issue
# ---------------------------------------------------------------------------


class TestGetIssue:
    def test_fetches_issue_with_status(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        # First call: fetch issue data (no comments, will trigger paginated fetch).
        # Second call: paginate comments.
        # Third call: query item for status (item id is in map).
        call_count = [0]

        def side_effect(
            query: str, variables: dict[str, Any] | None = None, **kwargs: Any
        ) -> dict[str, Any]:
            call_count[0] += 1
            if "comments" in query and "fieldValues" not in query:
                # Paginated comment query.
                return {
                    "node": {
                        "comments": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [],
                        }
                    }
                }
            if "fieldValues" in query:
                # Item field values query.
                return {
                    "node": {
                        "fieldValues": {
                            "nodes": [
                                {
                                    "field": {"name": "Symphony"},
                                    "name": "on",
                                    "optionId": "opt_on",
                                },
                                {"field": {"name": "Status"}, "name": "In Progress"},
                            ],
                        }
                    }
                }
            # _fetch_issue_raw query.
            return {
                "node": {
                    "__typename": "Issue",
                    "id": "I_1",
                    "number": 42,
                    "title": "Test",
                    "body": "Description text",
                    "state": "OPEN",
                    "updatedAt": "2025-01-01T00:00:00Z",
                    "labels": {"nodes": [{"name": "Model: strong"}]},
                    "repository": {
                        "sshUrl": "git@github.com:my-org/my-repo.git",
                        "url": "https://github.com/my-org/my-repo",
                        "nameWithOwner": "my-org/my-repo",
                    },
                }
            }

        client_mock._query.side_effect = side_effect
        tracker._item_id_map["I_1"] = "PVTI_1"

        issue = tracker.get_issue("I_1")
        assert issue.id == "I_1"
        assert issue.identifier == "my-org-my-repo-42-bcc89b7f"
        assert issue.description == "Description text"
        assert issue.state == "In Progress"
        assert issue.labels == ["Model: strong"]
        assert issue.tracker_data is not None
        assert issue.tracker_data["clone_url"] == "git@github.com:my-org/my-repo.git"

    def test_tolerates_null_labels(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        # The labels connection is nullable — an explicit null on the raw
        # issue payload must be tolerated like a missing key.
        def side_effect(
            query: str, variables: dict[str, Any] | None = None, **kwargs: Any
        ) -> dict[str, Any]:
            if "comments" in query and "fieldValues" not in query:
                # Paginated comment query.
                return {
                    "node": {
                        "comments": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [],
                        }
                    }
                }
            if "fieldValues" in query:
                # Item field values query.
                return {
                    "node": {
                        "fieldValues": {
                            "nodes": [
                                {"field": {"name": "Status"}, "name": "In Progress"},
                            ],
                        }
                    }
                }
            # _fetch_issue_raw query.
            return {
                "node": {
                    "__typename": "Issue",
                    "id": "I_1",
                    "number": 42,
                    "title": "Test",
                    "body": None,
                    "state": "OPEN",
                    "updatedAt": "2025-01-01T00:00:00Z",
                    "labels": None,
                    "repository": {
                        "sshUrl": "git@github.com:my-org/my-repo.git",
                        "url": "https://github.com/my-org/my-repo",
                        "nameWithOwner": "my-org/my-repo",
                    },
                }
            }

        client_mock._query.side_effect = side_effect
        tracker._item_id_map["I_1"] = "PVTI_1"

        issue = tracker.get_issue("I_1")
        assert issue.labels == []

    def test_not_found_raises(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        client_mock._query.return_value = {"node": None}
        with pytest.raises(GitHubNotFoundError, match="Issue not found"):
            tracker.get_issue("bad-id")

    def test_refreshes_item_map_when_missing(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        call_count = [0]

        def side_effect(
            query: str, variables: dict[str, Any] | None = None, **kwargs: Any
        ) -> dict[str, Any]:
            call_count[0] += 1

            # _fetch_issue_raw call (no comments, no items, no fieldValues).
            if (
                "comments" not in query
                and "items" not in query
                and "fieldValues" not in query
            ):
                return {
                    "node": {
                        "__typename": "Issue",
                        "id": "I_1",
                        "number": 42,
                        "title": "Test",
                        "body": None,
                        "state": "OPEN",
                        "updatedAt": "2025-01-01T00:00:00Z",
                        "repository": {
                            "sshUrl": "ssh://r",
                            "url": "https://example.com/r",
                            "nameWithOwner": "o/r",
                        },
                    }
                }

            # Comment pagination query.
            if "comments" in query:
                return {
                    "node": {
                        "comments": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [],
                        }
                    }
                }

            # _refresh_item_map call (project items query).
            if "items" in query:
                return {
                    "node": {
                        "items": {
                            "pageInfo": {"hasNextPage": False},
                            "nodes": [
                                _issue_item(item_id="PVTI_1", issue_id="I_1"),
                            ],
                        }
                    }
                }

            # _query_item_field_values for the status.
            return {
                "node": {
                    "fieldValues": {
                        "nodes": [
                            {"field": {"name": "Status"}, "name": "In Progress"},
                        ],
                    }
                }
            }

        client_mock._query.side_effect = side_effect
        # Map is empty — triggers refresh.
        tracker._item_id_map.clear()

        issue = tracker.get_issue("I_1")
        assert issue.state == "In Progress"
        # No labels key in the raw payload — extraction must tolerate it.
        assert issue.labels == []
        with tracker._item_map_lock:
            assert tracker._item_id_map == {"I_1": "PVTI_1"}

    def test_handles_missing_item_in_project(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        # Issue exists but is not in the project.
        call_count = [0]

        def side_effect(
            query: str, variables: dict[str, Any] | None = None, **kwargs: Any
        ) -> dict[str, Any]:
            call_count[0] += 1
            if (
                "comments" not in query
                and "items" not in query
                and "fieldValues" not in query
            ):
                return {
                    "node": {
                        "__typename": "Issue",
                        "id": "I_1",
                        "number": 42,
                        "title": "Test",
                        "body": "text",
                        "state": "OPEN",
                        "updatedAt": "2025-01-01T00:00:00Z",
                        "repository": {
                            "sshUrl": "ssh://r",
                            "url": "https://example.com/r",
                            "nameWithOwner": "o/r",
                        },
                    }
                }
            if "comments" in query:
                return {
                    "node": {
                        "comments": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [],
                        }
                    }
                }
            # _refresh_item_map returns empty.
            return {
                "node": {
                    "items": {
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [],
                    }
                }
            }

        client_mock._query.side_effect = side_effect
        tracker._item_id_map.clear()

        issue = tracker.get_issue("I_1")
        # state is empty because the item wasn't found.
        assert issue.state == ""


# ---------------------------------------------------------------------------
# list_comments_since
# ---------------------------------------------------------------------------


class TestListCommentsSince:
    def _comment_node(
        self, id_: str, body: str, user_id: str | None = None
    ) -> dict[str, Any]:
        c: dict[str, Any] = {
            "id": id_,
            "body": body,
            "createdAt": f"2025-01-0{id_[-1]}T00:00:00Z",
        }
        if user_id:
            c["author"] = {"id": user_id}
        return c

    @pytest.mark.parametrize(
        ("last_seen", "expected_ids"),
        [
            (None, ["c1", "c2", "c3"]),
            ("c2", ["c3"]),
            ("c3", []),
            ("deleted-id", []),
        ],
        ids=["all", "after", "last-matches", "not-found"],
    )
    def test_returns_comments_after_last_seen(
        self,
        tracker: GitHubTracker,
        client_mock: MagicMock,
        last_seen: str | None,
        expected_ids: list[str],
    ) -> None:
        nodes = [
            self._comment_node("c1", "First", "user1"),
            self._comment_node("c2", "Second"),
            self._comment_node("c3", "Third"),
        ]
        # _fetch_issue_raw returns without comments; _comments_from_raw fetches.
        issue_raw = {
            "node": {
                "__typename": "Issue",
                "id": "I_1",
                "number": 1,
                "title": "T",
                "body": "B",
                "state": "OPEN",
                "updatedAt": "2025-01-01T00:00:00Z",
                "repository": None,
            }
        }
        comment_page = {
            "node": {
                "comments": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": nodes,
                }
            }
        }

        def side_effect(
            query: str, variables: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            if "comments" in query:
                return comment_page
            return issue_raw

        client_mock._query.side_effect = side_effect

        comments = tracker.list_comments_since("I_1", last_seen)

        assert [c.id for c in comments] == expected_ids

    def test_issue_not_found_raises(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        client_mock._query.return_value = {"node": None}
        with pytest.raises(GitHubNotFoundError):
            tracker.list_comments_since("bad-id", None)


# ---------------------------------------------------------------------------
# post_comment / edit_comment
# ---------------------------------------------------------------------------


class TestPostComment:
    def test_creates_and_returns_comment(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        client_mock._query.return_value = {
            "addComment": {
                "commentEdge": {
                    "node": {
                        "id": "c_new",
                        "body": "hello world\n\n*Symphony · test*",
                        "createdAt": "2025-01-01T00:00:00Z",
                        "author": {"id": "U_bot123"},
                    }
                }
            }
        }
        comment = tracker.post_comment("I_1", "hello world", "test")
        assert comment.id == "c_new"
        assert comment.body == "hello world\n\n*Symphony · test*"
        assert comment.user_id == "U_bot123"

        # Verify the daemon actually submitted the body with the footer.
        call_args = client_mock._query.call_args
        assert call_args is not None
        # call_args is ((query_str, variables_dict),) or ((query_str,), {'variables': ...})
        # GitHub tracker passes variables as a dict in the second positional arg.
        variables = call_args[0][1]
        assert variables["input"]["body"] == "hello world\n\n*Symphony · test*"

    def test_no_comment_raises(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        client_mock._query.return_value = {
            "addComment": {"commentEdge": {"node": None}}
        }
        with pytest.raises(GitHubError, match="no comment"):
            tracker.post_comment("I_1", "x", "test")


class TestEditComment:
    def test_updates_successfully(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        client_mock._query.return_value = {"updateIssueComment": {}}
        # Should not raise.
        tracker.edit_comment("c_1", "updated body", "test")

        # Verify the daemon submitted the body with the footer.
        call_args = client_mock._query.call_args
        assert call_args is not None
        variables = call_args[0][1]
        assert variables["input"]["body"] == "updated body\n\n*Symphony · test*"


# ---------------------------------------------------------------------------
# transition_to
# ---------------------------------------------------------------------------


class TestTransitionTo:
    @pytest.mark.parametrize(
        ("target", "option_id"),
        [
            (TransitionTarget.in_progress, "opt_ip"),
            (TransitionTarget.needs_input, "opt_ni"),
            (TransitionTarget.qa, "opt_qa"),
        ],
    )
    def test_maps_target_to_the_status_option(
        self,
        tracker: GitHubTracker,
        client_mock: MagicMock,
        target: TransitionTarget,
        option_id: str,
    ) -> None:
        tracker._item_id_map["I_1"] = "PVTI_1"
        client_mock._query.return_value = {"updateProjectV2ItemFieldValue": {}}

        tracker.transition_to("I_1", target)

        call_args = client_mock._query.call_args
        variables = call_args[0][1]["input"]
        assert variables["fieldId"] == "PVTSSF_status"
        assert variables["itemId"] == "PVTI_1"
        assert variables["value"]["singleSelectOptionId"] == option_id

    def test_qa_raises_when_not_configured(
        self, client_mock: MagicMock, config_no_qa: GitHubTrackerConfig
    ) -> None:
        tracker = _resolved_tracker(client_mock, config_no_qa)
        with pytest.raises(ValueError, match="no qa_status"):
            tracker.transition_to("I_1", TransitionTarget.qa)

    def test_refreshes_item_map_when_missing(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        tracker._item_id_map.clear()

        call_count = [0]

        def side_effect(
            query: str, variables: dict[str, Any] | None = None, **kwargs: Any
        ) -> dict[str, Any]:
            call_count[0] += 1
            if "items" in query:
                return {
                    "node": {
                        "items": {
                            "pageInfo": {"hasNextPage": False},
                            "nodes": [
                                _issue_item(item_id="PVTI_found", issue_id="I_1"),
                            ],
                        }
                    }
                }
            return {"updateProjectV2ItemFieldValue": {}}

        client_mock._query.side_effect = side_effect
        tracker.transition_to("I_1", TransitionTarget.in_progress)

        # Should have made two calls: refresh + mutation.
        assert call_count[0] == 2
        assert tracker._item_id_map == {"I_1": "PVTI_found"}

    def test_missing_item_after_refresh_raises(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        tracker._item_id_map.clear()
        client_mock._query.return_value = {
            "node": {
                "items": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [],
                }
            }
        }
        with pytest.raises(GitHubNotFoundError, match="No project item found"):
            tracker.transition_to("I_1", TransitionTarget.in_progress)


# ---------------------------------------------------------------------------
# is_still_triggered
# ---------------------------------------------------------------------------


class TestIsStillTriggered:
    def _build_issue(self, item_id: str, state: str = "In Progress") -> Issue:
        return Issue(
            id="I_1",
            identifier="o/r-42",
            title="Test",
            state=state,
            updatedAt="2025-01-01T00:00:00Z",
            tracker_data={
                "clone_url": "ssh://r",
                "project_item_id": item_id,
            },
        )

    @pytest.mark.parametrize(
        ("content_state", "trigger_name", "status_name", "expected"),
        [
            ("OPEN", "on", "In Progress", True),
            ("CLOSED", "on", "In Progress", False),
            ("OPEN", None, "In Progress", False),
            ("OPEN", "on", "Done", False),
            ("OPEN", "on", "QA", True),
        ],
        ids=["triggered", "closed", "trigger-off", "inactive-status", "qa"],
    )
    def test_triggered_follows_content_trigger_field_and_status(
        self,
        tracker: GitHubTracker,
        client_mock: MagicMock,
        content_state: str,
        trigger_name: str | None,
        status_name: str,
        expected: bool,
    ) -> None:
        client_mock._query.return_value = {
            "node": {
                "content": {"state": content_state},
                "fieldValues": {
                    "nodes": [
                        {
                            "field": {"name": "Symphony"},
                            "name": trigger_name,
                            "optionId": "opt_on" if trigger_name else None,
                        },
                        {"field": {"name": "Status"}, "name": status_name},
                    ],
                },
            }
        }
        issue = self._build_issue("PVTI_1")
        assert tracker.is_still_triggered(issue) is expected

    def test_qa_not_active_when_unconfigured(
        self, client_mock: MagicMock, config_no_qa: GitHubTrackerConfig
    ) -> None:
        tracker = _resolved_tracker(client_mock, config_no_qa)
        client_mock._query.return_value = {
            "node": {
                "content": {"state": "OPEN"},
                "fieldValues": {
                    "nodes": [
                        {"field": {"name": "Symphony"}, "name": "on"},
                        {"field": {"name": "Status"}, "name": "QA"},
                    ],
                },
            }
        }
        issue = Issue(
            id="I_1",
            identifier="o/r-42",
            title="Test",
            state="QA",
            updatedAt="2025-01-01T00:00:00Z",
            tracker_data={"project_item_id": "PVTI_1"},
        )
        assert tracker.is_still_triggered(issue) is False

    def test_missing_item_id_returns_false(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        issue = Issue(
            id="I_1",
            identifier="o/r-42",
            title="Test",
            state="In Progress",
            updatedAt="2025-01-01T00:00:00Z",
            tracker_data={},  # no project_item_id
        )
        assert tracker.is_still_triggered(issue) is False
        client_mock._query.assert_not_called()

    def test_item_not_found_returns_false(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        client_mock._query.return_value = {"node": None}
        issue = self._build_issue("PVTI_gone")
        assert tracker.is_still_triggered(issue) is False


# ---------------------------------------------------------------------------
# repo_url_for
# ---------------------------------------------------------------------------


class TestRepoUrlFor:
    def test_returns_clone_url_from_tracker_data(self, tracker: GitHubTracker) -> None:
        issue = Issue(
            id="I_1",
            identifier="o/r-42",
            title="Test",
            state="In Progress",
            updatedAt="2025-01-01T00:00:00Z",
            tracker_data={"clone_url": "git@github.com:o/r.git"},
        )
        assert tracker.repo_url_for(issue) == "git@github.com:o/r.git"

    def test_raises_when_no_tracker_data(self, tracker: GitHubTracker) -> None:
        issue = Issue(
            id="I_1",
            identifier="o/r-42",
            title="Test",
            state="In Progress",
            updatedAt="2025-01-01T00:00:00Z",
        )
        with pytest.raises(TrackerError, match="No repository linked"):
            tracker.repo_url_for(issue)


# ---------------------------------------------------------------------------
# QA helpers
# ---------------------------------------------------------------------------


class TestIsInQa:
    def _issue(self, state: str) -> Issue:
        return Issue(
            id="I_1",
            identifier="o/r-42",
            title="T",
            state=state,
            updatedAt="2025-01-01T00:00:00Z",
        )

    @pytest.mark.parametrize(
        ("state", "expected"), [("QA", True), ("In Progress", False)]
    )
    def test_reflects_the_issue_state(
        self, tracker: GitHubTracker, state: str, expected: bool
    ) -> None:
        assert tracker.is_in_qa(self._issue(state)) is expected

    def test_false_when_qa_not_configured(
        self, client_mock: MagicMock, config_no_qa: GitHubTrackerConfig
    ) -> None:
        tracker = _resolved_tracker(client_mock, config_no_qa)
        assert tracker.is_in_qa(self._issue("QA")) is False


class TestQaEnabled:
    def test_true_when_configured(self, tracker: GitHubTracker) -> None:
        assert tracker.qa_enabled is True

    def test_false_when_not_configured(
        self, client_mock: MagicMock, config_no_qa: GitHubTrackerConfig
    ) -> None:
        tracker = _resolved_tracker(client_mock, config_no_qa)
        assert tracker.qa_enabled is False


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
        self, tracker: GitHubTracker, target: TransitionTarget, expected: str
    ) -> None:
        assert tracker.transition_name_for(target) == expected

    def test_qa_raises_when_not_configured(
        self, client_mock: MagicMock, config_no_qa: GitHubTrackerConfig
    ) -> None:
        tracker = _resolved_tracker(client_mock, config_no_qa)
        with pytest.raises(ValueError, match="no qa_status"):
            tracker.transition_name_for(TransitionTarget.qa)


# ---------------------------------------------------------------------------
# Bot comment filtering (verifies author.id — not author.login — is used)
# ---------------------------------------------------------------------------


class TestBotCommentFiltering:
    """Verify that ``Comment.user_id`` holds a node id (not a login) so that
    the orchestrator's ``c.user_id != bot_user_id`` filter works correctly."""

    def _mock_raw_issue(self) -> dict[str, Any]:
        return {
            "node": {
                "__typename": "Issue",
                "id": "I_1",
                "number": 1,
                "title": "T",
                "body": "B",
                "state": "OPEN",
                "updatedAt": "2025-01-01T00:00:00Z",
                "repository": None,
            }
        }

    def _mock_comments_page(self, nodes: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "node": {
                "comments": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": nodes,
                }
            }
        }

    def test_comment_user_id_is_node_id(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        """A comment authored by a User gets the node id, not the login."""
        call_count = [0]

        def side_effect(
            query: str, variables: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            call_count[0] += 1
            if "comments" in query:
                return self._mock_comments_page(
                    [
                        {
                            "id": "c1",
                            "body": "hello",
                            "createdAt": "2025-01-01T00:00:00Z",
                            "author": {"id": "U_0123456789"},
                        },
                    ]
                )
            return self._mock_raw_issue()

        client_mock._query.side_effect = side_effect
        comments = tracker.list_comments_since("I_1", None)
        assert len(comments) == 1
        # Should be the node id, not a login string.
        assert comments[0].user_id == "U_0123456789"


# ---------------------------------------------------------------------------
# Comment pagination
# ---------------------------------------------------------------------------


class TestCommentPagination:
    def test_paginates_past_100(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        """When more than 100 comments exist, pagination walks all pages."""

        call_count = [0]

        def side_effect(
            query: str, variables: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            call_count[0] += 1
            if "comments" not in query:
                return {
                    "node": {
                        "__typename": "Issue",
                        "id": "I_1",
                        "number": 1,
                        "title": "T",
                        "body": "B",
                        "state": "OPEN",
                        "updatedAt": "2025-01-01T00:00:00Z",
                        "repository": None,
                    }
                }

            cursor = (variables or {}).get("cursor")
            if cursor is None:
                # First page: has next.
                return {
                    "node": {
                        "comments": {
                            "pageInfo": {"hasNextPage": True, "endCursor": "c2"},
                            "nodes": [
                                {
                                    "id": "c1",
                                    "body": "one",
                                    "createdAt": "2025-01-01T00:00:00Z",
                                    "author": {"id": "U1"},
                                },
                            ],
                        }
                    }
                }
            elif cursor == "c2":
                # Second page: has next.
                return {
                    "node": {
                        "comments": {
                            "pageInfo": {"hasNextPage": True, "endCursor": "c3"},
                            "nodes": [
                                {
                                    "id": "c2",
                                    "body": "two",
                                    "createdAt": "2025-01-02T00:00:00Z",
                                    "author": {"id": "U2"},
                                },
                            ],
                        }
                    }
                }
            else:
                # Third page: last.
                return {
                    "node": {
                        "comments": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {
                                    "id": "c3",
                                    "body": "three",
                                    "createdAt": "2025-01-03T00:00:00Z",
                                    "author": {"id": "U3"},
                                },
                            ],
                        }
                    }
                }

        client_mock._query.side_effect = side_effect
        comments = tracker.list_comments_since("I_1", None)
        assert len(comments) == 3
        assert [c.id for c in comments] == ["c1", "c2", "c3"]
        # 1 raw fetch + 3 pagination calls = 4 total
        assert call_count[0] == 4

    def test_comment_query_does_not_use_orderby(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        """The comment pagination query must not include orderBy — GitHub's
        IssueCommentOrderField does not accept CREATED_AT."""
        call_count = [0]

        def side_effect(
            query: str, variables: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            call_count[0] += 1
            if "comments" in query:
                # Assert the query text does not contain orderBy.
                assert "orderBy" not in query, (
                    f"Comment query must not contain orderBy: {query}"
                )
                return {
                    "node": {
                        "comments": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [],
                        }
                    }
                }
            return {
                "node": {
                    "__typename": "Issue",
                    "id": "I_1",
                    "number": 1,
                    "title": "T",
                    "body": "B",
                    "state": "OPEN",
                    "updatedAt": "2025-01-01T00:00:00Z",
                    "repository": None,
                }
            }

        client_mock._query.side_effect = side_effect
        tracker.list_comments_since("I_1", None)
        # Should have made a comment pagination call.
        assert call_count[0] >= 2  # raw issue + at least 1 comment call

    def test_sorts_out_of_order_pages_oldest_first(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        """Comments are returned oldest-first even when pages arrive out of
        chronological order.  Fetch order acts as stable tie-breaker."""
        call_count = [0]

        def side_effect(
            query: str, variables: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            call_count[0] += 1
            if "comments" not in query:
                return {
                    "node": {
                        "__typename": "Issue",
                        "id": "I_1",
                        "number": 1,
                        "title": "T",
                        "body": "B",
                        "state": "OPEN",
                        "updatedAt": "2025-01-01T00:00:00Z",
                        "repository": None,
                    }
                }

            cursor = (variables or {}).get("cursor")
            if cursor is None:
                # First page returns newer comments.
                return {
                    "node": {
                        "comments": {
                            "pageInfo": {"hasNextPage": True, "endCursor": "c2"},
                            "nodes": [
                                {
                                    "id": "c3",
                                    "body": "third",
                                    "createdAt": "2025-01-03T00:00:00Z",
                                    "author": {"id": "U3"},
                                },
                                {
                                    "id": "c4",
                                    "body": "fourth",
                                    "createdAt": "2025-01-04T00:00:00Z",
                                    "author": {"id": "U4"},
                                },
                            ],
                        }
                    }
                }
            else:
                # Second page returns older comments.
                return {
                    "node": {
                        "comments": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {
                                    "id": "c1",
                                    "body": "first",
                                    "createdAt": "2025-01-01T00:00:00Z",
                                    "author": {"id": "U1"},
                                },
                                {
                                    "id": "c2",
                                    "body": "second",
                                    "createdAt": "2025-01-02T00:00:00Z",
                                    "author": {"id": "U2"},
                                },
                            ],
                        }
                    }
                }

        client_mock._query.side_effect = side_effect
        comments = tracker.list_comments_since("I_1", None)
        assert len(comments) == 4
        # Must be sorted oldest-first regardless of page arrival order.
        assert [c.id for c in comments] == ["c1", "c2", "c3", "c4"]
        assert call_count[0] == 3  # raw + 2 comment pages

    def test_stable_sort_on_identical_timestamps(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        """When comments share the same createdAt, fetch order is preserved
        as the stable tie-breaker."""
        call_count = [0]

        def side_effect(
            query: str, variables: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            call_count[0] += 1
            if "comments" not in query:
                return {
                    "node": {
                        "__typename": "Issue",
                        "id": "I_1",
                        "number": 1,
                        "title": "T",
                        "body": "B",
                        "state": "OPEN",
                        "updatedAt": "2025-01-01T00:00:00Z",
                        "repository": None,
                    }
                }

            cursor = (variables or {}).get("cursor")
            if cursor is None:
                return {
                    "node": {
                        "comments": {
                            "pageInfo": {"hasNextPage": True, "endCursor": "c2"},
                            "nodes": [
                                {
                                    "id": "c1",
                                    "body": "one",
                                    "createdAt": "2025-01-02T00:00:00Z",
                                    "author": {"id": "U1"},
                                },
                                {
                                    "id": "c2",
                                    "body": "two",
                                    "createdAt": "2025-01-02T00:00:00Z",
                                    "author": {"id": "U2"},
                                },
                            ],
                        }
                    }
                }
            else:
                return {
                    "node": {
                        "comments": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {
                                    "id": "c3",
                                    "body": "three",
                                    "createdAt": "2025-01-03T00:00:00Z",
                                    "author": {"id": "U3"},
                                },
                            ],
                        }
                    }
                }

        client_mock._query.side_effect = side_effect
        comments = tracker.list_comments_since("I_1", None)
        assert len(comments) == 3
        # c1 and c2 share the same timestamp; they must appear in fetch order.
        assert [c.id for c in comments] == ["c1", "c2", "c3"]


# ---------------------------------------------------------------------------
# Trigger option resolution
# ---------------------------------------------------------------------------


class TestTriggerOptionResolution:
    def test_finds_on_option_explicitly(
        self, client_mock: MagicMock, config: GitHubTrackerConfig
    ) -> None:
        """When the trigger field has options ["off", "on"], the option named
        "on" is selected (not the first one)."""
        tracker = GitHubTracker(client_mock, config)
        tracker._project_node_id = "PVT_p1"
        client_mock._query.return_value = {
            "node": {
                "fields": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        {"id": "PVTSSF_status", "name": "Status", "options": []},
                        {
                            "id": "PVTSSF_sym",
                            "name": "Symphony",
                            "options": [
                                {"id": "opt_off", "name": "off"},
                                {"id": "opt_on", "name": "on"},
                            ],
                        },
                    ],
                }
            }
        }
        tracker._resolve_trigger_field()
        assert tracker._trigger_option_id == "opt_on"

    def test_missing_on_option_raises(
        self, client_mock: MagicMock, config: GitHubTrackerConfig
    ) -> None:
        """When the trigger field exists but has no 'on' option, raise."""
        tracker = GitHubTracker(client_mock, config)
        tracker._project_node_id = "PVT_p1"
        client_mock._query.return_value = {
            "node": {
                "fields": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        {
                            "id": "PVTSSF_sym",
                            "name": "Symphony",
                            "options": [
                                {"id": "opt_off", "name": "off"},
                            ],
                        },
                    ],
                }
            }
        }
        with pytest.raises(ValueError, match="no option named 'on'"):
            tracker._resolve_trigger_field()


# ---------------------------------------------------------------------------
# Clone protocol
# ---------------------------------------------------------------------------


class TestCloneProtocol:
    """Tests for the clone_protocol config option."""

    def test_invalid_clone_protocol_raises(self) -> None:
        with pytest.raises(ValueError, match="clone_protocol must be 'ssh' or 'https'"):
            GitHubTrackerConfig(
                token="ghp_test",
                project_ref="orgs/my-org/projects/1",
                in_progress_status="In Progress",
                needs_input_status="Needs Input",
                clone_protocol="http",
            )

    @pytest.mark.parametrize(
        ("clone_protocol", "repo", "expected"),
        [
            (
                "ssh",
                {"sshUrl": "git@github.com:o/r.git", "url": "https://github.com/o/r"},
                "git@github.com:o/r.git",
            ),
            (
                "https",
                {"sshUrl": "git@github.com:o/r.git", "url": "https://github.com/o/r"},
                "https://github.com/o/r.git",
            ),
            ("https", {"sshUrl": "git@github.com:o/r.git"}, None),
            ("ssh", None, None),
        ],
        ids=["ssh", "https", "https-no-url", "no-repo"],
    )
    def test_repo_clone_url(
        self,
        client_mock: MagicMock,
        config: GitHubTrackerConfig,
        clone_protocol: str,
        repo: dict[str, Any] | None,
        expected: str | None,
    ) -> None:
        config.clone_protocol = clone_protocol
        tracker = GitHubTracker(client_mock, config)
        assert tracker._repo_clone_url(repo) == expected


# ---------------------------------------------------------------------------
# download_attachment
# ---------------------------------------------------------------------------


class TestDownloadAttachment:
    """Tests for download_attachment on GitHubTracker."""

    def test_success_returns_bytes_and_content_type(
        self, tracker: GitHubTracker
    ) -> None:
        from unittest.mock import patch

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json; charset=utf-8"}
        mock_response.iter_bytes.return_value = [b'{"key":', b" 123}"]

        with patch("symphony_linear.github_tracker.httpx.stream") as mock_stream:
            mock_stream.return_value.__enter__.return_value = mock_response
            data, ct = tracker.download_attachment(
                "https://github.com/some/repo/assets/data.json"
            )

        assert data == b'{"key": 123}'
        assert ct == "application/json"
        mock_stream.assert_called_once()
        call_kwargs = mock_stream.call_args.kwargs
        assert isinstance(call_kwargs["auth"], _GitHubAuth)
        assert call_kwargs["auth"]._token == "ghp_test"
        assert call_kwargs["follow_redirects"] is True
        assert call_kwargs["timeout"] == 30.0

    def test_missing_content_type_returns_none(self, tracker: GitHubTracker) -> None:
        from unittest.mock import patch

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}
        mock_response.iter_bytes.return_value = [b"binary-data"]

        with patch("symphony_linear.github_tracker.httpx.stream") as mock_stream:
            mock_stream.return_value.__enter__.return_value = mock_response
            data, ct = tracker.download_attachment(
                "https://user-images.githubusercontent.com/123/f.bin"
            )

        assert data == b"binary-data"
        assert ct is None

    def test_http_404_raises_attachment_download_error(
        self, tracker: GitHubTracker
    ) -> None:
        from unittest.mock import patch

        mock_response = MagicMock()
        mock_response.status_code = 404

        with patch("symphony_linear.github_tracker.httpx.stream") as mock_stream:
            mock_stream.return_value.__enter__.return_value = mock_response
            with pytest.raises(AttachmentDownloadError, match="HTTP 404"):
                tracker.download_attachment("https://github.com/some/repo/missing.txt")

    def test_content_length_exceeds_limit_raises_too_large(
        self, tracker: GitHubTracker
    ) -> None:
        from unittest.mock import patch

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-length": str(15 * 1024 * 1024)}

        with patch("symphony_linear.github_tracker.httpx.stream") as mock_stream:
            mock_stream.return_value.__enter__.return_value = mock_response
            with pytest.raises(AttachmentTooLargeError, match="bytes"):
                tracker.download_attachment(
                    "https://objects.githubusercontent.com/huge.bin"
                )

    def test_oversize_response_body_raises_too_large(
        self, tracker: GitHubTracker
    ) -> None:
        from unittest.mock import patch

        big_chunk = b"x" * (11 * 1024 * 1024)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}
        mock_response.iter_bytes.return_value = [big_chunk]

        with patch("symphony_linear.github_tracker.httpx.stream") as mock_stream:
            mock_stream.return_value.__enter__.return_value = mock_response
            with pytest.raises(AttachmentTooLargeError, match="exceeds"):
                tracker.download_attachment(
                    "https://private-user-images.githubusercontent.com/huge.bin"
                )

    @pytest.mark.parametrize(
        "error_type", ["NetworkError", "TimeoutException"], ids=["network", "timeout"]
    )
    def test_network_error_raises_attachment_download_error(
        self, tracker: GitHubTracker, error_type: str
    ) -> None:
        from unittest.mock import patch

        import httpx

        error = getattr(httpx, error_type)("failed")
        with patch("symphony_linear.github_tracker.httpx.stream", side_effect=error):
            with pytest.raises(AttachmentDownloadError, match="Download failed"):
                tracker.download_attachment("https://github.com/some/repo/f.txt")

    # ------------------------------------------------------------------
    # SSRF defence — host allowlist validation before any I/O
    # ------------------------------------------------------------------

    def test_non_allowlisted_host_raises_before_http(
        self, tracker: GitHubTracker
    ) -> None:
        from unittest.mock import patch

        with patch("symphony_linear.github_tracker.httpx.stream") as mock_stream:
            with pytest.raises(AttachmentDownloadError, match="host not on allowlist"):
                tracker.download_attachment("https://evil.example.com/x.png")

        mock_stream.assert_not_called()

    def test_http_allowlisted_host_raises_before_http(
        self, tracker: GitHubTracker
    ) -> None:
        from unittest.mock import patch

        with patch("symphony_linear.github_tracker.httpx.stream") as mock_stream:
            with pytest.raises(AttachmentDownloadError, match="host not on allowlist"):
                tracker.download_attachment(
                    "http://user-images.githubusercontent.com/12345/img.png"
                )

        mock_stream.assert_not_called()

    def test_ip_literal_raises_before_http(self, tracker: GitHubTracker) -> None:
        from unittest.mock import patch

        with patch("symphony_linear.github_tracker.httpx.stream") as mock_stream:
            with pytest.raises(AttachmentDownloadError, match="host not on allowlist"):
                tracker.download_attachment("https://169.254.169.254/x.png")

        mock_stream.assert_not_called()


# ---------------------------------------------------------------------------
# is_upload_url
# ---------------------------------------------------------------------------


class TestIsUploadUrl:
    """Tests for GitHubTracker.is_upload_url.

    The download allowlist contains all of github.com, so extraction must
    only treat ``/user-attachments/`` paths as uploads there.
    """

    @pytest.mark.parametrize(
        "url",
        [
            "https://user-images.githubusercontent.com/123/x.png",
            "https://private-user-images.githubusercontent.com/123/x.png",
            "https://objects.githubusercontent.com/x.bin",
            "https://github.com/user-attachments/assets/abc-123",
            "https://github.com/user-attachments/files/123/archive.zip",
        ],
    )
    def test_upload_urls(self, tracker: GitHubTracker, url: str) -> None:
        assert tracker.is_upload_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/org/repo",
            "https://github.com/org/repo/pull/1",
            "https://github.com/org/repo/blob/main/file.zip",
            "https://github.com/user-attachments",
            "https://raw.githubusercontent.com/org/repo/main/f.py",
            "https://evil.example.com/x.png",
            "http://user-images.githubusercontent.com/123/x.png",
            "https://169.254.169.254/x.png",
            "",
            "not a url",
        ],
    )
    def test_non_upload_urls(self, tracker: GitHubTracker, url: str) -> None:
        assert tracker.is_upload_url(url) is False


# ---------------------------------------------------------------------------
# _GitHubAuth (host-based auth header restriction)
# ---------------------------------------------------------------------------


class TestGitHubAuth:
    """Tests for the _GitHubAuth custom httpx.Auth class."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://user-images.githubusercontent.com/12345/img.png",
            "https://private-user-images.githubusercontent.com/67890/pic.jpg",
            "https://github.com/some/repo/assets/file.gif",
            "https://objects.githubusercontent.com/s3-key",
        ],
    )
    def test_allowlisted_host_gets_auth_header(self, url: str) -> None:
        import httpx

        auth = _GitHubAuth("ghp_secret")
        req = httpx.Request("GET", url)
        flow = auth.auth_flow(req)
        result = next(flow)
        assert result.headers["Authorization"] == "Bearer ghp_secret"

    def test_non_allowlisted_host_gets_no_auth_header(self) -> None:
        import httpx

        auth = _GitHubAuth("ghp_secret")
        req = httpx.Request("GET", "https://evil.example.com/log")
        flow = auth.auth_flow(req)
        result = next(flow)
        assert "Authorization" not in result.headers

    def test_http_allowlisted_host_denied(self) -> None:
        """An HTTP (not HTTPS) request to an allowlisted host does NOT
        receive the auth header — prevents plaintext token leaks."""
        import httpx

        auth = _GitHubAuth("ghp_secret")
        req = httpx.Request(
            "GET", "http://user-images.githubusercontent.com/12345/img.png"
        )
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
        ``_GitHubAuth`` class as defence-in-depth for the first request.
        This test confirms that when a GitHub CDN host redirects to an
        external host the second hop carries no credentials.
        """
        import httpx

        target_seen = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal target_seen
            if request.url.host.endswith("githubusercontent.com"):
                return httpx.Response(
                    302,
                    headers={"Location": "https://external-cdn.example.com/final.jpg"},
                )
            # This is the redirect target — must NOT carry Authorization.
            target_seen = True
            assert "Authorization" not in request.headers
            return httpx.Response(200, content=b"ok")

        transport = httpx.MockTransport(handler)
        auth = _GitHubAuth("ghp_secret")

        with httpx.Client(
            transport=transport,
            auth=auth,
            follow_redirects=True,
            base_url="https://user-images.githubusercontent.com",
        ) as client:
            resp = client.get("/12345/img.png")

        assert resp.status_code == 200
        assert target_seen, "redirect target was never requested"
