"""End-to-end v2 regression (M10): the full loop through the web API.

notes → extract (profile-injected, rationales) → review → commit → manage
(reorder / prioritize) → chat-refine (advice, MERGE, COMPLETE) → done — with
the advisory gate, live-list re-injection, and no-resurrect rules asserted
along the way. Providers are scripted; no API key needed.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from meeting_notes_todos.models import ExtractedItem, ExtractionResponse
from meeting_notes_todos.providers.base import ChatResult, CompletionResult, LLMProvider, ToolCall, Usage
from meeting_notes_todos.store import MarkdownStore
from meeting_notes_todos.web.app import app, get_provider, get_store

PROFILE = "Name: Austin Zhang. Role: eval lead on project Beacon."

EXTRACTION = ExtractionResponse(
    items=[
        ExtractedItem(
            title="Send the Q3 eval report to leadership", owner="Priya",
            due_date_text="by Friday", source_snippet="s1",
            rationale="Named owner with a stated deadline.",
        ),
        ExtractedItem(
            title="Get the Q3 numbers write-up out to the leadership team",
            source_snippet="s2", description="the exec summary",
            rationale="Re-stated ask, phrased differently; no owner given.",
        ),
        ExtractedItem(
            title="Renew the staging cert", owner="Marco", source_snippet="s3",
            rationale="Explicit infrastructure task.",
        ),
    ]
)


class _LoopProvider(LLMProvider):
    """complete() serves extraction; chat() pops turns queued just-in-time."""

    def __init__(self) -> None:
        self.extract_prompts: list[str] = []
        self.chat_systems: list[str] = []
        self._chat: list[ChatResult] = []

    def queue_chat(self, text: str, calls: list[ToolCall] | None = None) -> None:
        self._chat.append(ChatResult(text=text, tool_calls=calls or [], usage=Usage(5, 2)))

    def complete(self, *, system_prompt, user_content, max_tokens=None, response_schema=None):
        self.extract_prompts.append(user_content)
        return CompletionResult(text="", usage=Usage(10, 5), parsed=EXTRACTION)

    def chat(self, *, system_prompt, messages, tools=None, max_tokens=None):
        self.chat_systems.append(system_prompt)
        return self._chat.pop(0)


def test_full_v2_loop_end_to_end(tmp_path):
    store = MarkdownStore(tmp_path / "todos.md")
    provider = _LoopProvider()
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_provider] = lambda: provider
    client = TestClient(app)
    try:
        # 1. seed the profile (v2 §12.9: user-seeded)
        client.put("/api/profile", json={"profile": PROFILE})

        # 2. review: extraction sees the profile; proposals carry rationales (§12.3)
        review = client.post(
            "/api/review", json={"notes": "standup notes", "meeting_date": "2026-07-06"}
        ).json()
        assert PROFILE in provider.extract_prompts[0]
        proposals = review["proposals"]
        assert [p["kind"] for p in proposals] == ["new", "new", "new"]
        assert all(p["rationale"] for p in proposals)

        # 3. the user accepts all three (reviewed values are authoritative)
        commit = client.post("/api/commit", json={
            "meeting_date": "2026-07-06",
            "items": [
                {"action": "add", "title": p["title"], "owner": p["owner"],
                 "due_date_text": p["due_date_text"], "description": p["description"],
                 "id": p["id"], "rationale": p["rationale"]}
                for p in proposals
            ],
        }).json()
        assert commit["added"] == 3
        q1, q2, cert = [p["id"] for p in proposals]

        # 4. manage: prioritize and reorder, both persist (§12.1)
        assert client.patch(f"/api/items/{cert}", json={"priority": "high"}).status_code == 200
        assert client.post("/api/items/reorder", json={"ids": [cert, q1, q2]}).status_code == 200
        items = store.load()
        assert [i.id for i in items] == [cert, q1, q2]
        assert items[0].priority == "high" and [i.position for i in items] == [0, 1, 2]

        # 5. chat, pure-advice turn: a real answer, zero list changes (§12.4)
        provider.queue_chat("Do the cert first — it has the tightest deadline.")
        turn = client.post("/api/chat", json={"messages": [
            {"role": "user", "content": "what should I do first?"}]}).json()
        assert turn["proposals"] == [] and len(store.load()) == 3
        assert PROFILE in provider.chat_systems[0]  # profile feeds chat too (§12.9)
        assert f"id: {q1}" in provider.chat_systems[0]  # live list with ids (§4.5)

        # 6. chat proposes a MERGE for the semantic duplicate; the gate holds (§12.6/7)
        provider.queue_chat(
            "Those two Q3 items are the same task.",
            [ToolCall(id="t1", name="propose_merge", input={"keep_id": q1, "absorb_id": q2})],
        )
        turn = client.post("/api/chat", json={"messages": [
            {"role": "user", "content": "merge the Q3 duplicates"}]}).json()
        (merge,) = turn["proposals"]
        assert merge["op"] == "merge" and len(store.load()) == 3  # staged, not applied

        # 7. accepting the merge collapses the duplicates (§12.5/6)
        client.post("/api/chat/commit", json={"ops": [
            {"op": "merge", "id": q1, "absorb_id": q2}]})
        items = store.load()
        assert [i.id for i in items] == [cert, q1]
        assert items[1].description == "the exec summary"  # filled from the absorbed item

        # 8. chat COMPLETE → done (§12.5)
        provider.queue_chat("Done!", [ToolCall(id="t2", name="propose_complete",
                                               input={"id": cert})])
        client.post("/api/chat", json={"messages": [{"role": "user", "content": "cert is done"}]})
        client.post("/api/chat/commit", json={"ops": [{"op": "complete", "id": cert}]})
        assert store.load()[0].status == "done"

        # 9. the next turn reasons about the live, updated list (§12.8)
        provider.queue_chat("One Q3 item left; the cert is done.")
        client.post("/api/chat", json={"messages": [{"role": "user", "content": "what's left?"}]})
        assert "Get the Q3 numbers" not in provider.chat_systems[-1]  # absorbed item gone
        assert f"[done] Renew the staging cert" in provider.chat_systems[-1]

        # 10. stale ids are caught, never applied (§12.8)
        bad = client.post("/api/chat/commit", json={"ops": [
            {"op": "complete", "id": q2}]}).json()  # q2 was absorbed — stale now
        assert bad["applied"] == [] and len(bad["skipped"]) == 1

        # 11. nothing resurrects the done item — review-commit or chat (§12.2)
        skipped = client.post("/api/commit", json={"items": [
            {"action": "update", "target_id": cert, "title": "Renew it again"}]}).json()
        assert skipped["updated"] == 0 and "done" in skipped["skipped"][0]["reason"]
        rejected = client.post("/api/chat/commit", json={"ops": [
            {"op": "update", "id": cert, "title": "Renew it again"}]}).json()
        assert rejected["applied"] == [] and store.load()[0].title == "Renew the staging cert"
    finally:
        app.dependency_overrides.clear()
