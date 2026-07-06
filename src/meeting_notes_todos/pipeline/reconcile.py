"""Reconcile stage (build plan §3, step 4).

For each newly extracted item, cheaply find candidate matches in the existing list
(fuzzy title match), then ask the LLM to adjudicate ONLY those candidates: is the
new item NEW, a DUPLICATE of an existing task, or an UPDATE that adds information?
Items with no candidate skip the LLM entirely — input stays flat as the list grows
(cost control, build plan §4.4).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

from rapidfuzz import fuzz

from ..config import PromptsConfig
from ..models import ActionItem, Adjudication
from ..prompts import load_prompt
from ..providers.base import CompletionResult, LLMProvider, Usage

Label = Literal["NEW", "DUPLICATE", "UPDATE"]


@dataclass
class Decision:
    item: ActionItem  # the newly extracted item
    label: Label
    target: ActionItem | None = None  # matched existing item (DUPLICATE / UPDATE)
    reason: str | None = None


@dataclass
class ReconcileRun:
    decisions: list[Decision]
    usage: Usage = field(default_factory=Usage)


def reconcile(
    *,
    provider: LLMProvider,
    prompts: PromptsConfig,
    new_items: list[ActionItem],
    existing_items: list[ActionItem],
    threshold: float = 75.0,
    max_candidates: int = 5,
) -> ReconcileRun:
    """Label each new item NEW / DUPLICATE / UPDATE against the existing list."""
    system_prompt = load_prompt(prompts.dir, prompts.system)
    usage = Usage()
    decisions: list[Decision] = []

    for item in new_items:
        candidates = _candidates(item, existing_items, threshold, max_candidates)
        if not candidates:
            decisions.append(Decision(item=item, label="NEW"))
            continue

        user_prompt = load_prompt(
            prompts.dir,
            prompts.reconcile,
            new_item=_format_item(item),
            candidate_items=_format_candidates(candidates),
        )
        result = provider.complete(
            system_prompt=system_prompt,
            user_content=user_prompt,
            response_schema=Adjudication,
        )
        usage = usage + result.usage
        decisions.append(_decide(item, candidates, result))

    return ReconcileRun(decisions=decisions, usage=usage)


def _candidates(
    item: ActionItem,
    existing_items: list[ActionItem],
    threshold: float,
    max_candidates: int,
) -> list[ActionItem]:
    # deleted items are invisible to reconcile: they can't match (and so can't be
    # updated or resurrected) — a re-mentioned deleted task comes back as NEW
    scored = [
        (fuzz.token_sort_ratio(item.title, other.title), other)
        for other in existing_items
        if other.status != "deleted"
    ]
    scored = [(score, other) for score, other in scored if score >= threshold]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [other for _, other in scored[:max_candidates]]


def _decide(
    item: ActionItem, candidates: list[ActionItem], result: CompletionResult
) -> Decision:
    verdict = _coerce(result)
    if verdict is None:
        # Couldn't read the verdict — safest is to treat it as a new item.
        return Decision(item=item, label="NEW", reason="adjudication unparsed; treated as new")

    if verdict.label in ("DUPLICATE", "UPDATE"):
        target = _target(candidates, verdict.candidate)
        if target is None:  # bad / missing index → fall back to NEW
            return Decision(item=item, label="NEW", reason="no valid candidate; treated as new")
        return Decision(item=item, label=verdict.label, target=target, reason=verdict.reason)

    return Decision(item=item, label="NEW", reason=verdict.reason)


def _target(candidates: list[ActionItem], index: int | None) -> ActionItem | None:
    if index is None or index < 1 or index > len(candidates):
        return None
    return candidates[index - 1]


def _coerce(result: CompletionResult) -> Adjudication | None:
    if isinstance(result.parsed, Adjudication):
        return result.parsed
    text = _strip_code_fences(result.text.strip())
    if not text:
        return None
    try:
        return Adjudication.model_validate(json.loads(text))
    except Exception:
        return None


def _format_item(item: ActionItem) -> str:
    bits = [f"title: {item.title}"]
    if item.owner:
        bits.append(f"owner: {item.owner}")
    if item.due_date:
        bits.append(f"due: {item.due_date.isoformat()}")
    elif item.due_date_text:
        bits.append(f'due phrase: "{item.due_date_text}"')
    if item.description:
        bits.append(f"description: {item.description}")
    return "; ".join(bits)


def _format_candidates(candidates: list[ActionItem]) -> str:
    return "\n".join(
        f"{i}. [{c.status}] {_format_item(c)}" for i, c in enumerate(candidates, 1)
    )


def _strip_code_fences(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.splitlines()[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()
