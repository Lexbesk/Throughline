"""Local web app for the review UX (build plan §6, §12.1) and management view (v2 M6).

A small FastAPI backend that runs the pipeline and serves a minimal single-page UI
at localhost. ``/api/review`` (extract + reconcile → proposals) and ``/api/commit``
(apply the user's reviewed choices) drive the review flow — nothing is written
until ``/api/commit``. The management view uses ``/api/items/{id}`` (status /
priority) and ``/api/items/reorder`` (manual ordering), which write immediately:
they are direct user edits, not model proposals, so no advisory gate applies.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..auth import (
    COOKIE_NAME,
    SESSION_TTL_DAYS,
    User,
    authenticate,
    create_session,
    delete_session,
    delete_user_sessions,
    session_user,
    set_password,
)
from ..backends import build_backends
from ..chat import ChatOp, apply_chat_ops, run_chat_turn
from ..config import Config, LLMConfig, load_config
from ..keys import KEY_PROVIDERS, ApiKeyStore, get_cipher
from ..models import ActionItem, Priority, Status
from ..pipeline.extract import ExtractionError, extract_action_items
from ..pipeline.reconcile import reconcile
from ..profile import (
    SECTION_CAP,
    FileProfile,
    PostgresProfile,
    ProfileBackend,
    default_profile,
)
from ..providers import build_provider, resolve_provider_name
from ..providers.base import LLMProvider
from ..review import Proposal, ReviewedItem, build_proposals, commit_reviewed
from ..store import Store
from ..store.postgres_store import PostgresStore
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


# v3 M14 / v4 M17: the model switch. In local (markdown) mode there is one
# global selection; in hosted (postgres) mode the selection is per-user. Both
# are in-memory by design — a restart falls back to config, and a durable
# change is a config edit.
_current_tier: str | None = None
_tier_by_user: dict[str, str] = {}


def get_config() -> Config:
    return load_config()


def _auth_enabled(config: Config) -> bool:
    # multi-user backend ⇒ login required; local files ⇒ single-user, no auth
    return config.store.backend == "postgres"


def _secure_cookies() -> bool:
    # off for localhost dev over http; M19 sets THROUGHLINE_SECURE_COOKIES=1 in prod
    return os.environ.get("THROUGHLINE_SECURE_COOKIES", "") not in ("", "0", "false")


def get_current_user(request: Request, config: Config = Depends(get_config)) -> User | None:
    """The logged-in user (postgres mode) or None (local single-user mode).

    In postgres mode a missing/invalid session is a 401 — this dependency is
    what makes every data route require login (v4 §5).
    """
    if not _auth_enabled(config):
        return None
    from ..db import get_pool

    token = request.cookies.get(COOKIE_NAME)
    if token:
        user = session_user(get_pool(), token)
        if user is not None:
            return user
    raise HTTPException(status_code=401, detail="login required")


def _tier_for(user: User | None) -> str | None:
    return _tier_by_user.get(user.id) if user else _current_tier


def effective_llm(config: Config, tier: str | None) -> "LLMConfig":
    """The LLM config with the selected tier resolved onto it."""
    resolved = config.llm.model_for_tier(tier)
    if resolved == config.llm.model:
        return config.llm
    return config.llm.model_copy(update={"model": resolved})


def get_store(
    config: Config = Depends(get_config),
    user: User | None = Depends(get_current_user),
) -> Store:
    if _auth_enabled(config):
        from ..db import get_pool

        return PostgresStore(get_pool(), user.id)  # scoped to the session user
    return build_backends(config.store)[0]


def get_profile_backend(store: Store = Depends(get_store)) -> ProfileBackend:
    """The profile paired with the store's backend (and, for postgres, the same
    user). Deriving from the store keeps a test's ``get_store`` override
    consistent for both."""
    if isinstance(store, PostgresStore):
        return PostgresProfile(store.pool, store.user_id)
    path = getattr(store, "path", None)
    if path is None:
        raise HTTPException(status_code=500, detail="store has no profile backend")
    return FileProfile(path)


def _key_store(store: Store) -> ApiKeyStore:
    """Per-user key store for the session user (postgres mode). Surfaces a clean
    500 when the master encryption key isn't configured, rather than a traceback."""
    try:
        cipher = get_cipher()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return ApiKeyStore(store.pool, store.user_id, cipher)


def get_provider(
    config: Config = Depends(get_config),
    user: User | None = Depends(get_current_user),
    store: Store = Depends(get_store),
) -> LLMProvider:
    llm = effective_llm(config, _tier_for(user))
    if not _auth_enabled(config):
        return build_provider(llm)  # local single-user mode: keys from the env

    # hosted mode: run on the requesting user's stored key for the selected
    # provider — never the env, never another user's key (v4 §7)
    provider_name = resolve_provider_name(llm)
    if provider_name not in KEY_PROVIDERS:  # e.g. a local endpoint
        return build_provider(llm)
    api_key = _key_store(store).get_key(provider_name)
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail=f"No {provider_name} API key set for your account. "
            'Add one under "Keys" to use this model.',
        )
    return build_provider(llm, api_key=api_key)


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


class LoginRequest(BaseModel):
    username: str
    password: str


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


class ApiKeyRequest(BaseModel):
    api_key: str


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
    profile: ProfileBackend = Depends(get_profile_backend),
    config: Config = Depends(get_config),
    user: User | None = Depends(get_current_user),
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
            profile=profile.load(),
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
    llm = effective_llm(config, _tier_for(user))
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
    profile: ProfileBackend = Depends(get_profile_backend),
    config: Config = Depends(get_config),
    user: User | None = Depends(get_current_user),
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
            profile=profile.load(),
        )
    except NotImplementedError:
        raise HTTPException(
            status_code=502, detail="the configured provider does not support chat with tools"
        )
    llm = effective_llm(config, _tier_for(user))
    record_run(config.usage.path, command="web-chat", provider=resolve_provider_name(llm),
               model=llm.model, usage=turn.usage)
    return {
        "message": turn.message,
        "proposals": turn.proposals,
        "dropped": turn.dropped,
        "usage": {"input_tokens": turn.usage.input_tokens, "output_tokens": turn.usage.output_tokens},
    }


@app.post("/api/chat/commit")
def api_chat_commit(
    req: ChatCommitRequest,
    store: Store = Depends(get_store),
    profile: ProfileBackend = Depends(get_profile_backend),
) -> dict:
    item_ops = [op for op in req.ops if op.op != "profile"]
    result = apply_chat_ops(store.load(), item_ops)
    store.save(result.final)
    applied = list(result.applied)
    skipped = list(result.skipped)

    # profile updates bypass the item store: an accepted section body is written
    # into that one section of the profile (v3 M11); everything else is preserved
    for op in (op for op in req.ops if op.op == "profile"):
        section = (op.section or "").strip()
        new_text = (op.new_text or "").strip()
        if not section or not new_text:
            skipped.append(("profile", "a section name and new text are required"))
        elif len(new_text) > SECTION_CAP:
            skipped.append((f'profile: "{section}"',
                            f"section text over the {SECTION_CAP}-char cap — keep it tight"))
        else:
            profile.update_section(section, new_text)
            applied.append(f'profile: updated "{section}"')

    return {
        "applied": applied,
        "skipped": [{"summary": s, "reason": r} for s, r in skipped],
        "items": [item.model_dump(mode="json") for item in result.final],
    }


# --- accounts & sessions (v4 M17; login-only, no signup path exists) ---------


@app.get("/api/me")
def api_me(request: Request, config: Config = Depends(get_config)) -> dict:
    """Login state for the UI — never a 401, it *reports* rather than gates."""
    if not _auth_enabled(config):
        return {"auth_required": False, "username": None}
    from ..db import get_pool

    token = request.cookies.get(COOKIE_NAME)
    user = session_user(get_pool(), token) if token else None
    return {"auth_required": True, "username": user.username if user else None}


@app.post("/api/login")
def api_login(
    req: LoginRequest, response: Response, config: Config = Depends(get_config)
) -> dict:
    if not _auth_enabled(config):
        raise HTTPException(status_code=400, detail="login is not used in local mode")
    from ..db import get_pool

    pool = get_pool()
    user = authenticate(pool, req.username, req.password)
    if user is None:
        raise HTTPException(status_code=401, detail="invalid username or password")
    token = create_session(pool, user.id)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_TTL_DAYS * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=_secure_cookies(),
        path="/",
    )
    return {"username": user.username}


@app.post("/api/logout")
def api_logout(
    request: Request, response: Response, config: Config = Depends(get_config)
) -> dict:
    if _auth_enabled(config):
        from ..db import get_pool

        token = request.cookies.get(COOKIE_NAME)
        if token:
            delete_session(get_pool(), token)  # revoked server-side, not just forgotten
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@app.post("/api/password")
def api_change_password(
    req: PasswordChangeRequest,
    response: Response,
    user: User | None = Depends(get_current_user),
) -> dict:
    """Change one's own password (e.g. after first login); revokes all sessions."""
    if user is None:
        raise HTTPException(status_code=400, detail="no accounts in local mode")
    if len(req.new_password) < 8:
        raise HTTPException(status_code=400, detail="new password must be at least 8 characters")
    from ..db import get_pool

    pool = get_pool()
    if authenticate(pool, user.username, req.current_password) is None:
        raise HTTPException(status_code=403, detail="current password is incorrect")
    set_password(pool, user.username, req.new_password)
    delete_user_sessions(pool, user.id)  # every old session dies with the old password
    token = create_session(pool, user.id)  # …but this browser stays logged in
    response.set_cookie(
        COOKIE_NAME, token, max_age=SESSION_TTL_DAYS * 24 * 3600,
        httponly=True, samesite="lax", secure=_secure_cookies(), path="/",
    )
    return {"ok": True}


# --- per-user API keys (v4 M18; encrypted at rest, never returned in the clear) ---


def get_key_store(
    config: Config = Depends(get_config),
    store: Store = Depends(get_store),
) -> ApiKeyStore:
    """Require hosted mode; the store dependency already enforced the session."""
    if not _auth_enabled(config):
        raise HTTPException(status_code=400, detail="API keys are not used in local mode")
    return _key_store(store)


@app.get("/api/keys")
def api_list_keys(keys: ApiKeyStore = Depends(get_key_store)) -> dict:
    # masked only — provider, last 4 chars, timestamp; never the key itself
    return {"providers": list(KEY_PROVIDERS), "keys": keys.list_keys()}


@app.put("/api/keys/{provider}")
def api_set_key(
    provider: str, req: ApiKeyRequest, keys: ApiKeyStore = Depends(get_key_store)
) -> dict:
    try:
        keys.set_key(provider, req.api_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # respond with the masked view only — the plaintext is never echoed back
    entry = next((k for k in keys.list_keys() if k["provider"] == provider), None)
    return {"provider": provider, "last4": entry["last4"] if entry else None}


@app.delete("/api/keys/{provider}")
def api_delete_key(provider: str, keys: ApiKeyStore = Depends(get_key_store)) -> dict:
    return {"removed": keys.delete_key(provider)}


@app.get("/api/model")
def api_get_model(
    config: Config = Depends(get_config),
    user: User | None = Depends(get_current_user),
) -> dict:
    tier = _tier_for(user)
    llm = effective_llm(config, tier)
    return {
        "tiers": config.llm.tiers,
        "tier": tier,  # None = the config's startup model
        "model": llm.model,
        "provider": resolve_provider_name(llm),  # the vendor the model string routes to
    }


@app.put("/api/model")
def api_put_model(
    req: ModelRequest,
    config: Config = Depends(get_config),
    user: User | None = Depends(get_current_user),
) -> dict:
    """Select the model tier for the next turn — per-user when logged in (v4 M17),
    app-wide in local single-user mode (v3 §6)."""
    global _current_tier
    if req.tier not in config.llm.tiers:
        raise HTTPException(
            status_code=400,
            detail=f"unknown tier {req.tier!r}; configured tiers: {sorted(config.llm.tiers)}",
        )
    if user is not None:
        _tier_by_user[user.id] = req.tier
    else:
        _current_tier = req.tier
    return api_get_model(config, user)


@app.get("/api/profile")
def api_get_profile(profile: ProfileBackend = Depends(get_profile_backend)) -> dict:
    # an empty/missing profile shows the seeded five-section template (v3 §5.1);
    # nothing is written until the user saves or accepts a section update
    return {"profile": profile.load() or default_profile()}


@app.put("/api/profile")
def api_put_profile(
    req: ProfileRequest, profile: ProfileBackend = Depends(get_profile_backend)
) -> dict:
    """Direct user edit of the profile — no gate needed, it's the user's own text."""
    profile.save(req.profile)
    return {"profile": profile.load() or default_profile()}


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
