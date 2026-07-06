# Meeting Notes → TODOs — v3 Build Plan

v3 **extends** the shipped app (v1 = M0–M5, v2 = M6–M10). It does not rebuild anything. Everything stays: the provider abstraction, the extract/validate/reconcile/merge pipeline, the deterministic date resolver, the markdown store, the active/completed/deleted views, the advisory proposal gate, the reasoning panel, the chat assistant, and the profile.

**The pivot:** v1 and v2 built a *capture* tool — get what was said into a clean list. v3 turns it into a *planning* tool — reason about that list against where you're trying to go. The profile becomes the north star; a single conversational surface turns any life input into todos or goal-updates through the proposal gate; and the assistant can analyze your plan against your goals.

Milestones continue as **M11–M15**. This document contains no code.

---

## 1. What v3 is

- **Capture → planning.** The center of gravity moves from "what did I commit to?" to "am I doing the things that get me where I want to go?"
- **The profile is the north star.** Once it holds your long-term goals, the assistant can reason about your list *against* them. This is the foundation everything else stands on.
- **One conversational surface.** The extraction/input box and the assistant merge into a single chatbox — paste notes, converse, refine, all in one place, all through the same accept/edit/reject gate.
- **The assistant proposes to two targets.** From that one box, a proposal lands in either the **todo list** (near-term actions) or the **profile** (goals and other sections).
- **Gap analysis.** The assistant can, on request, reason over the todo list against the profile's goals and flag what's missing or misaligned. This is the single feature that justifies the pivot.

---

## 2. Scope and Non-Goals (v3)

**In scope:** the five items in §1, built on the existing advisory gate.

**Non-goals (deferred):**
- Still out (carried forward): multi-user, accounts, hosting, mobile, audio/speech-to-text.
- Still deferred from v2 §2: agentic auto-apply as default, embeddings-based dedup, persisted chat sessions. (Chat is now the primary surface, so persisted sessions may be worth pulling forward later — flagged, not scheduled.)

**Not a non-goal, just deferred to your call:** the **rename** (see §13). The app's identity is broadening past "MeetingNotes"; the rename is mechanical and blocks nothing.

---

## 3. Locked decisions

1. **One global model switch** — a single Haiku / Sonnet / Opus selector for the whole app (there is one surface now, so per-surface routing isn't needed). Model strings live in config, mapped from tier names, so a changing lineup is a config edit, not code.
2. **One conversational surface** — input box + assistant merged; extraction becomes the special case "pull items out of this text."
3. **The assistant chats *and* proposes** — a turn produces a natural-language message (real conversation) plus zero-or-more proposals targeting the todo list and/or the profile.
4. **Todo list stays near-term only** — no long-term tag or view; keeps active/completed/deleted.
5. **Long-term goals live in the profile**, as a section, not as todos. (A goal is a direction, not a checkbox.)
6. **Gap analysis at reason-time** — the assistant reasons from the two injected contexts (profile = goals, list = todos); no stored link between a goal and a todo is required.
7. **Broadened scope** *(one-line flip if you'd rather stay meeting-shaped)* — the assistant works on any life input (a new idea, a person met, a situation), not only meeting notes.

---

## 4. The Assistant (one surface, chats + proposes to two targets)

### 4.1 It is a conversational partner, not an extract button
A turn does two jobs (the v2 design, carried forward): (a) a **natural-language message** — planning your week, advising, running gap analysis, discussing how/order/importance, or just talking — and (b) **zero or more proposals**. Most turns are pure conversation with no proposals; only concrete changes become cards. Do not force replies into item-JSON.

### 4.2 Proposals now target two places
Each proposal is routed and labeled by **target**:
- **Todo list** — the six operations from v2 (NEW, UPDATE, DELETE, MERGE, REPRIORITIZE, COMPLETE), referencing items by id.
- **Profile** — an update to a named profile **section** (add / revise / remove a goal, refine priorities, etc.), shown with before → after, landing only on approval. This extends the M9 propose-don't-overwrite mechanism to the structured profile (§5).

The UI surfaces each proposal as an accept / edit / reject card pointing at the right destination. The advisory gate is unchanged: nothing is applied without confirmation.

### 4.3 Extraction is now a mode of the assistant
Pasting notes is a chat turn whose input is pasted text; the assistant responds with item proposals. Keep the **source-beside-proposals** layout from M4 — proposals render as cards with their source visible — so *misses* stay catchable. This is the one thing not to lose when the extraction box is absorbed into the chat.

### 4.4 Context assembled before the user's message (unchanged shape, richer profile)
System prompt (advisory contract + op vocabulary + the two targets + when to propose vs. just talk) → **the live todo list with ids** → **the full profile (§5)** → conversation history → the new user message. Re-inject the live list and profile every turn (the M8 live-state rule).

---

## 5. Profile as North Star (with the must-have sections)

The profile is a living markdown document (`profile.md`), displayed as **rendered markdown**, hand-editable but **mainly updated by the assistant** — which *proposes* section updates that land only after approval (M9 gate). It is injected into every turn, so it is kept **tight**: each section is a few lines, length-capped, signal over volume. A bloated profile makes every call costlier and dilutes the goals the assistant is supposed to reason about.

### 5.1 The must-have sections
Seeded for every user, user-extendable. Five sections, each serving a distinct reasoning job:

1. **Long-term goals** — the north star. A handful (≈2–5) of durable directions you're steering toward, each a short statement, optionally with a rough horizon. Not checkboxes. This is what gap analysis reasons against ("which goals have nothing serving them?"). Stable; evolves slowly.
2. **Current focus** — the medium-term themes and active efforts bridging daily todos and long-term goals (e.g. "finishing X," "preparing for Y"). A few active items. More dynamic; the assistant updates as focus shifts. This is what lets the assistant connect a new todo to the bigger picture and notice when daily work drifts from stated focus.
3. **Priorities & values** — how you weigh competing demands and what matters most right now: the weighting function for ranking todos and giving tradeoff advice. Distinct from goals (destinations) and focus (current efforts) — this is *how to choose* between them.
4. **Working style & personality** — how you operate and how the assistant should tailor itself: communication preferences, how you like plans structured, what motivates or derails you, temperament. Tunes *delivery* — phrasing, ordering, tone. Mostly stable.
5. **Context & constraints** — durable situational facts that ground the assistant: role, life stage, location, relevant commitments, and hard limits (time, money, non-negotiables). Keeps reasoning realistic rather than generic. Stable; changes at life events.

### 5.2 How sections are updated
The assistant targets a **named section** when proposing a profile change; the card shows the section and its before → after; committing edits that section of `profile.md`. The user can also hand-edit any section directly. The assistant proposes updates only when an input genuinely reveals something (a stated goal, a shifted priority) — not every turn, to avoid churn.

---

## 6. Global Model Switch

- A single selector in the UI — **Haiku / Sonnet / Opus** (or fast / balanced / max) — applied to the whole app for the next turn.
- Tier names map to concrete model strings **in config**, so the lineup changing is a config edit.
- Uses the existing provider abstraction (already config-driven and verified config-only in M10); v3 just makes the choice **UI-driven per turn** instead of static.
- **Tradeoff you've accepted by choosing global:** running the cheapest tier on a heavy planning/gap-analysis turn gives a weaker answer. That's fine — it's user-controlled; escalate to a stronger tier for the strategic turns. Cost stays trivial at personal volume.

---

## 7. Broadened Input + Gap Analysis

### 7.1 Any life input
The assistant's extraction behavior generalizes from "pull stated action items from meeting notes" to "surface potential todos and goal-updates from any input, **inferring** what might matter where nothing is explicitly stated." This is mostly a prompt shift. Expect it to be **fuzzier and more generative** — "I met someone interesting" states no action, so the assistant moves from *extracting what's stated* to *inferring what might matter*, with lower precision. That's acceptable because the advisory gate catches bad suggestions — but lean on the gate and expect more speculative proposals.

### 7.2 Gap analysis
On request ("what's missing given my goals?", "review my plan"), the assistant reasons over the **todo list** against the profile's **goals and current focus** and flags gaps and misalignment — e.g. a stated goal with no todos serving it, or daily work that has drifted from stated focus. Both contexts are already injected (§4.4), so this is primarily a prompt/capability plus (optionally) a dedicated affordance — a "Review my plan against my goals" action that seeds the turn. Output is conversation (with proposals only if the user accepts a suggested addition), labeled as the assistant's reasoning, not ground truth.

---

## 8. Data Model / Architecture Additions

- **Profile structure:** `profile.md` stays a human-editable markdown file but gains a defined **section schema** (the §5 sections, keyed by header) so the assistant can target updates and the UI can render sections. A light parse (by markdown header) maps file ↔ sections.
- **Proposals gain a `target`** (`todo` | `profile`) and, for profile proposals, a **section reference**. (v2 proposals were todo-only.)
- **Model selection** becomes a per-turn setting surfaced in the UI, resolved through the config tier→model map.
- **Surface merge:** the extraction/review endpoint and the chat endpoint unify — extraction routes through the assistant pipeline as a text-input turn. `extract.md` and `chat.md` may merge into a single assistant prompt with an extraction mode, or `chat.md` becomes primary; keep prompts as editable md files.

Keep the store and profile thin modules (as before) so these additions stay isolated.

---

## 9. Project Layout Additions

```
prompts/
├── assistant.md         # the unified assistant prompt (conversation + extraction mode
│                        #   + two proposal targets + gap-analysis behavior)
└── (extract.md / chat.md folded in or kept as includes)
src/meeting_notes_todos/
├── profile.py           # + section parse/render, + per-section proposed updates
├── chat/                # + route proposals by target (todo | profile:section)
└── config.toml          # + model tier→string map, + default tier
web/
├── app.py               # merge extraction + chat endpoints; add model-selector state
└── static/index.html    # one chatbox (chat + paste-notes), model selector,
                         #   profile rendered as markdown w/ section update cards
```

---

## 10. Milestones (M11–M15)

- **M11 — Profile as structured north star.** Implement the §5 must-have sections (section schema over `profile.md`); render the profile as markdown; extend M9 so the assistant proposes updates to a **named section** (approved before written, hand-editable); inject the full profile into every turn. *Foundation — goals must be first-class before gap analysis works.* *Acceptance:* profile shows the five sections, renders as markdown, is hand-editable, and the assistant can propose a section update that lands only after approval.
- **M12 — Unified conversational surface.** Merge the extraction/input box into the assistant chatbox; pasting notes becomes a chat turn that yields item proposals; conversation and extraction share one surface and one gate; **preserve source-beside-proposals** card rendering. *Acceptance:* one box handles both "paste notes → item proposals" and free conversation; the miss-catching layout and the advisory gate both hold.
- **M13 — Two-target proposals.** From the one box, the assistant proposes to the **todo list** (v2 ops) or the **profile** (M11 section updates); the UI routes and labels each card to its target with the right accept/edit/reject affordance. *Acceptance:* in one conversation the assistant proposes a todo *and* a profile-goal update, each surfaced as a card pointing at the right destination, each landing in the right store on accept.
- **M14 — Global model switch.** A single UI selector (Haiku/Sonnet/Opus) for the whole app; tier→string map in config; the selected model drives the next turn. *Acceptance:* switching the selector changes the model used next; adding/swapping a model is a config edit, not code.
- **M15 — Broadened input + gap analysis.** Generalize the assistant's extraction prompt to infer todos/goal-updates from **any** input (lean on the gate for the fuzzier proposals); add gap analysis over the list against the profile's goals/focus (both contexts already injected; add a "Review my plan against my goals" affordance). *Acceptance:* a non-meeting input (an idea, a person met) yields sensible proposals through the gate; "what's missing given my goals?" produces a grounded gap analysis that cites profile goals.

---

## 11. Acceptance Criteria for v3 (definition of done)

1. There is one conversational surface: pasting notes and free conversation happen in the same box, through the same gate.
2. The assistant holds real conversation (planning, advice, discussion) and only concrete changes become cards.
3. Proposals target either the todo list or the profile, are labeled/routed correctly, and land in the right store on accept.
4. The profile has the five must-have sections, renders as markdown, is hand-editable, and is updated mainly by assistant proposals that land only after approval.
5. Long-term goals live in the profile (not the todo list); the todo list stays near-term with its existing views.
6. A single global model selector (config-mapped) changes the model used, with no code change to swap models.
7. The assistant produces todos/goal-updates from general life input, not only meeting notes.
8. Gap analysis reasons over the list against the profile's goals and flags what's missing/misaligned, cited from the profile and labeled as reasoning.
9. Source-beside-proposals miss-catching and the advisory gate survive the surface merge.
10. All v1/v2 behaviors still pass; provider switching still works through the same abstraction.

---

## 12. Risks & Edge Cases

- **Surface merge regresses miss-catching** → explicitly preserve source-beside-proposals cards (§4.3, M12 acceptance).
- **Inferred extraction is fuzzier** → more speculative proposals; lean on the gate; watch precision on general input.
- **Profile bloat** (injected every turn) → keep sections tight and length-capped; the assistant proposes concise updates.
- **Profile churn** (assistant over-proposing updates) → propose only when an input genuinely reveals something, not every turn; the M9 gate is the backstop.
- **Gap analysis quality depends on profile quality** → the must-have sections mitigate garbage-in by ensuring goals are actually captured; still label output as the assistant's reasoning, not ground truth.
- **Cheap tier on a heavy planning turn** → weaker gap analysis; user-controlled, so escalate the tier for strategic turns.
- **Goal/todo confusion creeping back** → enforce the split in the prompt: goals are profile sections, todos are list items; never write a never-completing "goal" into the todo list.

---

## 13. Open Decisions

1. **The name.** The app has outgrown "MeetingNotes." Pick a new name when you like — it's a mechanical rename and blocks nothing. (Directions, not prescriptions: something around goals/direction/planning rather than notes.) Until then, the codebase name stays.
2. **Broadened scope** (§3.7) — locked as broaden-to-any-input; flip to stay meeting-shaped if you'd rather. Confirm.
3. **Persisted chat sessions** — deferred, but chat is now the primary surface, so worth deciding whether to pull it forward: in-session only (current) vs. saved conversations.
