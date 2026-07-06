"""Local web app for the review UX (build plan §6, §12.1) and management view (v2 M6).

A small FastAPI backend that runs the pipeline and serves a minimal single-page UI
at localhost. ``/api/review`` (extract + reconcile → proposals) and ``/api/commit``
(apply the user's reviewed choices) drive the review flow — nothing is written
until ``/api/commit``. The management view uses ``/api/items/{id}`` (status /
priority) and ``/api/items/reorder`` (manual ordering), which write immediately:
they are direct user edits, not model proposals, so no advisory gate applies.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..chat import ChatOp, apply_chat_ops, run_chat_turn
from ..config import Config, LLMConfig, load_config
from ..models import ActionItem, Priority, Status
from ..pipeline.extract import ExtractionError, extract_action_items
from ..pipeline.reconcile import reconcile
from ..profile import SECTION_CAP, default_profile, load_profile, save_profile, update_section
from ..providers import build_provider, resolve_provider_name
from ..providers.base import LLMProvider
from ..review import Proposal, ReviewedItem, build_proposals, commit_reviewed
from ..store import Store, build_store
from ..usage import record_run

try:  # so `uvicorn meeting_notes_todos.web.app:app` also picks up .env
    from dotenv import load_dotenv

    load_dotenv()
except ModuleNotFoundError:  # pragma: no cover
    pass

_STATIC_DIR = Path(__file__).parent / "static"
_INDEX_HTML = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")

app = FastAPI(title="Meeting Notes → TODOs")
# vendored client libraries (marked, DOMPurify) — served locally so the app works offline
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


# --- dependencies (overridable in tests) ------------------------------------


# v3 M14: the global model switch. One tier selection for the whole app, applied
# to the next turn; None = the config's startup model. In-memory by design — a
# restart falls back to config, and a durable change is a config edit.
_current_tier: str | None = None


def get_config() -> Config:
    return load_config()


def effective_llm(config: Config) -> "LLMConfig":
    """The LLM config with the globally selected tier resolved onto it."""
    resolved = config.llm.model_for_tier(_current_tier)
    if resolved == config.llm.model:
        return config.llm
    return config.llm.model_copy(update={"model": resolved})


def get_provider(config: Config = Depends(get_config)) -> LLMProvider:
    return build_provider(effective_llm(config))


def get_store(config: Config = Depends(get_config)) -> Store:
    return build_store(config.store)


# --- request models ---------------------------------------------------------


class ReviewRequest(BaseModel):
    notes: str
    meeting_date: date | None = None
    meeting_id: str | None = None


class ReviewedItemIn(BaseModel):
    action: Literal["add", "update"]
    title: str
    owner: str | None = None
    due_date_text: str | None = None
    description: str | None = None
    project: str | None = None
    target_id: str | None = None
    id: str | None = None
    source_snippet: str | None = None
    source_meeting_id: str | None = None
    rationale: str | None = None  # passed through from the proposal (M7)


class CommitRequest(BaseModel):
    meeting_date: date | None = None
    items: list[ReviewedItemIn]


class ItemPatch(BaseModel):
    """Partial edit; an explicit ``"priority": null`` clears the priority."""

    status: Status | None = None
    priority: Priority | None = None


class ReorderRequest(BaseModel):
    ids: list[str]  # every current item id, in the desired order


class ChatMessageIn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessageIn]  # in-session history, ending with the new user message


class ChatCommitRequest(BaseModel):
    ops: list[ChatOp]  # only the proposals the user accepted (possibly edited)


class ProfileRequest(BaseModel):
    profile: str


class ModelRequest(BaseModel):
    tier: str


# --- routes -----------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _INDEX_HTML


@app.get("/api/store")
def api_store(store: Store = Depends(get_store)) -> dict:
    return {"items": [item.model_dump(mode="json") for item in store.load()]}


@app.post("/api/review")
def api_review(
    req: ReviewRequest,
    provider: LLMProvider = Depends(get_provider),
    store: Store = Depends(get_store),
    config: Config = Depends(get_config),
) -> dict:
    notes = req.notes.strip()
    if not notes:
        raise HTTPException(status_code=400, detail="notes are empty")
    meeting_date = req.meeting_date or date.today()
    meeting_id = req.meeting_id or f"meeting-{meeting_date.isoformat()}-{uuid4().hex[:8]}"

    existing = store.load()
    try:
        run = extract_action_items(
            provider=provider,
            prompts=config.prompts,
            notes=notes,
            meeting_date=meeting_date,
            meeting_id=meeting_id,
            profile=_load_profile(store),
        )
    except ExtractionError as exc:
        raise HTTPException(status_code=502, detail=f"extraction failed: {exc}")

    rec = reconcile(
        provider=provider,
        prompts=config.prompts,
        new_items=run.items,
        existing_items=existing,
    )
    proposals = build_proposals(rec.decisions)
    usage = run.usage + rec.usage
    llm = effective_llm(config)
    record_run(config.usage.path, command="web-review", provider=resolve_provider_name(llm),
               model=llm.model, usage=usage)
    return {
        "source_notes": notes,
        "meeting_id": meeting_id,
        "meeting_date": meeting_date.isoformat(),
        "proposals": [_proposal_dict(p) for p in proposals],
        "usage": {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens},
    }


@app.post("/api/commit")
def api_commit(req: CommitRequest, store: Store = Depends(get_store)) -> dict:
    meeting_date = req.meeting_date or date.today()
    reviewed = [ReviewedItem(**item.model_dump()) for item in req.items]
    result = commit_reviewed(store.load(), reviewed, meeting_date)
    store.save(result.final)
    return {
        "added": len(result.added),
        "updated": len(result.updated),
        "skipped": [{"title": t, "reason": r} for t, r in result.skipped],
        "items": [item.model_dump(mode="json") for item in result.final],
    }


@app.post("/api/chat")
def api_chat(
    req: ChatRequest,
    provider: LLMProvider = Depends(get_provider),
    store: Store = Depends(get_store),
    config: Config = Depends(get_config),
) -> dict:
    messages = [m.model_dump() for m in req.messages]
    if not messages or messages[-1]["role"] != "user" or not messages[-1]["content"].strip():
        raise HTTPException(status_code=400, detail="last message must be a non-empty user message")

    items = store.load()  # the live list, re-injected every turn (§4.6)
    try:
        turn = run_chat_turn(
            provider=provider,
            prompts=config.prompts,
            items=items,
            messages=messages,
            profile=_load_profile(store),
        )
    except NotImplementedError:
        raise HTTPException(
            status_code=502, detail="the configured provider does not support chat with tools"
        )
    llm = effective_llm(config)
    record_run(config.usage.path, command="web-chat", provider=resolve_provider_name(llm),
               model=llm.model, usage=turn.usage)
    return {
        "message": turn.message,
        "proposals": turn.proposals,
        "dropped": turn.dropped,
        "usage": {"input_tokens": turn.usage.input_tokens, "output_tokens": turn.usage.output_tokens},
    }


@app.post("/api/chat/commit")
def api_chat_commit(req: ChatCommitRequest, store: Store = Depends(get_store)) -> dict:
    item_ops = [op for op in req.ops if op.op != "profile"]
    result = apply_chat_ops(store.load(), item_ops)
    store.save(result.final)
    applied = list(result.applied)
    skipped = list(result.skipped)

    # profile updates bypass the item store: an accepted section body is written
    # into that one section of profile.md (v3 M11); everything else is preserved
    for op in (op for op in req.ops if op.op == "profile"):
        section = (op.section or "").strip()
        new_text = (op.new_text or "").strip()
        store_path = getattr(store, "path", None)
        if not section or not new_text:
            skipped.append(("profile", "a section name and new text are required"))
        elif len(new_text) > SECTION_CAP:
            skipped.append((f'profile: "{section}"',
                            f"section text over the {SECTION_CAP}-char cap — keep it tight"))
        elif store_path is None:
            skipped.append(("profile", "store has no path to keep a profile beside"))
        else:
            update_section(store_path, section, new_text)
            applied.append(f'profile: updated "{section}"')

    return {
        "applied": applied,
        "skipped": [{"summary": s, "reason": r} for s, r in skipped],
        "items": [item.model_dump(mode="json") for item in result.final],
    }


@app.get("/api/model")
def api_get_model(config: Config = Depends(get_config)) -> dict:
    llm = effective_llm(config)
    return {
        "tiers": config.llm.tiers,
        "tier": _current_tier,  # None = the config's startup model
        "model": llm.model,
        "provider": resolve_provider_name(llm),  # the vendor the model string routes to
    }


@app.put("/api/model")
def api_put_model(req: ModelRequest, config: Config = Depends(get_config)) -> dict:
    """Select the global model tier — it drives the next turn, app-wide (v3 §6)."""
    global _current_tier
    if req.tier not in config.llm.tiers:
        raise HTTPException(
            status_code=400,
            detail=f"unknown tier {req.tier!r}; configured tiers: {sorted(config.llm.tiers)}",
        )
    _current_tier = req.tier
    return api_get_model(config)


@app.get("/api/profile")
def api_get_profile(store: Store = Depends(get_store)) -> dict:
    # an empty/missing profile shows the seeded five-section template (v3 §5.1);
    # nothing is written until the user saves or accepts a section update
    return {"profile": _load_profile(store) or default_profile()}


@app.put("/api/profile")
def api_put_profile(req: ProfileRequest, store: Store = Depends(get_store)) -> dict:
    """Direct user edit of the profile — no gate needed, it's the user's own text."""
    store_path = getattr(store, "path", None)
    if store_path is None:
        raise HTTPException(status_code=500, detail="store has no path to keep a profile beside")
    save_profile(store_path, req.profile)
    return {"profile": _load_profile(store) or default_profile()}


def _load_profile(store: Store) -> str | None:
    """The profile.md beside the store, if present (v2 §4.5, §6)."""
    path = getattr(store, "path", None)
    return load_profile(path) if path is not None else None


@app.patch("/api/items/{item_id}")
def api_patch_item(item_id: str, patch: ItemPatch, store: Store = Depends(get_store)) -> dict:
    items = store.load()
    target = next((item for item in items if item.id == item_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="item not found")

    updates: dict = {}
    if patch.status is not None:
        updates["status"] = patch.status
    if "priority" in patch.model_fields_set:  # sent at all — null means clear
        updates["priority"] = patch.priority
    if not updates:
        raise HTTPException(status_code=400, detail="nothing to change")

    updates["updated_at"] = datetime.now(timezone.utc)
    patched = target.model_copy(update=updates)
    store.save([patched if item.id == item_id else item for item in items])
    return {"item": patched.model_dump(mode="json")}


@app.delete("/api/items/{item_id}")
def api_delete_item_permanently(item_id: str, store: Store = Depends(get_store)) -> dict:
    """Hard-remove a row. The UI offers this only from the Deleted view; the normal
    delete path is the soft one (PATCH status=deleted / chat DELETE)."""
    items = store.load()
    remaining = [item for item in items if item.id != item_id]
    if len(remaining) == len(items):
        raise HTTPException(status_code=404, detail="item not found")
    store.save(remaining)
    return {"items": [item.model_dump(mode="json") for item in remaining]}


@app.post("/api/items/reorder")
def api_reorder_items(req: ReorderRequest, store: Store = Depends(get_store)) -> dict:
    items = store.load()
    by_id = {item.id: item for item in items}
    if sorted(req.ids) != sorted(by_id):
        raise HTTPException(
            status_code=400,
            detail="ids must be exactly the current item ids — reload the list and retry",
        )
    reordered = [
        by_id[item_id].model_copy(update={"position": pos})
        for pos, item_id in enumerate(req.ids)
    ]
    store.save(reordered)
    return {"items": [item.model_dump(mode="json") for item in reordered]}


def _proposal_dict(p: Proposal) -> dict:
    return {
        "kind": p.kind,
        "id": p.item.id,
        "title": p.item.title,
        "owner": p.item.owner,
        "due_date_text": p.item.due_date_text,
        "due_date": p.item.due_date.isoformat() if p.item.due_date else None,
        "description": p.item.description,
        "source_snippet": p.item.source_snippet,
        "source_meeting_id": p.item.source_meeting_id,
        "target_id": p.target.id if p.target else None,
        "target": _item_brief(p.target) if p.target else None,
        "changes": p.changes,
        "reason": p.reason,
        "rationale": p.rationale,
    }


def _item_brief(item: ActionItem) -> dict:
    return {
        "title": item.title,
        "owner": item.owner,
        "status": item.status,
        "due_date": item.due_date.isoformat() if item.due_date else None,
        "due_date_text": item.due_date_text,
    }
