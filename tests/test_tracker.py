"""Tests for the tracker protocol utilities."""

from __future__ import annotations

from symphony_linear.tracker import is_bot_comment


class TestIsBotComment:
    def test_positive_workspace_kind(self) -> None:
        """A body containing the '*Symphony · workspace*' footer returns True."""
        assert is_bot_comment("Some work done\n\n*Symphony · workspace*") is True

    def test_positive_context_tokens_variant(self) -> None:
        """A body containing the '*Symphony · context: ... tokens*' footer returns True."""
        assert is_bot_comment("Done.\n\n*Symphony · context: 37,074 tokens*") is True

    def test_negative_plain_human_comment(self) -> None:
        """A plain human comment with no marker returns False."""
        assert is_bot_comment("This is a regular human comment.") is False

    def test_negative_partial_match_no_dot(self) -> None:
        """A body containing 'Symphony' without the middle dot returns False."""
        assert is_bot_comment("I like Symphony music") is False

    def test_negative_only_marker_substring(self) -> None:
        """Only the '*Symphony · ' substring without the closing '*' returns True."""
        # The marker is '*Symphony · ', trailing content does not matter.
        assert is_bot_comment("Foo\n\n*Symphony · ") is True

    def test_negative_empty_body(self) -> None:
        """An empty string returns False."""
        assert is_bot_comment("") is False

    def test_negative_none_raises(self) -> None:
        """None raises TypeError (since str methods cannot be called on None)."""
        import pytest

        with pytest.raises(TypeError):
            is_bot_comment(None)  # type: ignore[arg-type]
