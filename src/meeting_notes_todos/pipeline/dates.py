"""Deterministic relative-date resolution (build plan §3, Validate stage).

The model only emits the deadline *phrase* (``due_date_text``); we resolve it to
an absolute date here, in code, rather than trusting the LLM to compute weekdays.

Convention: resolve to the **nearest upcoming occurrence** on or after the meeting
date. "by Friday", "next Friday", and "this Friday" therefore all resolve to the
next Friday. Truly vague phrases ("this week", "soon", "next sprint") and anything
unparseable return ``None`` — we never guess.
"""

from __future__ import annotations

import re
from datetime import date, datetime

import dateparser
from dateparser.search import search_dates

# Leading words that introduce a deadline but aren't part of the date itself.
_LEADING_FILLER = (
    "no later than",
    "not later than",
    "due by",
    "due on",
    "by",
    "before",
    "due",
    "on",
    "until",
    "around",
)

# Qualifiers we drop so "next/this/coming Friday" == "Friday" (nearest upcoming).
_NEAREST_UPCOMING = ("next", "this", "coming", "upcoming")

_VAGUE_EXACT = {"soon", "later", "sometime", "whenever", "tbd", "asap", "eventually"}

# PREFER_DATES_FROM=future makes a bare weekday resolve to the upcoming one.
_SETTINGS = {"PREFER_DATES_FROM": "future"}


def resolve_due_date(phrase: str | None, meeting_date: date) -> date | None:
    """Resolve a deadline phrase to an absolute date, or ``None`` if vague/unparseable."""
    if not phrase or not phrase.strip():
        return None

    text = phrase.strip().lower().rstrip(".")
    text = _strip_leading_filler(text)
    if not text or _is_vague(text):
        return None
    text = _strip_nearest_upcoming(text)

    base = datetime(meeting_date.year, meeting_date.month, meeting_date.day)
    settings = {**_SETTINGS, "RELATIVE_BASE": base}

    parsed = dateparser.parse(text, languages=["en"], settings=settings)
    if parsed is None and " " in text:
        # Phrase carried extra words (e.g. "tuesday's review"); pull a date out of it.
        parsed = _search(text, settings)
    return parsed.date() if parsed is not None else None


def _strip_leading_filler(text: str) -> str:
    for filler in _LEADING_FILLER:
        if text == filler:
            return ""
        if text.startswith(filler + " "):
            return text[len(filler) + 1 :].strip()
    return text


def _strip_nearest_upcoming(text: str) -> str:
    first, _, rest = text.partition(" ")
    if first in _NEAREST_UPCOMING and rest:
        return rest.strip()
    return text


def _is_vague(text: str) -> bool:
    if text in _VAGUE_EXACT:
        return True
    if re.search(r"\b(soon|later|sometime|whenever|tbd|asap|eventually)\b", text):
        return True
    # "this/next/coming/early/mid/late <week|month|quarter|sprint|year>"
    if re.search(
        r"\b(this|next|coming|upcoming|early|mid|late)\s+(week|month|quarter|sprint|year)\b",
        text,
    ):
        return True
    if re.search(r"\bend of\b", text) or re.search(r"\b(eow|eom|eoq)\b", text):
        return True
    return False


def _search(text: str, settings: dict):
    try:
        found = search_dates(text, languages=["en"], settings=settings)
    except Exception:  # search_dates can be finicky on odd input
        return None
    return found[0][1] if found else None
