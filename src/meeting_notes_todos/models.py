"""Pydantic data models (build plan §5)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

Status = Literal["todo", "doing", "done", "cancelled", "deleted"]
# cancelled = a task we decided not to do; deleted = soft-removed from the list/view
# (kept in the store, reversible). Both are terminal for merge/chat purposes.
Priority = Literal["high", "medium", "low"]


class ExtractedItem(BaseModel):
    """One action item as emitted by the LLM (the extraction output shape).

    The model copies the deadline phrase verbatim into ``due_date_text``; it does
    not compute dates. The pipeline enriches each item into a stored ActionItem.
    """

    title: str
    description: str | None = None
    owner: str | None = None
    due_date_text: str | None = None  # deadline phrase as written (e.g. "by Friday")
    source_snippet: str
    rationale: str | None = None  # one short sentence: why this was extracted/kept (v2 M7)


class ExtractionResponse(BaseModel):
    """Top-level structured-output wrapper: a list of extracted items."""

    items: list[ExtractedItem]


class Adjudication(BaseModel):
    """LLM verdict for one new item vs. its candidate matches (build plan §3.4)."""

    label: Literal["NEW", "DUPLICATE", "UPDATE"]
    candidate: int | None = None  # 1-based index into the presented candidates
    reason: str | None = None


class ActionItem(BaseModel):
    """The unit extracted and stored (build plan §5)."""

    id: str
    title: str
    description: str | None = None
    status: Status = "todo"
    priority: Priority | None = None  # None = unprioritized (v2 M6)
    position: int | None = None  # manual sort key; stamped on reorder (v2 M6)
    owner: str | None = None
    due_date_text: str | None = None  # raw phrase; display fallback when due_date is None
    due_date: date | None = None  # resolved deterministically in code from due_date_text
    tags: list[str] = Field(default_factory=list)
    project: str | None = None
    rationale: str | None = None  # the model's latest *stated* rationale, not a trace (v2 M7)
    source_meeting_id: str
    source_snippet: str
    created_at: datetime
    updated_at: datetime
