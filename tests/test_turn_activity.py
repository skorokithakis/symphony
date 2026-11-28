"""Tests for the in-memory step record behind the web UI's ticket page.

Nothing here touches an agent: the class is fed by the adapters' stdout drain
threads in production, so the tests call the record/read methods directly.
"""

from __future__ import annotations

from symphony_linear.turn_activity import (
    AgentStep,
    StepPage,
    TurnActivity,
    _MAX_STEPS_PER_TURN,
    _MAX_STEP_TEXT_LENGTH,
    _MAX_TOOL_SUMMARY_LENGTH,
    one_line_summary,
    tool_step_summary,
)


def _text(text: str) -> AgentStep:
    return AgentStep(kind="text", text=text)


def _tool(tool: str, summary: str) -> AgentStep:
    return AgentStep(kind="tool", tool=tool, text=summary)


class TestNoTurn:
    def test_an_unknown_ticket_is_a_replacement_with_no_identifier(self) -> None:
        page = TurnActivity().read_steps("ticket-1", None)

        assert page == StepPage(
            turn_identifier=None, steps=(), total_steps=0, replace=True
        )

    def test_output_alone_does_not_create_a_turn(self) -> None:
        """Only a step mints the identifier; a turn that has said nothing has
        no steps to show."""
        activity = TurnActivity()
        activity.record_output("ticket-1")

        assert activity.read_steps("ticket-1", None).turn_identifier is None


class TestCursor:
    def test_a_cursor_matching_the_identifier_appends_the_new_steps(self) -> None:
        activity = TurnActivity()
        activity.record_step("ticket-1", _text("Looking at the code."))
        first = activity.read_steps("ticket-1", None)
        activity.record_step("ticket-1", _tool("bash", "pytest -q"))

        page = activity.read_steps(
            "ticket-1", (first.turn_identifier, first.total_steps)
        )

        assert page.turn_identifier == first.turn_identifier
        assert page.replace is False
        assert page.total_steps == 2
        assert [(step.number, step.kind, step.text) for step in page.steps] == [
            (2, "tool", "pytest -q")
        ]

    def test_an_outdated_cursor_appends_every_missing_step_in_order(self) -> None:
        activity = TurnActivity()
        for index in range(4):
            activity.record_step("ticket-1", _text(f"step {index}"))
        identifier = activity.read_steps("ticket-1", None).turn_identifier
        assert identifier is not None

        page = activity.read_steps("ticket-1", (identifier, 1))

        assert page.replace is False
        assert [step.number for step in page.steps] == [2, 3, 4]

    def test_another_identifier_replaces_the_callers_list(self) -> None:
        activity = TurnActivity()
        activity.record_step("ticket-1", _text("First turn."))
        first = activity.read_steps("ticket-1", None)
        activity.clear("ticket-1")
        activity.record_step("ticket-1", _text("Second turn."))

        page = activity.read_steps("ticket-1", (first.turn_identifier, 1))

        assert page.replace is True
        assert page.turn_identifier != first.turn_identifier
        assert [step.text for step in page.steps] == ["Second turn."]
        assert page.total_steps == 1

    def test_no_cursor_replaces_even_with_a_matching_conversation(self) -> None:
        activity = TurnActivity()
        activity.record_step("ticket-1", _text("Only step."))

        page = activity.read_steps("ticket-1", None)

        assert page.replace is True
        assert page.total_steps == 1


class TestStepWindow:
    def test_only_the_last_steps_are_kept_but_numbers_keep_growing(self) -> None:
        activity = TurnActivity()
        for index in range(_MAX_STEPS_PER_TURN + 5):
            activity.record_step("ticket-1", _text(f"step {index}"))

        page = activity.read_steps("ticket-1", None)

        assert page.total_steps == _MAX_STEPS_PER_TURN + 5
        assert len(page.steps) == _MAX_STEPS_PER_TURN
        assert page.steps[0].number == 6
        assert page.steps[-1].number == _MAX_STEPS_PER_TURN + 5

    def test_a_cursor_inside_the_window_still_appends(self) -> None:
        """Trimming the oldest steps must not turn a valid cursor into a
        replacement: a poller that is at most one window behind keeps its
        list."""
        activity = TurnActivity()
        for index in range(_MAX_STEPS_PER_TURN + 5):
            activity.record_step("ticket-1", _text(f"step {index}"))
        page = activity.read_steps("ticket-1", None)
        assert page.turn_identifier is not None

        later = activity.read_steps(
            "ticket-1", (page.turn_identifier, page.total_steps)
        )

        assert later.replace is False
        assert later.steps == ()

    def test_a_cursor_older_than_the_window_replaces_instead_of_leaving_a_gap(
        self,
    ) -> None:
        """A page that slept through more than 200 steps cannot fill the gap
        by appending, so it must start the list over."""
        activity = TurnActivity()
        for index in range(_MAX_STEPS_PER_TURN + 5):
            activity.record_step("ticket-1", _text(f"step {index}"))
        page = activity.read_steps("ticket-1", None)
        assert page.turn_identifier is not None
        oldest_kept = page.steps[0].number

        just_before_the_window = activity.read_steps(
            "ticket-1", (page.turn_identifier, oldest_kept - 1)
        )
        behind_the_window = activity.read_steps(
            "ticket-1", (page.turn_identifier, oldest_kept - 2)
        )

        # The step just before the oldest kept one is the boundary: the next
        # append fills the list with no gap.
        assert just_before_the_window.replace is False
        assert behind_the_window.replace is True
        assert behind_the_window.steps == page.steps

    def test_a_long_text_step_is_cut_with_a_visible_mark(self) -> None:
        activity = TurnActivity()
        activity.record_step("ticket-1", _text("x" * (_MAX_STEP_TEXT_LENGTH + 50)))

        step = activity.read_steps("ticket-1", None).steps[0]

        assert len(step.text) == _MAX_STEP_TEXT_LENGTH
        assert step.text.endswith("…")

    def test_clear_drops_the_steps_and_the_age(self) -> None:
        activity = TurnActivity()
        activity.record_output("ticket-1")
        activity.record_step("ticket-1", _text("Working."))

        activity.clear("ticket-1")

        assert activity.output_age("ticket-1") is None
        assert activity.read_steps("ticket-1", None).turn_identifier is None


class TestToolStepSummary:
    def test_string_values_are_joined_in_the_inputs_own_order(self) -> None:
        summary = tool_step_summary(
            {
                "command": "git  status",
                "timeout": 30,
                "description": "Check\nthe tree",
            }
        )

        assert summary == "git status, Check the tree"

    def test_an_input_without_strings_gives_an_empty_summary(self) -> None:
        # The page shows the tool's name alone for these; repeating it here
        # would render "read: read".
        assert tool_step_summary({"offset": 0, "limit": 20}) == ""
        assert tool_step_summary(None) == ""

    def test_a_long_summary_is_cut(self) -> None:
        summary = tool_step_summary({"command": "x" * 500})

        assert len(summary) == _MAX_TOOL_SUMMARY_LENGTH
        assert summary.endswith("…")

    def test_one_line_summary_collapses_and_cuts_agent_text(self) -> None:
        assert one_line_summary("Running\nthe tests") == "Running the tests"
        assert len(one_line_summary("x " * 500)) == _MAX_TOOL_SUMMARY_LENGTH
