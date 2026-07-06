"""Deterministic date-resolution tests (key-free)."""

from __future__ import annotations

from datetime import date

import pytest

from meeting_notes_todos.pipeline.dates import resolve_due_date

# Sunday. June 29 = Mon, June 30 = Tue, July 3 = Fri.
MEETING = date(2026, 6, 28)


@pytest.mark.parametrize(
    "phrase, expected",
    [
        ("by Friday", date(2026, 7, 3)),
        ("before next Tuesday", date(2026, 6, 30)),
        ("tomorrow", date(2026, 6, 29)),
        ("this week", None),
        ("", None),
        ("   ", None),
        (None, None),
        ("soon", None),
        ("sometime next sprint", None),
        ("blah blah", None),
    ],
)
def test_resolve_due_date(phrase, expected):
    assert resolve_due_date(phrase, MEETING) == expected


def test_resolved_dates_are_on_or_after_the_meeting_date():
    # "nearest upcoming occurrence" — never resolves into the past
    for phrase in ("Friday", "Monday", "next Wednesday"):
        resolved = resolve_due_date(phrase, MEETING)
        assert resolved is not None and resolved >= MEETING
