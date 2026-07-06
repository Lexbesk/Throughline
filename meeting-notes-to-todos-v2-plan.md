# Meeting Notes → TODOs — v2 Build Plan

v2 **extends** the shipped v1 app (milestones M0–M5). It does not rebuild anything. Everything from v1 stays: the provider abstraction, the extract → validate → reconcile → merge pipeline, the deterministic date resolver, the markdown store, the prompts-as-md-files pattern, and the FastAPI + vanilla-JS review UI. v2 layers four independent capabilities on top and turns the tool into an assistant.

Milestones continue the v1 numbering (**M6–M10**) so Claude Code treats this as one evolving project. This document contains no code.

---

## 1. What v2 adds (four independent features)

Each feature is separately shippable and separately testable. Do a real manual test pass at the end of each milestone before starting the next.

1. **Management view** — the app is currently a review screen (notes → proposals → commit), not a list you live in. v2 adds a persistent list view where tasks stay, can be prioritized and ordered, and can be **checked off**. This is the foundation the other three build on.
2. **Reasoning display** — the model emits a short rationale for each item (why it kept, merged, or classified it), shown in a panel beside the items.
3. **Chat assistant** — a continuous conversation beside the list: refine tasks, correct the model's logic, and talk through how / in what order / what matters most. It also becomes the manual override for deduplication misses (e.g. "merge those two Q3 items"). This is the centerpiece of v2.
4. **User profile** — a `profile.md` beside `todos.md`, seeded by the user and updated over time by the model (proposing updates, kept human-editable), injected as context so extraction and prioritization know the user's role, projects, and preferences.

---

## 2. Scope and Non-Goals (v2)

**In scope:** the four features above, built **advisory-first** (the assistant proposes; the user approves).

**Non-goals (deferred):**
- **Agentic auto-apply** as the default. The chat proposes and the user confirms; a chat that mutates the list without confirmation is a documented *future* toggle, not v2.
- **Semantic (embedding-based) deduplication** — deferred pending the decision in §14; in v2 the chat's MERGE operation covers the fuzzy-match misses by hand.
- Still out (carried from v1): multi-user, accounts, hosting, mobile, audio/speech-to-text.

---

## 3. Locked decisions (both are one-line flips)

1. **Advisory-first.** Every concrete change the assistant wants to make is *staged as a proposal* the user accepts, edits, or rejects. Nothing is auto-applied. This is the same propose/dispose gate that has been the spine of the whole project.
2. **Tool use** as the chat implementation pattern (details in §4). The simpler **single-structured-response** approach is written in as a documented, migratable fallback — and doubles as the graceful path for local models whose tool-calling is weak.

If either changes, it changes in one place: the chat module's response-handling (§4.4) and the system prompt (§4.5).

---

## 4. Chat Assistant (the centerpiece)

### 4.1 A chat turn does two jobs at once
Extraction was one job: notes in, structured items out. A chat turn is a **superset**: it produces (a) a natural-language message to the user *and* (b) zero or more structured proposals. Forcing everything into item-JSON would kill the conversation, which is the whole point of a chat rather than another extract button. So a turn = **message + optional proposals**.

### 4.2 Proposal operations
Richer than extraction's NEW-only, because the chat edits an existing list rather than filling an empty one. Each operation references existing items **by their stable id**:
- `NEW` — add an item
- `UPDATE` — change fields on an item (show filled before → after)
- `DELETE` — remove an item
- `MERGE` — collapse two items into one (**first-class** — this is the chat's override for the semantic dedup misses the fuzzy pre-filter can't catch, e.g. the two Q3 lines)
- `REPRIORITIZE` — change priority / ordering
- `COMPLETE` — mark done (respects the existing rule: never resurrect a done/cancelled item)

### 4.3 The advisory gate
Proposals are surfaced in the UI as accept / edit / reject cards — identical in spirit to the M4 review screen. Nothing touches the store until the user commits. Reviewed values are authoritative (as in v1's `commit_reviewed`), but committing still never rewrites a done/cancelled item's status, id, or creation date.

### 4.4 Implementation pattern — tool use (with a documented fallback)
**Tool use (chosen):** the model is given tools — `propose_new`, `propose_update`, `propose_delete`, `propose_merge`, `reprioritize`, `propose_complete`. It talks normally in its text and emits a tool call whenever it wants to propose a change. In advisory-first, **the tools do not execute — they stage a proposal** for approval. This cleanly separates *talking* from *acting*: the model's text is the conversation (and the reasoning to show, §5), the tool calls are the structured proposals. It also gives a clean migration path — going advisory → agentic later is simply "execute the accepted tool instead of staging it."

**Fallback (documented, migratable):** a single structured response — one JSON object `{ "message": "...", "proposals": [ {op, id?, fields...} ] }`. This reuses v1's schema-validation-with-repair machinery most directly and is the sensible degradation for a local model that doesn't do tool calling well. The two patterns are not a permanent fork; the chat module can migrate between them.

### 4.5 What is assembled *before* the user's message
The chat is **stateful** in a way extraction is not — to say anything useful about "these tasks" or "what's most important," the model must see the current list, the profile, and the conversation so far. So "what goes before the user's message" is the system prompt **plus injected state**, in this order:

1. **System prompt** (`prompts/chat.md`) — the role (a task assistant refining a list built from meeting notes); the **advisory contract** (propose, never silently apply; every concrete change is a user-confirmed proposal); the operation vocabulary (§4.2), each referencing items **by id**; **when to propose vs. just talk** (planning/prioritization questions often need *no* proposals — do not fabricate changes to look busy); and the same grounding rules as extraction (don't invent items, keep owners/dates faithful, leave unknowns empty).
2. **The current todo list**, injected as structured text/JSON, each item with its **id**, status, owner, dates, and priority. Without ids, UPDATE/DELETE/MERGE/COMPLETE have nothing to target.
3. **The user profile** (`profile.md`) — role, projects, priorities — so prioritization advice is actually about this user.
4. **Conversation history** — so "reprioritize *those*" resolves.
5. **The new user message.**

### 4.6 The live-state rule
Re-inject the **live** list every turn. After the user accepts proposals and the list changes, the next turn must see the updated list — not a stale snapshot. This is the classic stateful-chat bug; call it out and test for it.

### 4.7 Safeguards
- **Validate every proposal's id** against the real list; drop or flag ones that don't exist (hallucinated or stale) — the same repair discipline already used on extraction JSON.
- **Keep advice distinct from proposals.** Most chat is pure talk with no list change; only concrete mutations become cards. Tool use gives this separation for free (prose vs. tool call); the fallback enforces it via the empty-`proposals` case.
- **Guard MERGE and over-aggressive edits** — surface before/after, lean toward keeping items separate when unsure, let the user veto.

### 4.8 Provider interface extension
The chat needs the provider abstraction to support tools — either extend `complete()` to accept tool definitions, or add a sibling `chat()` method that accepts tools and returns a response that may contain both text and tool-call blocks. Keep it **inside** the abstraction (a small, contained extension, not a break): `AnthropicProvider` implements it via the SDK's tool use; `LocalProvider` implements it via OpenAI-compatible function calling, or falls back to the structured-response pattern (§4.4) when local tool-calling is unreliable.

---

## 5. Reasoning Display

- Extraction (and each chat turn) emits a short **rationale** — why an item was kept, merged, or classified — shown in a panel beside the items on the right.
- Cheapest of the four features: a `rationale` field on the schema + a prompt line + a UI panel.
- With tool use, this pairs naturally: the model's **text between tool calls is the reasoning**, so the same panel serves both extraction and chat.
- **Epistemic caveat (state it in the UI):** this surfaces the model's *stated* reasoning, not a literal trace of its computation — useful for steering, not ground truth. Same lesson as the dates.

---

## 6. User Profile

- A `profile.md` living beside `todos.md`: the user's role, projects, priorities, working preferences.
- **Seeded by the user** at the start, then **updated by the model over time** — but the model **proposes** updates (shown for approval, kept human-editable), never silently rewrites. This is the same gate again, and it is the specific guard against the well-known failure of auto-updating memory: **drift and junk accumulation** if nothing checks it. Keep the profile short.
- **Injected as context** into both extraction and chat, so classification and prioritization reflect who the user is.
- Treated "like the todo list": an evolving, human-editable artifact.

---

## 7. Management View

- A persistent **list view**, distinct from the review screen: all stored tasks shown with status, owner, dates, and priority, in a user-controlled order.
- **Check-off / status:** the `status` field (todo / doing / done / cancelled) already exists in the v1 data model, and merge already refuses to resurrect done items — so the *backend* notion of "done" is real. What's missing is the **UI to set it**.
- **Priority and manual ordering:** new field(s) on the item, plus UI to set them.
- This view is also the **foundation for the chat** — it gives the assistant a visible list to manipulate, and it is where "what order / what's most important" becomes concrete.

---

## 8. Data Model Additions

- **ActionItem:** add `priority` (e.g. an enum or integer) and an explicit `order`/`position` for manual ordering.
- **Rationale:** produced per extraction/chat turn and shown in the UI; persist the latest per item if useful (optional).
- **New persisted artifact:** `profile.md` (the user profile), read/written like the store.
- **Chat state:** conversation history (in-session at minimum; persistence is an open decision, §14). Staged proposals are transient — committed to the store or discarded.

Keep the store a thin module (as in v1) so these additions stay isolated.

---

## 9. Provider Interface Extension

See §4.8. One small, contained change: teach the provider interface about tool definitions (extend `complete()` or add `chat()`), implement it for Anthropic (SDK tool use) and local (OpenAI-compatible function calling, with the structured-response fallback). The extraction/reconcile paths are unaffected.

---

## 10. Project Layout Additions

```
prompts/
├── chat.md              # chat system prompt + advisory contract + op vocabulary
└── profile_update.md    # prompt for proposing profile updates (or fold into chat.md)
src/meeting_notes_todos/
├── chat/                # assemble context, call provider w/ tools, parse text + tool calls
│                        #   into staged proposals, commit accepted ones
└── profile.py           # read/write/inject profile; propose (not apply) updates
data/
└── profile.md           # the user profile (seeded by user, model proposes updates)
web/
├── app.py               # + list/management endpoints, + chat endpoints
└── static/index.html    # + list view, + chat panel, + reasoning panel
```

---

## 11. Milestones (M6–M10)

- **M6 — Management view.** Persistent list view separate from the review screen; mark tasks done/cancelled from the UI; add priority + manual ordering (data model + UI). *Acceptance:* view all tasks; check one off and it persists (and merge won't resurrect it); reorder/prioritize and it persists.
- **M7 — Reasoning display.** Extraction emits a per-item `rationale` (schema + prompt + UI panel). *Acceptance:* after extraction, each proposed item shows a short rationale; a merged/excluded decision is explainable. (Panel is reused by the chat in M8.)
- **M8 — Chat assistant (advisory-first, tool use).** Extend the provider for tool use (§4.8); define the proposal tools that **stage** (don't execute); assemble per-turn context (§4.5); parse each turn into a chat message (+ reasoning panel) and accept/edit/reject proposal cards; commit accepted proposals through the existing store and re-inject the live list next turn (§4.6); validate proposal ids (§4.7). *Acceptance:* the assistant can (a) answer a planning/prioritization question with **no** list change, and (b) propose concrete changes as cards; accepting a **MERGE** collapses two items (fixes a Q3-style duplicate by hand); rejecting a proposal changes nothing; a stale/invalid id is caught, not applied; the next turn reasons about the updated list.
- **M9 — User profile.** `profile.md` seeded by the user; a mechanism where the model **proposes** profile updates (approved before written, human-editable); profile injected into extraction and chat context. *Acceptance:* user creates/edits the profile; the model proposes an update that only lands after approval; extraction/prioritization reflect profile content.
- **M10 — Integration & polish.** Ensure the profile feeds both extraction and chat, and the reasoning panel serves both; full end-to-end regression (notes → extract → review → list → chat-refine → done); update README/docs for v2 (new commands/config). *Acceptance:* the full v2 loop works end to end; docs updated; all v1 behaviors still pass.

---

## 12. Acceptance Criteria for v2 (definition of done)

1. Tasks persist in a list view and can be reordered and prioritized.
2. Tasks can be marked done/cancelled in the UI; done items are never resurrected by merge or chat.
3. Extraction and chat each show a short, per-item rationale (labeled as stated reasoning, not a trace).
4. The chat holds a real conversation: pure-advice turns produce no list changes; concrete changes appear as accept/edit/reject cards.
5. All proposal operations work — NEW, UPDATE (with before→after), DELETE, MERGE, REPRIORITIZE, COMPLETE — and reference items by id.
6. Accepting a MERGE fixes a semantic duplicate the fuzzy pre-filter missed.
7. Nothing is applied without user confirmation (advisory gate holds).
8. Invalid/stale proposal ids are caught and not applied; each turn reasons about the live list.
9. The profile is user-seeded and model-*proposed* (never silently rewritten) and measurably influences extraction/prioritization.
10. Switching provider/model is still a config-only change; the chat works through the same provider abstraction.

---

## 13. Risks & Edge Cases

- **Hallucinated / stale item ids in proposals** → id validation + repair (§4.7).
- **Over-aggressive MERGE or edits** (from dedup *or* chat) → show before/after, keep separate when unsure, user vetoes. A bad merge is a silent loss dressed up as a merge — the same failure shape as a wrong date.
- **Profile drift / junk accumulation** → model proposes, human approves and edits; keep it short.
- **Stated reasoning mistaken for ground truth** → label it as the model's rationale in the UI.
- **Stale list in chat** → re-inject live state every turn (§4.6); test for it explicitly.
- **Local-model tool-use variance** → structured-response fallback behind the same provider abstraction (§4.4/§4.8).
- **Scope creep from "assistant"** → the advisory gate bounds it; agentic auto-apply stays out of v2.

---

## 14. Open Decisions

1. **Chat pattern:** tool use (locked lean, §4.4) vs. single structured response (fallback). Confirm.
2. **Advisory vs. agentic:** advisory-first is locked for v2; agentic auto-apply is a documented future toggle. Confirm it stays out for now.
3. **Semantic dedup:** add an embeddings pre-filter for the Q3-style miss, or rely on the chat's MERGE for now. *Lean: defer embeddings* — the chat handles it by hand in v2.
4. **Meeting-date capture:** confirm ingest lets the user set the meeting date (relative dates resolve against it). If the UI currently assumes "today," fix it in M6.
5. **Profile ordering:** M9 as sequenced, or pull the profile **earlier** (before the chat, M8) since chat prioritization benefits from it. Your call.
6. **Chat-history persistence:** in-session only (simpler v2 default) vs. persisted chat sessions. Decide.
