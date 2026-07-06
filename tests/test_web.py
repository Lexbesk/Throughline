"""Web endpoint tests (via FastAPI TestClient, with a fake provider + temp store)."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from meeting_notes_todos.models import ActionItem, ExtractedItem, ExtractionResponse
from meeting_notes_todos.providers.base import (
    ChatResult,
    CompletionResult,
    LLMProvider,
    ToolCall,
    Usage,
)
from meeting_notes_todos.store import MarkdownStore
from meeting_notes_todos.web.app import app, get_provider, get_store

_TS = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)


def _stored_item(item_id: str, title: str, **overrides) -> ActionItem:
    base = dict(
        id=item_id,
        title=title,
        source_meeting_id="m1",
        source_snippet=title,
        created_at=_TS,
        updated_at=_TS,
    )
    base.update(overrides)
    return ActionItem(**base)


class _FakeProvider(LLMProvider):
    def __init__(self, items: list[ExtractedItem]) -> None:
        self._items = items
        self.calls: list[dict] = []

    def complete(self, *, system_prompt, user_content, max_tokens=None, response_schema=None):
        # extract call → an ExtractionResponse; reconcile isn't reached (empty store)
        self.calls.append({"system": system_prompt, "user": user_content})
        return CompletionResult(text="", usage=Usage(5, 2), parsed=ExtractionResponse(items=self._items))


def _setup(tmp_path, items):
    store = MarkdownStore(tmp_path / "todos.md")
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_provider] = lambda: _FakeProvider(items)
    return TestClient(app), store


def test_index_is_served():
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "<html" in resp.text.lower() and "Throughline" in resp.text


def test_review_then_commit_writes_only_accepted_items(tmp_path):
    items = [
        ExtractedItem(title="Send the report", owner="Lei", due_date_text="by Friday", source_snippet="a"),
        ExtractedItem(title="Update the docs", source_snippet="b"),
    ]
    client, store = _setup(tmp_path, items)
    try:
        assert client.get("/api/store").json()["items"] == []

        review = client.post("/api/review", json={"notes": "n", "meeting_date": "2026-07-01"})
        assert review.status_code == 200
        proposals = review.json()["proposals"]
        assert [p["kind"] for p in proposals] == ["new", "new"]
        assert proposals[0]["due_date"] == "2026-07-03"  # resolved in code

        # accept the first, reject the second, add a hand-written one
        commit = client.post(
            "/api/commit",
            json={
                "meeting_date": "2026-07-01",
                "items": [
                    {
                        "action": "add",
                        "title": "Send the report",
                        "owner": "Lei",
                        "due_date_text": "by Friday",
                        "id": proposals[0]["id"],
                        "source_snippet": "a",
                    },
                    {"action": "add", "title": "Ping the vendor"},
                ],
            },
        )
        assert commit.status_code == 200
        assert commit.json()["added"] == 2
        assert [i.title for i in store.load()] == ["Send the report", "Ping the vendor"]
    finally:
        app.dependency_overrides.clear()


def test_empty_notes_are_rejected(tmp_path):
    client, _ = _setup(tmp_path, [])
    try:
        assert client.post("/api/review", json={"notes": "   "}).status_code == 400
    finally:
        app.dependency_overrides.clear()


# --- management view (M6) -----------------------------------------------------


def test_mark_done_persists_and_merge_wont_resurrect(tmp_path):
    client, store = _setup(tmp_path, [])
    try:
        store.save([_stored_item("a", "Send the Q3 deck", owner="Lei")])

        resp = client.patch("/api/items/a", json={"status": "done"})
        assert resp.status_code == 200
        assert resp.json()["item"]["status"] == "done"
        assert store.load()[0].status == "done"  # persisted

        # a later review that targets the done item must not touch it
        commit = client.post(
            "/api/commit",
            json={
                "items": [
                    {"action": "update", "title": "Send the Q3 deck v2", "target_id": "a"}
                ]
            },
        )
        assert commit.status_code == 200
        assert commit.json()["updated"] == 0
        assert "done" in commit.json()["skipped"][0]["reason"]
        (item,) = store.load()
        assert item.status == "done" and item.title == "Send the Q3 deck"
    finally:
        app.dependency_overrides.clear()


def test_priority_set_clear_and_bad_patches(tmp_path):
    client, store = _setup(tmp_path, [])
    try:
        store.save([_stored_item("a", "Task")])

        resp = client.patch("/api/items/a", json={"priority": "high"})
        assert resp.status_code == 200
        assert store.load()[0].priority == "high"
        assert "!high" in store.path.read_text()  # visible in the markdown line

        resp = client.patch("/api/items/a", json={"priority": None})  # explicit null clears
        assert resp.status_code == 200
        assert store.load()[0].priority is None

        assert client.patch("/api/items/a", json={}).status_code == 400
        assert client.patch("/api/items/nope", json={"status": "done"}).status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_reorder_persists_and_stamps_positions(tmp_path):
    client, store = _setup(tmp_path, [])
    try:
        store.save([_stored_item(i, i.upper()) for i in ("a", "b", "c")])

        resp = client.post("/api/items/reorder", json={"ids": ["c", "a", "b"]})
        assert resp.status_code == 200
        items = store.load()
        assert [it.id for it in items] == ["c", "a", "b"]  # persisted order
        assert [it.position for it in items] == [0, 1, 2]

        # not a permutation of the current ids → rejected, order unchanged
        for bad in (["c", "a"], ["c", "a", "x"], ["c", "a", "a"]):
            assert client.post("/api/items/reorder", json={"ids": bad}).status_code == 400
        assert [it.id for it in store.load()] == ["c", "a", "b"]
    finally:
        app.dependency_overrides.clear()


def test_soft_delete_restore_and_permanent_delete(tmp_path):
    client, store = _setup(tmp_path, [])
    try:
        store.save([_stored_item("a", "Keep me"), _stored_item("b", "Bin me")])

        # soft delete: the row stays in the store, glyph flips to ~
        resp = client.patch("/api/items/b", json={"status": "deleted"})
        assert resp.status_code == 200
        assert [(i.id, i.status) for i in store.load()] == [("a", "todo"), ("b", "deleted")]
        assert "- [~] Bin me" in store.path.read_text()

        # restore: back to todo (soft delete is reversible)
        assert client.patch("/api/items/b", json={"status": "todo"}).status_code == 200
        assert store.load()[1].status == "todo"

        # permanent delete actually removes the row
        client.patch("/api/items/b", json={"status": "deleted"})
        resp = client.delete("/api/items/b")
        assert resp.status_code == 200
        assert [i.id for i in store.load()] == ["a"]
        assert client.delete("/api/items/b").status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_review_returns_rationale_and_commit_persists_it(tmp_path):
    items = [ExtractedItem(title="Send the report", source_snippet="s",
                           rationale="Explicit commitment in the notes.")]
    client, store = _setup(tmp_path, items)
    try:
        review = client.post("/api/review", json={"notes": "n", "meeting_date": "2026-07-01"})
        (proposal,) = review.json()["proposals"]
        assert proposal["rationale"] == "Explicit commitment in the notes."  # shown in the panel

        commit = client.post("/api/commit", json={
            "meeting_date": "2026-07-01",
            "items": [{"action": "add", "title": "Send the report",
                       "id": proposal["id"], "rationale": proposal["rationale"]}],
        })
        assert commit.status_code == 200
        assert store.load()[0].rationale == "Explicit commitment in the notes."  # persisted
    finally:
        app.dependency_overrides.clear()


def test_profile_get_put_roundtrip_and_seeded_template(tmp_path):
    client, store = _setup(tmp_path, [])
    try:
        # no profile yet → the seeded five-section template, without writing a file
        seeded = client.get("/api/profile").json()["profile"]
        for name in ("Long-term goals", "Current focus", "Priorities & values",
                     "Working style & personality", "Context & constraints"):
            assert f"## {name}" in seeded
        assert not (tmp_path / "profile.md").exists()

        resp = client.put("/api/profile", json={"profile": "Role: eval lead."})
        assert resp.status_code == 200 and resp.json()["profile"] == "Role: eval lead."
        assert (tmp_path / "profile.md").read_text().strip() == "Role: eval lead."

        client.put("/api/profile", json={"profile": ""})  # user clears it → template again
        assert "## Long-term goals" in client.get("/api/profile").json()["profile"]
    finally:
        app.dependency_overrides.clear()


def test_review_injects_the_saved_profile_into_extraction(tmp_path):
    provider = _FakeProvider([])
    store = MarkdownStore(tmp_path / "todos.md")
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_provider] = lambda: provider
    client = TestClient(app)
    try:
        client.put("/api/profile", json={"profile": "Name: Austin. Project: Atlas."})
        assert client.post("/api/review", json={"notes": "n"}).status_code == 200
        assert "Name: Austin. Project: Atlas." in provider.calls[0]["user"]
    finally:
        app.dependency_overrides.clear()


# --- global model switch (v3 M14) --------------------------------------------


def test_model_tier_resolution_is_config_driven():
    from meeting_notes_todos.config import LLMConfig

    llm = LLMConfig()
    assert llm.model_for_tier(None) == llm.model  # no tier → startup model
    assert llm.model_for_tier("Sonnet 5") == "claude-sonnet-5"
    assert llm.model_for_tier("Fable 5") == "claude-fable-5"  # the Mythos-class tier
    assert llm.model_for_tier("GPT 5.5") == "gpt-5.5"  # GPT tiers in the same map
    assert llm.model_for_tier("nope") == llm.model  # unknown → safe fallback

    # swapping the lineup is a config edit, not code (M14 acceptance)
    custom = LLMConfig(tiers={"turbo": "my-model-x"})
    assert custom.model_for_tier("turbo") == "my-model-x"


def test_model_switch_endpoint_drives_the_next_provider():
    import meeting_notes_todos.web.app as web_app
    from meeting_notes_todos.config import Config
    from meeting_notes_todos.providers.anthropic_provider import AnthropicProvider

    client = TestClient(app)  # no overrides: we inspect the real dependency
    try:
        data = client.get("/api/model").json()
        assert data["tier"] is None and "Haiku 4.5" in data["tiers"]

        resp = client.put("/api/model", json={"tier": "Sonnet 5"})
        assert resp.status_code == 200
        assert resp.json()["model"] == "claude-sonnet-5"
        assert client.get("/api/model").json()["tier"] == "Sonnet 5"

        # the selection drives provider construction, i.e. the next turn app-wide
        # (local/markdown mode ⇒ env keys, store untouched)
        provider = web_app.get_provider(Config(), user=None, store=None)
        assert isinstance(provider, AnthropicProvider)
        assert provider._model == "claude-sonnet-5"

        assert client.put("/api/model", json={"tier": "GPT 9"}).status_code == 400
        assert client.get("/api/model").json()["tier"] == "Sonnet 5"  # unchanged
    finally:
        web_app._current_tier = None


def test_vendored_chat_libraries_are_served():
    client = TestClient(app)
    for name in ("marked.min.js", "purify.min.js"):
        resp = client.get(f"/static/vendor/{name}")
        assert resp.status_code == 200, name
        assert "javascript" in resp.headers["content-type"]


# --- chat assistant (M8) --------------------------------------------------------


class _FakeChatProvider(LLMProvider):
    """Scripted chat() results; records each system prompt (live-state checks)."""

    def __init__(self, results: list[ChatResult]) -> None:
        self._results = list(results)
        self.systems: list[str] = []

    def complete(self, *, system_prompt, user_content, max_tokens=None, response_schema=None):
        raise AssertionError("the chat endpoint must not call complete()")

    def chat(self, *, system_prompt, messages, tools=None, max_tokens=None):
        self.systems.append(system_prompt)
        return self._results.pop(0)


def _chat_setup(tmp_path, results):
    store = MarkdownStore(tmp_path / "todos.md")
    provider = _FakeChatProvider(results)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_provider] = lambda: provider
    return TestClient(app), store, provider


def test_chat_stages_merge_gate_holds_and_next_turn_sees_live_list(tmp_path):
    results = [
        ChatResult(
            text="Those two Q3 items look like the same task — here's a merge.",
            tool_calls=[ToolCall(id="t1", name="propose_merge",
                                 input={"keep_id": "q1", "absorb_id": "q2"})],
            usage=Usage(9, 4),
        ),
        ChatResult(text="The list now has a single Q3 item.", tool_calls=[], usage=Usage(5, 1)),
    ]
    client, store, provider = _chat_setup(tmp_path, results)
    try:
        store.save([
            _stored_item("q1", "Send the Q3 eval report", owner="Priya"),
            _stored_item("q2", "Get the Q3 report out", description="the exec summary"),
        ])

        # turn 1: the merge is staged, not applied (advisory gate)
        resp = client.post("/api/chat", json={"messages": [
            {"role": "user", "content": "aren't the two Q3 items duplicates?"}]})
        assert resp.status_code == 200
        data = resp.json()
        assert "same task" in data["message"]
        (proposal,) = data["proposals"]
        assert proposal["op"] == "merge"
        assert proposal["id"] == "q1" and proposal["absorb_id"] == "q2"
        assert data["dropped"] == []
        assert len(store.load()) == 2  # nothing touched the store

        # the user accepts → commit collapses the two into one
        commit = client.post("/api/chat/commit", json={"ops": [
            {"op": "merge", "id": "q1", "absorb_id": "q2"}]})
        assert commit.status_code == 200
        assert len(commit.json()["applied"]) == 1
        (merged,) = store.load()
        assert merged.id == "q1" and merged.description == "the exec summary"

        # turn 2 re-injects the live (merged) list (§4.6)
        resp2 = client.post("/api/chat", json={"messages": [
            {"role": "user", "content": "thanks, what's left?"}]})
        assert resp2.status_code == 200
        assert "Send the Q3 eval report" in provider.systems[1]
        assert "Get the Q3 report out" not in provider.systems[1]
    finally:
        app.dependency_overrides.clear()


def test_paste_turn_extracts_items_through_the_chat_gate(tmp_path):
    """The unified surface (M12): pasted notes are a chat turn yielding NEW cards
    with source snippets; nothing lands until each card is accepted."""
    results = [ChatResult(
        text="I found two action items; the docs line was already tracked.",
        tool_calls=[
            ToolCall(id="t1", name="propose_new",
                     input={"title": "Renew the staging cert", "owner": "Marco",
                            "due_date_text": "by Tuesday",
                            "source_snippet": "Marco offered to renew the staging cert by Tuesday."}),
            ToolCall(id="t2", name="propose_new",
                     input={"title": "Draft the launch note",
                            "source_snippet": "Someone should draft the launch note."}),
        ],
        usage=Usage(20, 9),
    )]
    client, store, _ = _chat_setup(tmp_path, results)
    try:
        notes = ("- Standup\n- Marco offered to renew the staging cert by Tuesday.\n"
                 "- Someone should draft the launch note.")
        data = client.post("/api/chat", json={"messages": [
            {"role": "user", "content": notes}]}).json()
        assert [p["op"] for p in data["proposals"]] == ["new", "new"]
        assert all(p["source_snippet"] for p in data["proposals"])  # miss-catching link
        assert store.load() == []  # the gate: nothing extracted lands unasked

        # the user accepts one card and rejects the other (never sent)
        client.post("/api/chat/commit", json={"ops": [
            {"op": "new", "title": "Renew the staging cert", "owner": "Marco",
             "due_date_text": "by Tuesday",
             "source_snippet": "Marco offered to renew the staging cert by Tuesday."}]})
        (item,) = store.load()
        assert item.title == "Renew the staging cert"
        assert item.source_snippet == "Marco offered to renew the staging cert by Tuesday."
    finally:
        app.dependency_overrides.clear()


def test_chat_drops_stale_ids_instead_of_staging_them(tmp_path):
    results = [ChatResult(
        text="Marking it done.",
        tool_calls=[ToolCall(id="t1", name="propose_complete", input={"id": "nope"})],
        usage=Usage(3, 2),
    )]
    client, store, _ = _chat_setup(tmp_path, results)
    try:
        store.save([_stored_item("a", "Real task")])
        data = client.post("/api/chat", json={"messages": [
            {"role": "user", "content": "finish it"}]}).json()
        assert data["proposals"] == []
        assert len(data["dropped"]) == 1 and "nope" in data["dropped"][0]
        assert [it.title for it in store.load()] == ["Real task"]  # untouched
    finally:
        app.dependency_overrides.clear()


def test_chat_commit_revalidates_ids_and_frozen_items(tmp_path):
    client, store, _ = _chat_setup(tmp_path, [])
    try:
        store.save([_stored_item("d", "Shipped", status="done")])
        data = client.post("/api/chat/commit", json={"ops": [
            {"op": "complete", "id": "zzz"},
            {"op": "update", "id": "d", "title": "Rewrite history"},
        ]}).json()
        assert data["applied"] == [] and len(data["skipped"]) == 2
        (item,) = store.load()
        assert item.title == "Shipped" and item.status == "done"
    finally:
        app.dependency_overrides.clear()


def test_chat_commit_delete_is_soft(tmp_path):
    client, store, _ = _chat_setup(tmp_path, [])
    try:
        store.save([_stored_item("a", "Old noise")])
        data = client.post("/api/chat/commit", json={"ops": [{"op": "delete", "id": "a"}]}).json()
        assert data["applied"] and "moved to Deleted" in data["applied"][0]
        (item,) = store.load()
        assert item.status == "deleted"  # row kept — hidden from the active view
    finally:
        app.dependency_overrides.clear()


def test_chat_profile_section_update_lands_only_after_acceptance(tmp_path):
    results = [
        ChatResult(
            text="That's a durable goal — proposing a profile update.",
            tool_calls=[ToolCall(id="t1", name="propose_profile_update",
                                 input={"section": "Long-term goals",
                                        "new_text": "Ship the eval platform."})],
            usage=Usage(6, 2),
        ),
        ChatResult(text="ok", tool_calls=[], usage=Usage(2, 1)),
    ]
    client, store, provider = _chat_setup(tmp_path, results)
    try:
        store.save([_stored_item("a", "Some task")])
        client.put("/api/profile", json={
            "profile": "## Long-term goals\n\nTBD.\n\n## Current focus\n\nQ3 report."})

        # staging alone must not write anything (the gate)
        data = client.post("/api/chat", json={"messages": [
            {"role": "user", "content": "my real goal is shipping the eval platform"}]}).json()
        (proposal,) = data["proposals"]
        assert proposal["op"] == "profile" and proposal["section"] == "Long-term goals"
        assert proposal["before"] == "TBD."  # that section's current body
        assert "TBD." in (tmp_path / "profile.md").read_text()  # untouched so far

        # acceptance rewrites only the named section
        commit = client.post("/api/chat/commit", json={"ops": [
            {"op": "profile", "section": "Long-term goals",
             "new_text": "Ship the eval platform."}]}).json()
        assert 'profile: updated "Long-term goals"' in commit["applied"]
        text = (tmp_path / "profile.md").read_text()
        assert "Ship the eval platform." in text and "TBD." not in text
        assert "Q3 report." in text  # the other section survives

        # the next turn sees the accepted profile in its context
        client.post("/api/chat", json={"messages": [{"role": "user", "content": "thanks"}]})
        assert "Ship the eval platform." in provider.systems[1]

        # over-cap section text is skipped at commit time too
        bad = client.post("/api/chat/commit", json={"ops": [
            {"op": "profile", "section": "Current focus", "new_text": "x" * 700}]}).json()
        assert bad["applied"] == [] and "cap" in bad["skipped"][0]["reason"]
    finally:
        app.dependency_overrides.clear()


def test_two_target_proposals_land_in_their_own_stores(tmp_path):
    """M13 acceptance: one conversation proposes a todo AND a profile-goal
    update; accepting both routes each to the right store."""
    results = [ChatResult(
        text="That's a durable goal — and here's the first concrete step.",
        tool_calls=[
            ToolCall(id="t1", name="propose_profile_update",
                     input={"section": "Long-term goals",
                            "new_text": "Run a marathon next year."}),
            ToolCall(id="t2", name="propose_new",
                     input={"title": "Sign up for the Tuesday run club",
                            "due_date_text": "by Friday"}),
        ],
        usage=Usage(11, 6),
    )]
    client, store, _ = _chat_setup(tmp_path, results)
    try:
        store.save([_stored_item("a", "Existing task")])
        data = client.post("/api/chat", json={"messages": [
            {"role": "user", "content": "I want to run a marathon next year — get me started"}]}).json()
        assert [(p["op"], p["target"]) for p in data["proposals"]] == [
            ("profile", "profile"), ("new", "todo")
        ]
        assert len(store.load()) == 1 and not (tmp_path / "profile.md").exists()  # gate

        # accepting both in one commit routes each to its destination
        commit = client.post("/api/chat/commit", json={"ops": [
            {"op": "profile", "section": "Long-term goals",
             "new_text": "Run a marathon next year."},
            {"op": "new", "title": "Sign up for the Tuesday run club",
             "due_date_text": "by Friday"},
        ]}).json()
        assert len(commit["applied"]) == 2
        assert [i.title for i in store.load()] == [
            "Existing task", "Sign up for the Tuesday run club"
        ]
        assert "Run a marathon next year." in (tmp_path / "profile.md").read_text()
        assert "marathon" not in " ".join(i.title for i in store.load())  # goal ≠ todo
    finally:
        app.dependency_overrides.clear()


def test_chat_request_validation(tmp_path):
    client, _, _ = _chat_setup(tmp_path, [])
    try:
        assert client.post("/api/chat", json={"messages": []}).status_code == 400
        assert client.post("/api/chat", json={"messages": [
            {"role": "assistant", "content": "hi"}]}).status_code == 400
        assert client.post("/api/chat", json={"messages": [
            {"role": "user", "content": "   "}]}).status_code == 400
    finally:
        app.dependency_overrides.clear()


def test_chat_with_a_provider_lacking_chat_support_is_502(tmp_path):
    client, _ = _setup(tmp_path, [])  # v1 fake provider: complete() only
    try:
        resp = client.post("/api/chat", json={"messages": [
            {"role": "user", "content": "hello"}]})
        assert resp.status_code == 502
        assert "does not support chat" in resp.json()["detail"]
    finally:
        app.dependency_overrides.clear()
