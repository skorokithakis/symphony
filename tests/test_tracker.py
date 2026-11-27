"""Tests for the tracker protocol utilities."""

from __future__ import annotations

import logging

import pytest

from symphony_linear.linear import Issue, Project
from symphony_linear.tracker import (
    is_bot_comment,
    model_for_issue,
    model_from_labels,
    model_label_name,
)


def _make_issue(labels: list[str], project_labels: list[str] | None) -> Issue:
    """Build a minimal Issue with *labels*, and a project unless None."""
    project = (
        None
        if project_labels is None
        else Project(id="proj-1", name="Test Project", labels=project_labels)
    )
    return Issue(
        id="ticket-1",
        identifier="TEAM-1",
        title="Test ticket",
        state="In Progress",
        labels=labels,
        project=project,
        updatedAt="2025-06-01T00:00:00Z",
    )


class TestIsBotComment:
    @pytest.mark.parametrize(
        "body",
        [
            "Some work done\n\n*Symphony · workspace*",
            "Done.\n\n*Symphony · context: 37,074 tokens*",
            # The marker is the prefix alone; trailing content does not matter.
            "Foo\n\n*Symphony · ",
        ],
    )
    def test_marker_present(self, body: str) -> None:
        assert is_bot_comment(body) is True

    @pytest.mark.parametrize(
        "body",
        [
            "This is a regular human comment.",
            # 'Symphony' without the middle dot is not the marker.
            "I like Symphony music",
            "",
        ],
    )
    def test_marker_absent(self, body: str) -> None:
        assert is_bot_comment(body) is False


class TestModelFromLabels:
    """Unit tests for the Model: <value> label resolver."""

    ALIASES = {"strong": "anthropic/claude-opus-5", "cheap": "openai/gpt-5-mini"}

    @pytest.mark.parametrize("labels", [["Agent", "Bug"], []])
    def test_no_matching_label_returns_none(self, labels: list[str]) -> None:
        assert model_from_labels(labels, self.ALIASES) is None

    def test_alias_hit_returns_mapped_id(self) -> None:
        assert (
            model_from_labels(["Agent", "Model: strong"], self.ALIASES)
            == "anthropic/claude-opus-5"
        )

    @pytest.mark.parametrize("label", ["MODEL: strong", "Model: STRONG"])
    def test_prefix_and_alias_lookup_are_case_insensitive(self, label: str) -> None:
        assert model_from_labels([label], self.ALIASES) == "anthropic/claude-opus-5"

    def test_unknown_value_passed_through_verbatim(self) -> None:
        raw = "anthropic/claude-sonnet-4-6"
        assert model_from_labels([f"Model: {raw}"], self.ALIASES) == raw

    @pytest.mark.parametrize("label", ["Model:   strong   ", "  Model: strong  "])
    def test_value_and_label_are_stripped(self, label: str) -> None:
        assert model_from_labels([label], self.ALIASES) == "anthropic/claude-opus-5"

    def test_empty_value_ignored_with_warning_naming_the_ticket(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            assert model_from_labels(["Model: "], self.ALIASES) is None
        assert any(
            "empty" in rec.message and "on ticket" in rec.message
            for rec in caplog.records
        )

    def test_multiple_labels_take_the_first_in_sorted_order(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            result = model_from_labels(["Model: zeta", "Model: alpha"], self.ALIASES)
        assert result == "alpha"
        assert any("labels on ticket" in rec.message for rec in caplog.records)

    def test_multiple_labels_skip_empty_candidate(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            result = model_from_labels(["Model: ", "Model: strong"], self.ALIASES)
        assert result == "anthropic/claude-opus-5"
        # The multiple-match warning must name the label actually selected,
        # not the (skipped) empty candidate that sorts first.
        assert any("'Model: strong'" in rec.message for rec in caplog.records)

    def test_near_prefix_labels_do_not_match(self) -> None:
        assert model_from_labels(["Model", "modelish"], self.ALIASES) is None

    def test_source_is_interpolated_into_both_warnings(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            result = model_from_labels(
                ["Model: ", "Model: strong"], self.ALIASES, source="project"
            )
        assert result == "anthropic/claude-opus-5"
        messages = [rec.message for rec in caplog.records]
        assert any("empty value" in m and "on project" in m for m in messages)
        assert any("labels on project" in m for m in messages)
        assert not any("ticket" in m for m in messages)


class TestModelForIssue:
    """Unit tests for the two-tier issue > project resolver."""

    ALIASES = {"strong": "anthropic/claude-opus-5", "cheap": "openai/gpt-5-mini"}

    def test_issue_label_wins_over_project_label(self) -> None:
        issue = _make_issue(["Agent", "Model: strong"], ["Model: cheap"])
        assert model_for_issue(issue, self.ALIASES) == "anthropic/claude-opus-5"

    def test_project_label_used_when_issue_has_none(self) -> None:
        issue = _make_issue(["Agent"], ["Team: core", "Model: cheap"])
        assert model_for_issue(issue, self.ALIASES) == "openai/gpt-5-mini"

    @pytest.mark.parametrize("project_labels", [["Team: core"], [], None])
    def test_no_model_label_on_either_tier_returns_none(
        self, project_labels: list[str] | None
    ) -> None:
        """``project_labels=None`` is the GitHub backend, which has no project."""
        issue = _make_issue(["Agent"], project_labels)
        assert model_for_issue(issue, self.ALIASES) is None

    def test_no_project_still_honours_issue_label(self) -> None:
        issue = _make_issue(["Model: cheap"], None)
        assert model_for_issue(issue, self.ALIASES) == "openai/gpt-5-mini"

    def test_empty_issue_label_falls_through_to_project(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An unusable issue-tier label is not an override, so the project
        default still applies, and the warning names the ticket tier."""
        issue = _make_issue(["Model: "], ["Model: strong"])
        with caplog.at_level(logging.WARNING):
            assert model_for_issue(issue, self.ALIASES) == "anthropic/claude-opus-5"
        messages = [rec.message for rec in caplog.records]
        assert any("empty" in m and "on ticket" in m for m in messages)
        assert not any("project" in m for m in messages)

    def test_empty_project_label_warning_names_the_project(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A bad label on the project must not send the operator to the
        ticket, which carries no Model: label at all."""
        issue = _make_issue(["Agent"], ["Model: "])
        with caplog.at_level(logging.WARNING):
            assert model_for_issue(issue, self.ALIASES) is None
        messages = [rec.message for rec in caplog.records]
        assert any("empty value" in m and "on project" in m for m in messages)
        assert not any("ticket" in m for m in messages)


class TestModelLabelName:
    """Unit tests for the Model: <alias> label name builder."""

    def test_simple_alias(self) -> None:
        assert model_label_name("Strong") == "Model: Strong"

    def test_roundtrip_resolves_via_model_from_labels(self) -> None:
        """A label built by model_label_name resolves through the parser."""
        assert (
            model_from_labels(
                [model_label_name("Cheap")], {"cheap": "openai/gpt-5-mini"}
            )
            == "openai/gpt-5-mini"
        )
