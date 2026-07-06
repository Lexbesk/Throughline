"""Extract + Validate pipeline stages (build plan §3, steps 2-3).

One LLM call turns notes into a JSON list of action items; the result is parsed,
validated against the schema, and on failure repaired with exactly one retry that
feeds the error back. Validated drafts are then enriched into stored ActionItems:
due-date phrases are resolved deterministically in code, and within-batch
duplicates are collapsed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from uuid import uuid4

from pydantic import ValidationError

from ..config import PromptsConfig
from ..models import ActionItem, ExtractedItem, ExtractionResponse
from ..profile import NO_PROFILE
from ..prompts import load_prompt
from ..providers.base import CompletionResult, LLMProvider, Usage
from .dates import resolve_due_date


class ExtractionError(RuntimeError):
    """Raised when model output can't be parsed/validated, even after one repair."""


@dataclass
class ExtractionRun:
    items: list[ActionItem]
    usage: Usage


def extract_action_items(
    *,
    provider: LLMProvider,
    prompts: PromptsConfig,
    notes: str,
    meeting_date: date,
    meeting_id: str,
    profile: str | None = None,
) -> ExtractionRun:
    """Notes -> validated ``ActionItem`` list, with one repair retry on bad output."""
    system_prompt = load_prompt(prompts.dir, prompts.system)
    user_prompt = load_prompt(
        prompts.dir,
        prompts.extract,
        notes=notes,
        meeting_date=meeting_date.isoformat(),
        profile=(profile or "").strip() or NO_PROFILE,  # user context (v2 M9)
    )

    usage = Usage()

    # Stage 2 (Extract): one LLM call, preferring native structured output.
    result = provider.complete(
        system_prompt=system_prompt,
        user_content=user_prompt,
        response_schema=ExtractionResponse,
    )
    usage = usage + result.usage
    response, error = _coerce(result)

    # Stage 3 (Validate): on malformed output, exactly one repair retry that feeds
    # the error back; then surface a clear error rather than silently dropping data.
    if response is None:
        repair_prompt = _repair_prompt(user_prompt, result.text, error or "unknown error")
        result = provider.complete(system_prompt=system_prompt, user_content=repair_prompt)
        usage = usage + result.usage
        response, error = _coerce(result)
        if response is None:
            raise ExtractionError(
                f"Could not parse a valid action-item list after one repair attempt: {error}"
            )

    items = [_to_action_item(item, meeting_id, meeting_date) for item in response.items]
    items = _dedupe_within_batch(items)
    return ExtractionRun(items=items, usage=usage)


def _coerce(result: CompletionResult) -> tuple[ExtractionResponse | None, str | None]:
    """Turn a completion into a validated ``ExtractionResponse``, or ``(None, error)``."""
    if isinstance(result.parsed, ExtractionResponse):
        return result.parsed, None

    text = _strip_code_fences(result.text.strip())
    if not text:
        return None, "empty response"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc}"
    try:
        return ExtractionResponse.model_validate(data), None
    except ValidationError as exc:
        return None, f"schema validation failed: {exc}"


def _strip_code_fences(text: str) -> str:
    """Drop a leading ```/```json fence and a trailing ``` if the model added them."""
    if not text.startswith("```"):
        return text
    lines = text.splitlines()[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _repair_prompt(original_user: str, bad_output: str, error: str) -> str:
    return (
        f"{original_user}\n\n"
        "---\n"
        "Your previous response could not be parsed as the required JSON.\n"
        f"Error: {error}\n\n"
        "Previous response:\n"
        f"{bad_output}\n\n"
        'Return ONLY a corrected JSON object of the form {"items": [ ... ]} — '
        "no prose, no markdown, no code fences."
    )


def _to_action_item(item: ExtractedItem, meeting_id: str, meeting_date: date) -> ActionItem:
    now = datetime.now(timezone.utc)
    due_text = _clean(item.due_date_text)
    return ActionItem(
        id=uuid4().hex,
        title=item.title.strip(),
        description=_clean(item.description),
        status="todo",
        owner=_clean(item.owner),
        due_date_text=due_text,
        due_date=resolve_due_date(due_text, meeting_date),
        rationale=_clean(item.rationale),
        source_meeting_id=meeting_id,
        source_snippet=item.source_snippet.strip(),
        created_at=now,
        updated_at=now,
    )


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _dedupe_within_batch(items: list[ActionItem]) -> list[ActionItem]:
    """Collapse items that are clearly the same within this one batch (build plan §3)."""
    seen: set[str] = set()
    deduped: list[ActionItem] = []
    for item in items:
        key = _normalize_title(item.title)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _normalize_title(title: str) -> str:
    return " ".join(title.lower().split()).rstrip(".!:;,")
