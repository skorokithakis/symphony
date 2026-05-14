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
    _parse_project_ref,
)
from symphony_linear.linear import Issue
from symphony_linear.state import StateManager
from symphony_linear.tracker import (
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
    repo_ssh: str = "git@github.com:my-org/my-repo.git",
    updated_at: str = "2025-01-01T00:00:00Z",
) -> dict[str, Any]:
    """Build a project-item node dict."""

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

    return {
        "id": item_id,
        "content": {
            "__typename": "Issue",
            "id": issue_id,
            "number": number,
            "title": title,
            "state": state,
            "updatedAt": updated_at,
            "repository": {
                "sshUrl": repo_ssh,
                "nameWithOwner": repo_name,
            },
        },
        "fieldValues": {"nodes": field_values},
    }


def _single_select_fields(
    fields: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return fields


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_is_tracker_instance(
        self, client_mock: MagicMock, config: GitHubTrackerConfig
    ) -> None:
        t = GitHubTracker(client_mock, config)
        assert isinstance(t, Tracker)

    def test_all_methods_present(self) -> None:
        protocol_methods = [
            name
            for name in dir(Tracker)
            if not name.startswith("_") and callable(getattr(Tracker, name, None))
        ]
        tracker_methods = [
            name
            for name in dir(GitHubTracker)
            if not name.startswith("_") and callable(getattr(GitHubTracker, name, None))
        ]
        missing = [m for m in protocol_methods if m not in tracker_methods]
        assert missing == [], f"GitHubTracker missing methods: {missing}"


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
    def test_orgs_variant(self) -> None:
        owner_type, owner_name, number = _parse_project_ref("orgs/my-org/projects/5")
        assert owner_type == "orgs"
        assert owner_name == "my-org"
        assert number == 5

    def test_users_variant(self) -> None:
        owner_type, owner_name, number = _parse_project_ref("users/alice/projects/10")
        assert owner_type == "users"
        assert owner_name == "alice"
        assert number == 10

    def test_invalid_ref_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid project ref"):
            _parse_project_ref("garbage")

    def test_incomplete_ref_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid project ref"):
            _parse_project_ref("orgs/my-org/projects")

    def test_bad_prefix_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid project ref"):
            _parse_project_ref("teams/my-org/projects/1")


# ---------------------------------------------------------------------------
# resolve() — project resolution
# ---------------------------------------------------------------------------


class TestResolveProject:
    def test_resolves_org_project(
        self, client_mock: MagicMock, config: GitHubTrackerConfig
    ) -> None:
        tracker = GitHubTracker(client_mock, config)
        client_mock._query.return_value = {
            "organization": {
                "projectV2": {"id": "PVT_abc", "title": "My Project"},
            }
        }
        tracker._resolve_project()
        assert tracker._project_node_id == "PVT_abc"

        client_mock._query.assert_called_once()
        call_args = client_mock._query.call_args
        assert "organization(login:" in call_args[0][0]

    def test_resolves_user_project(
        self, client_mock: MagicMock, config: GitHubTrackerConfig
    ) -> None:
        config.project_ref = "users/alice/projects/2"
        tracker = GitHubTracker(client_mock, config)
        client_mock._query.return_value = {
            "user": {
                "projectV2": {"id": "PVT_xyz", "title": "Alice Project"},
            }
        }
        tracker._resolve_project()
        assert tracker._project_node_id == "PVT_xyz"

        call_args = client_mock._query.call_args
        assert "user(login:" in call_args[0][0]

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

        tracker.ensure_trigger_setup(state)

        assert tracker._project_node_id == "PVT_p1"
        assert tracker._trigger_field_id == "PVTSSF_sym"
        assert tracker._trigger_option_id == "opt_on"


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
        assert issues[0].identifier == "my-org-my-repo-42"
        assert issues[0].title == "Fix bug"
        assert issues[0].state == "In Progress"
        assert issues[0].labels == []
        assert issues[0].tracker_data == {
            "ssh_url": "git@github.com:my-org/my-repo.git",
            "project_item_id": "PVTI_1",
        }

    def test_filters_closed_issues(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        client_mock._query.return_value = {
            "node": {
                "items": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        _issue_item(issue_id="I_1", state="CLOSED"),
                        _issue_item(issue_id="I_2", state="OPEN"),
                    ],
                }
            }
        }
        issues = tracker.list_triggered_issues()
        assert len(issues) == 1
        assert issues[0].id == "I_2"

    def test_filters_non_triggered(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        client_mock._query.return_value = {
            "node": {
                "items": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        _issue_item(issue_id="I_1", trigger_on=False),
                        _issue_item(issue_id="I_2", trigger_on=True),
                    ],
                }
            }
        }
        issues = tracker.list_triggered_issues()
        assert len(issues) == 1
        assert issues[0].id == "I_2"

    def test_filters_inactive_status(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        client_mock._query.return_value = {
            "node": {
                "items": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        _issue_item(issue_id="I_1", status_name="Done"),
                        _issue_item(issue_id="I_2", status_name="In Progress"),
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
            query: str, variables: dict[str, Any] | None = None
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
        assert issues[0].tracker_data["ssh_url"] is None

    def test_includes_qa_when_configured(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
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
        assert len(issues) == 1

    def test_excludes_qa_when_not_configured(
        self, client_mock: MagicMock, config_no_qa: GitHubTrackerConfig
    ) -> None:
        tracker = _resolved_tracker(client_mock, config_no_qa)
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
        assert len(issues) == 0

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
            query: str, variables: dict[str, Any] | None = None
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
                    "repository": {
                        "sshUrl": "git@github.com:my-org/my-repo.git",
                        "nameWithOwner": "my-org/my-repo",
                    },
                }
            }

        client_mock._query.side_effect = side_effect
        tracker._item_id_map["I_1"] = "PVTI_1"

        issue = tracker.get_issue("I_1")
        assert issue.id == "I_1"
        assert issue.identifier == "my-org-my-repo-42"
        assert issue.description == "Description text"
        assert issue.state == "In Progress"
        assert issue.tracker_data is not None
        assert issue.tracker_data["ssh_url"] == "git@github.com:my-org/my-repo.git"

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
            query: str, variables: dict[str, Any] | None = None
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
                        "repository": {"sshUrl": "ssh://r", "nameWithOwner": "o/r"},
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
        with tracker._item_map_lock:
            assert tracker._item_id_map == {"I_1": "PVTI_1"}

    def test_handles_missing_item_in_project(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        # Issue exists but is not in the project.
        call_count = [0]

        def side_effect(
            query: str, variables: dict[str, Any] | None = None
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
                        "repository": {"sshUrl": "ssh://r", "nameWithOwner": "o/r"},
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

    def _comments_issue_raw(
        self, comment_nodes: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return {
            "node": {
                "__typename": "Issue",
                "id": "I_1",
                "comments": {"nodes": comment_nodes},
            }
        }

    def test_returns_all_when_last_seen_is_none(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        nodes = [
            self._comment_node("c1", "First", "user1"),
            self._comment_node("c2", "Second"),
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

        call_count = [0]

        def side_effect(
            query: str, variables: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            call_count[0] += 1
            if "comments" in query:
                return comment_page
            return issue_raw

        client_mock._query.side_effect = side_effect
        comments = tracker.list_comments_since("I_1", None)
        assert len(comments) == 2
        assert comments[0].id == "c1"
        assert comments[0].user_id == "user1"

    def test_returns_after_last_seen(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        nodes = [
            self._comment_node("c1", "First"),
            self._comment_node("c2", "Second"),
            self._comment_node("c3", "Third"),
        ]
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

        call_count = [0]

        def side_effect(
            query: str, variables: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            call_count[0] += 1
            if "comments" in query:
                return comment_page
            return issue_raw

        client_mock._query.side_effect = side_effect
        comments = tracker.list_comments_since("I_1", "c2")
        assert len(comments) == 1
        assert comments[0].id == "c3"

    def test_returns_empty_when_last_matches(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        nodes = [
            self._comment_node("c1", "First"),
            self._comment_node("c2", "Second"),
        ]
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

        call_count = [0]

        def side_effect(
            query: str, variables: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            call_count[0] += 1
            if "comments" in query:
                return comment_page
            return issue_raw

        client_mock._query.side_effect = side_effect
        comments = tracker.list_comments_since("I_1", "c2")
        assert comments == []

    def test_returns_empty_when_not_found(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        nodes = [self._comment_node("c1", "First")]
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

        call_count = [0]

        def side_effect(
            query: str, variables: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            call_count[0] += 1
            if "comments" in query:
                return comment_page
            return issue_raw

        client_mock._query.side_effect = side_effect
        comments = tracker.list_comments_since("I_1", "deleted-id")
        assert comments == []

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
                        "body": "hello world",
                        "createdAt": "2025-01-01T00:00:00Z",
                        "author": {"id": "U_bot123"},
                    }
                }
            }
        }
        comment = tracker.post_comment("I_1", "hello world")
        assert comment.id == "c_new"
        assert comment.body == "hello world"
        assert comment.user_id == "U_bot123"

    def test_no_comment_raises(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        client_mock._query.return_value = {
            "addComment": {"commentEdge": {"node": None}}
        }
        with pytest.raises(GitHubError, match="no comment"):
            tracker.post_comment("I_1", "x")


class TestEditComment:
    def test_updates_successfully(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        client_mock._query.return_value = {"updateIssueComment": {}}
        # Should not raise.
        tracker.edit_comment("c_1", "updated body")


# ---------------------------------------------------------------------------
# transition_to
# ---------------------------------------------------------------------------


class TestTransitionTo:
    def test_transitions_to_in_progress(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        tracker._item_id_map["I_1"] = "PVTI_1"
        client_mock._query.return_value = {"updateProjectV2ItemFieldValue": {}}
        tracker.transition_to("I_1", TransitionTarget.in_progress)

        call_args = client_mock._query.call_args
        variables = call_args[0][1]["input"]
        assert variables["fieldId"] == "PVTSSF_status"
        assert variables["itemId"] == "PVTI_1"
        assert variables["value"]["singleSelectOptionId"] == "opt_ip"

    def test_transitions_to_needs_input(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        tracker._item_id_map["I_1"] = "PVTI_1"
        client_mock._query.return_value = {"updateProjectV2ItemFieldValue": {}}
        tracker.transition_to("I_1", TransitionTarget.needs_input)

        call_args = client_mock._query.call_args
        variables = call_args[0][1]["input"]
        assert variables["value"]["singleSelectOptionId"] == "opt_ni"

    def test_transitions_to_qa(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        tracker._item_id_map["I_1"] = "PVTI_1"
        client_mock._query.return_value = {"updateProjectV2ItemFieldValue": {}}
        tracker.transition_to("I_1", TransitionTarget.qa)

        call_args = client_mock._query.call_args
        variables = call_args[0][1]["input"]
        assert variables["value"]["singleSelectOptionId"] == "opt_qa"

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
            query: str, variables: dict[str, Any] | None = None
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
                "ssh_url": "ssh://r",
                "project_item_id": item_id,
            },
        )

    def test_triggered_returns_true(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        client_mock._query.return_value = {
            "node": {
                "content": {"state": "OPEN"},
                "fieldValues": {
                    "nodes": [
                        {
                            "field": {"name": "Symphony"},
                            "name": "on",
                            "optionId": "opt_on",
                        },
                        {"field": {"name": "Status"}, "name": "In Progress"},
                    ],
                },
            }
        }
        issue = self._build_issue("PVTI_1")
        assert tracker.is_still_triggered(issue) is True

    def test_closed_issue_returns_false(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        client_mock._query.return_value = {
            "node": {
                "content": {"state": "CLOSED"},
                "fieldValues": {
                    "nodes": [
                        {
                            "field": {"name": "Symphony"},
                            "name": "on",
                            "optionId": "opt_on",
                        },
                        {"field": {"name": "Status"}, "name": "In Progress"},
                    ],
                },
            }
        }
        issue = self._build_issue("PVTI_1")
        assert tracker.is_still_triggered(issue) is False

    def test_not_triggered_field_off_returns_false(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        client_mock._query.return_value = {
            "node": {
                "content": {"state": "OPEN"},
                "fieldValues": {
                    "nodes": [
                        {"field": {"name": "Symphony"}, "name": None, "optionId": None},
                        {"field": {"name": "Status"}, "name": "In Progress"},
                    ],
                },
            }
        }
        issue = self._build_issue("PVTI_1")
        assert tracker.is_still_triggered(issue) is False

    def test_inactive_status_returns_false(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        client_mock._query.return_value = {
            "node": {
                "content": {"state": "OPEN"},
                "fieldValues": {
                    "nodes": [
                        {
                            "field": {"name": "Symphony"},
                            "name": "on",
                            "optionId": "opt_on",
                        },
                        {"field": {"name": "Status"}, "name": "Done"},
                    ],
                },
            }
        }
        issue = self._build_issue("PVTI_1")
        assert tracker.is_still_triggered(issue) is False

    def test_qa_is_active(self, tracker: GitHubTracker, client_mock: MagicMock) -> None:
        client_mock._query.return_value = {
            "node": {
                "content": {"state": "OPEN"},
                "fieldValues": {
                    "nodes": [
                        {
                            "field": {"name": "Symphony"},
                            "name": "on",
                            "optionId": "opt_on",
                        },
                        {"field": {"name": "Status"}, "name": "QA"},
                    ],
                },
            }
        }
        issue = self._build_issue("PVTI_1")
        assert tracker.is_still_triggered(issue) is True

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
    def test_returns_ssh_url_from_tracker_data(self, tracker: GitHubTracker) -> None:
        issue = Issue(
            id="I_1",
            identifier="o/r-42",
            title="Test",
            state="In Progress",
            updatedAt="2025-01-01T00:00:00Z",
            tracker_data={"ssh_url": "git@github.com:o/r.git"},
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
# human_trigger_description
# ---------------------------------------------------------------------------


class TestHumanTriggerDescription:
    def test_default_field_name(self, tracker: GitHubTracker) -> None:
        assert "Symphony" in tracker.human_trigger_description()
        assert "off" in tracker.human_trigger_description()

    def test_custom_field_name(
        self, client_mock: MagicMock, config: GitHubTrackerConfig
    ) -> None:
        config.trigger_field = "Agent"
        tracker = GitHubTracker(client_mock, config)
        assert "Agent" in tracker.human_trigger_description()


# ---------------------------------------------------------------------------
# current_user_id
# ---------------------------------------------------------------------------


class TestCurrentUserIdDelegation:
    def test_delegates(self, tracker: GitHubTracker, client_mock: MagicMock) -> None:
        client_mock.current_user_id.return_value = "gh-user-1"
        assert tracker.current_user_id() == "gh-user-1"
        client_mock.current_user_id.assert_called_once()


# ---------------------------------------------------------------------------
# QA helpers
# ---------------------------------------------------------------------------


class TestIsInQa:
    def test_true_when_in_qa(self, tracker: GitHubTracker) -> None:
        issue = Issue(
            id="I_1",
            identifier="o/r-42",
            title="T",
            state="QA",
            updatedAt="2025-01-01T00:00:00Z",
        )
        assert tracker.is_in_qa(issue) is True

    def test_false_when_not_qa(self, tracker: GitHubTracker) -> None:
        issue = Issue(
            id="I_1",
            identifier="o/r-42",
            title="T",
            state="In Progress",
            updatedAt="2025-01-01T00:00:00Z",
        )
        assert tracker.is_in_qa(issue) is False

    def test_false_when_qa_not_configured(
        self, client_mock: MagicMock, config_no_qa: GitHubTrackerConfig
    ) -> None:
        tracker = _resolved_tracker(client_mock, config_no_qa)
        issue = Issue(
            id="I_1",
            identifier="o/r-42",
            title="T",
            state="QA",
            updatedAt="2025-01-01T00:00:00Z",
        )
        assert tracker.is_in_qa(issue) is False


class TestQaEnabled:
    def test_true_when_configured(self, tracker: GitHubTracker) -> None:
        assert tracker.qa_enabled is True

    def test_false_when_not_configured(
        self, client_mock: MagicMock, config_no_qa: GitHubTrackerConfig
    ) -> None:
        tracker = _resolved_tracker(client_mock, config_no_qa)
        assert tracker.qa_enabled is False


class TestTransitionNameFor:
    def test_in_progress(self, tracker: GitHubTracker) -> None:
        assert (
            tracker.transition_name_for(TransitionTarget.in_progress) == "In Progress"
        )

    def test_needs_input(self, tracker: GitHubTracker) -> None:
        assert (
            tracker.transition_name_for(TransitionTarget.needs_input) == "Needs Input"
        )

    def test_qa(self, tracker: GitHubTracker) -> None:
        assert tracker.transition_name_for(TransitionTarget.qa) == "QA"

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

    def test_bot_comments_can_be_filtered(
        self, tracker: GitHubTracker, client_mock: MagicMock
    ) -> None:
        """Bot user id (from current_user_id) and comment user_id are both
        node ids — the inequality check works."""
        client_mock.current_user_id.return_value = "U_bot_node_id"

        call_count = [0]

        def side_effect(
            query: str, variables: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            call_count[0] += 1
            if "comments" in query:
                return self._mock_comments_page(
                    [
                        {
                            "id": "c_bot",
                            "body": "bot reply",
                            "createdAt": "2025-01-02T00:00:00Z",
                            "author": {"id": "U_bot_node_id"},
                        },
                        {
                            "id": "c_human",
                            "body": "human reply",
                            "createdAt": "2025-01-03T00:00:00Z",
                            "author": {"id": "U_human_node_id"},
                        },
                    ]
                )
            return self._mock_raw_issue()

        client_mock._query.side_effect = side_effect
        comments = tracker.list_comments_since("I_1", None)

        # Filter out bot comments the way the orchestrator does.
        bot_id = tracker.current_user_id()
        human = [c for c in comments if c.user_id != bot_id]
        assert len(human) == 1
        assert human[0].user_id == "U_human_node_id"


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
