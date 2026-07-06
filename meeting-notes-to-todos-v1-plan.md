# Meeting Notes → TODOs — v1 Build Plan

A spec for an app that turns unstructured meeting notes into clean, deduplicated, structured action items and merges them into a living TODO list. This document is the build brief; it intentionally contains no code.

---

## 1. Goal

Take raw meeting notes (typed, pasted, or already transcribed from voice) and produce structured action items, then merge them into an existing TODO list without creating duplicates and without resurrecting completed tasks. The LLM sits at the center and does the hard reasoning (what is an action vs. discussion, who owns it, when it's due, is this the same task we already have). Everything else is plumbing around that.

---

## 2. Scope and Non-Goals (v1)

**In scope**
- Text-in: paste/type notes, or point at a `.txt`/`.md` transcript file.
- LLM extraction of action items into a validated structured format.
- Within-batch deduplication (same action mentioned twice in one meeting).
- Cross-list deduplication and merge against the existing TODO list (NEW / DUPLICATE / UPDATE).
- A human review/confirm step before anything is committed.
- A persistent TODO store.
- LLM behavior fully configurable via markdown prompt files and a config file, with a provider abstraction so the model can be swapped (API ⇄ local) without code changes.

**Explicit non-goals (defer to later versions)**
- Audio capture or speech-to-text (notes arrive as text in v1; STT is a documented extension point).
- Multi-user, accounts, auth, sharing.
- Cloud sync or a hosted backend.
- Calendar / email / Slack / external task-tool integration.
- Mobile app.
- Recurring/automatic background syncing.

Keeping these out is deliberate — it keeps v1 small enough to actually finish and validates the core idea first.

---

## 3. Core Pipeline

```
raw notes
   │
   ▼
[1] Ingest        normalize input, attach meeting date + id
   │
   ▼
[2] Extract       LLM: notes → list of structured action items (JSON)
   │
   ▼
[3] Validate      parse JSON against schema, normalize dates, dedupe within batch
   │
   ▼
[4] Reconcile     pre-filter (fuzzy/embedding) → LLM adjudication vs. existing list
   │              → label each item NEW / DUPLICATE / UPDATE
   ▼
[5] Review        show a preview/diff; user accepts, edits, or rejects per item
   │
   ▼
[6] Merge         apply decisions to the TODO store; log what changed
```

**Stage detail**

1. **Ingest** — accept a text blob or a file path. Capture (or prompt for) the meeting date, because relative dates in step 3 resolve against it. Assign a session id.
2. **Extract** — one LLM call. Input: the raw notes + the meeting date. Output: a JSON array of action items. The prompt enforces the rules: extract only real action items (not background discussion), one action per item, imperative phrasing, and a source snippet for each. Soft or unowned phrasings ("we should…", "someone needs to…", "nobody owns this yet") still count as action items — a missing owner is never a reason to drop one. The model never invents an owner, and it does **not** compute due dates: it copies the deadline *phrase* exactly as written (e.g. "by Friday") into `due_date_text` for the Validate stage to resolve.
3. **Validate** — parse and schema-check the JSON; on malformed output, run one repair retry. Resolve due dates **deterministically in code**: a date library turns each `due_date_text` phrase into an absolute `due_date` relative to the meeting date (nearest upcoming occurrence). Vague or unparseable phrases leave `due_date` empty rather than guessing; the raw phrase is retained in `due_date_text`. Collapse duplicates that appear within this single batch.
4. **Reconcile** — for each new item, find candidate matches in the existing list cheaply first (fuzzy string match in v1; embeddings optional later), then ask the LLM to adjudicate only the candidates: is this the *same* task (DUPLICATE), the same task with new info like an added due date or owner (UPDATE), or genuinely new (NEW)? Sending only candidates — not the whole list — is what keeps token cost and latency low.
5. **Review** — present the proposed changes as a clear preview: new items to add, existing items to update (with a before/after), and duplicates being skipped. The user can accept all, edit any field, or reject individual items. Crucially, show the **original source notes alongside** the proposed items so that *misses* are visible, and let the user **add a missed item by hand** — extraction can silently drop a real action, and reviewing only what the model proposed would never surface it. This human-in-the-loop step is important in v1 because extraction is fuzzy and it both prevents garbage accumulating and builds trust.
6. **Merge** — apply confirmed decisions to the store. NEW → append. UPDATE → patch the existing item (preserve its status and creation date). DUPLICATE → skip (log it). Never change a task already marked done/cancelled. Write a short change log entry for the session.

---

## 4. LLM Integration (the center of the app)

### 4.1 Provider abstraction
Define one internal interface — conceptually `complete(system_prompt, user_content, response_schema?) -> structured_response` — and implement it multiple times:
- **AnthropicProvider** (default): calls the Claude API; uses structured-output/tool-use to return JSON reliably.
- **LocalProvider**: talks to a local OpenAI-compatible endpoint (Ollama or vLLM on the 5090). Same interface; prompts for JSON and parses/repairs.
- (Optional) **OpenAIProvider** for parity/comparison.

The extraction, reconcile, and merge logic only ever call this interface. The active provider, model name, and parameters come from config (below), so switching providers is a config edit, not a code change. This single decision is what makes the cost question a non-blocker.

### 4.2 Prompts as markdown files
All LLM behavior lives in editable, version-controlled markdown files (this mirrors the "config in md files / Claude agents" pattern):
- `prompts/system.md` — global persona/rules for the assistant (tone, the "only real action items," "never invent owners/dates" rules, output-format contract).
- `prompts/extract.md` — the extraction task prompt; templated slots for `{notes}` and `{meeting_date}`.
- `prompts/reconcile.md` — the dedup/merge adjudication prompt; slots for `{new_item}` and `{candidate_items}`.

Use simple slot substitution (e.g. `{notes}`) so prompts are readable and tunable without touching code. Tuning the app's behavior = editing these files.

### 4.3 Structured output + validation
Define the action-item JSON schema once (see §5) and validate every LLM response against it. On a schema failure, do exactly one repair attempt (feed the error back), then surface a clear error rather than silently dropping data. Prefer the provider's native structured-output mode where available (Anthropic), fall back to prompt-for-JSON + parse for local models.

### 4.4 Cost controls (built in from day one)
- Default to a cheap model (Haiku-class) for extraction and reconciliation; only escalate if quality demands it.
- Reconcile against **candidates only**, never the full list, so input tokens stay flat as the list grows.
- Cap/chunk very long notes so a single huge meeting doesn't blow the context window.
- Leave hooks for batch processing and prompt caching (both large discounts) if volume ever grows.
- Log token usage per run so cost is observable.

### 4.5 Hosting recommendation
Develop against the Haiku API for speed and reliable structured output. Keep `LocalProvider` as a drop-in (on the 5090) for zero marginal cost, offline use, or keeping sensitive notes fully local. Decide per-use, not up front — that's the point of the abstraction.

---

## 5. Data Model

**ActionItem (the unit extracted and stored)**
- `id` — stable unique id
- `title` — short imperative ("Send the Q3 deck to Lei")
- `description` — optional context
- `status` — `todo` | `doing` | `done` | `cancelled` (default `todo`)
- `owner` — optional; only if stated
- `due_date_text` — the raw deadline phrase as written (e.g. "by Friday"); kept for display and as a fallback when resolution yields nothing
- `due_date` — optional; resolved deterministically in code from `due_date_text` + the meeting date (nearest upcoming occurrence); null when the phrase is vague or unparseable
- `tags` / `project` — optional grouping
- `source_meeting_id` — which session it came from
- `source_snippet` — the note text it was derived from (for traceability)
- `created_at`, `updated_at`

**Session (one ingest run)**
- `id`, `date`, `raw_notes`, `extracted_items`, `decisions_log` (NEW/DUPLICATE/UPDATE per item)

**Store format** — default to a single human-readable, git-friendly markdown file (`todos.md`, e.g. checkbox list with light metadata) as the source of truth. SQLite is the alternative if structured querying/scale becomes important; the storage layer should be a thin module so this can be swapped. (This is one of the two open decisions below.)

---

## 6. Tech Stack (suggested)

- **Language:** Python — strong LLM glue, good schema validation, fast to build.
- **Schema/validation:** a typed model layer (e.g. pydantic-style) for the ActionItem schema and config.
- **LLM clients:** Anthropic SDK + an OpenAI-compatible client for the local provider.
- **Pre-filter for reconcile:** fuzzy string matching in v1 (e.g. rapidfuzz); leave room for a local embedding model later.
- **Date parsing:** a relative-date library (e.g. `dateparser`) resolves deadline phrases against the meeting date in code, so the model never computes dates itself.
- **Interface:** a **local web app** — a small local backend (running the pipeline, LLM calls, and store) plus a minimal single-page UI, opened in the browser at `localhost`. It runs entirely on the developer's machine; there is no server and no hosting in v1.
- **Config:** environment variables + a `config.toml`.

The pipeline and LLM core are independent of the interface. The later phases (a hosted multi-user version, or a desktop wrap via Tauri/Electron) reuse this same backend rather than rebuilding it.

---

## 7. Suggested Project Layout

```
meeting-notes-todos/
├── prompts/
│   ├── system.md
│   ├── extract.md
│   └── reconcile.md
├── config.toml                 # provider, model, params, paths
├── src/
│   ├── providers/              # provider interface + Anthropic/Local impls
│   ├── pipeline/               # ingest, extract, validate, reconcile, merge
│   ├── store/                  # read/write the TODO store (markdown by default)
│   ├── models.py               # ActionItem / Session schemas
│   ├── review.py               # the preview/confirm UX
│   └── app entrypoint
├── data/
│   └── todos.md                # the living TODO list (source of truth)
├── tests/
└── README.md
```

---

## 8. Configuration

- `config.toml`: `provider` (anthropic | local | openai), `model`, generation params, `store_path`, paths to prompt files.
- API keys via environment variables, never committed.
- Editing prompts in `prompts/*.md` is the supported way to tune behavior — no code change required.

---

## 9. Build Milestones (order for Claude Code)

- **M0 — Scaffold:** project structure, config loading, the provider interface + `AnthropicProvider`, the three prompt files (stubs). Acceptance: a "hello" prompt round-trips through the provider from config.
- **M1 — Extract:** notes → validated `ActionItem[]` via `extract.md`, with schema validation and one repair retry. Acceptance: a sample meeting yields correct structured items; malformed output is caught and repaired.
- **M2 — Store:** read/write `todos.md`; append NEW items; preserve formatting and existing item status. Acceptance: items persist across runs and the file stays human-readable.
- **M3 — Reconcile + Merge:** candidate pre-filter → LLM adjudication → NEW/DUPLICATE/UPDATE → apply to store; never touch done/cancelled items. Acceptance: re-running the same meeting adds nothing; a meeting that adds a due date to an existing task produces an UPDATE, not a duplicate.
- **M4 — Review UX:** the preview/diff with per-item accept/edit/reject before commit, shown **beside the original source notes**, plus a way to **add a missed item by hand**. Acceptance: user can reject one item and only the rest are written; a real action the model missed can be added during review.
- **M5 — Polish:** token-usage logging, `LocalProvider` wired to a local endpoint, README with setup + how to edit prompts. Acceptance: flipping `provider` in config switches models with no other change.

*(Future, out of v1 scope: audio→text ingest, embeddings-based reconcile, external task-tool sync, mobile.)*

---

## 10. Acceptance Criteria for v1 (definition of done)

1. Paste or point at meeting notes and get structured action items back.
2. Items follow the rules: imperative, one action each, no invented owners/dates, source snippet attached.
3. Re-ingesting the same notes adds zero duplicates.
4. New info about an existing task updates it in place rather than duplicating it.
5. Completed/cancelled tasks are never modified or re-added.
6. Nothing is written until the user confirms; per-item reject works.
7. The TODO store is persistent and human-readable.
8. Switching the LLM provider/model is a config-only change.
9. Behavior can be tuned by editing the markdown prompt files alone.
10. Relative due dates resolve correctly in code (computed from the meeting date), and vague phrases (e.g. "this week") are left empty rather than guessed.
11. The review step shows the original source notes alongside the proposed items and supports adding a missed item by hand.

---

## 11. Risks and Edge Cases

- **Hallucinated owners** → the prompt forbids inventing an owner; the confirm step is the backstop. Due dates are no longer a hallucination risk: the model only emits the deadline *phrase* and the date is computed in code.
- **Over-eager dedup merging distinct tasks** → adjudicate with the LLM on candidates and show updates in the preview so the user can catch bad merges.
- **Completed tasks reappearing** → reconcile must read status and exclude done/cancelled from being re-added or overwritten.
- **Ambiguous relative dates** → the model copies the deadline phrase verbatim into `due_date_text`; a date library resolves it deterministically against the meeting date (nearest upcoming occurrence). Vague/unparseable phrases leave `due_date` empty rather than guessing, with the raw phrase retained for display.
- **Missed (under-extracted) items** → the model can silently drop a real action (e.g. an unowned "we should…"); the extract prompt is tuned so soft/unowned items still count, and the review step shows the source notes beside the proposals so a human can catch and add what was missed.
- **Very long meetings** → chunk notes before extraction; merge the per-chunk item lists.
- **Malformed LLM JSON** → schema validation + one repair retry + a clear error rather than silent data loss.
- **Privacy** → meeting notes can be sensitive; the local-provider option keeps everything on-device, and API usage/data handling should be noted in the README.

---

## 12. Decisions (locked for v1)

1. **Form factor:** a **local web app** — a small local backend plus a minimal browser UI at `localhost`, running only on the developer's machine. No server, no hosting. Going multi-user (a hosted version) and wrapping it as a desktop app (Tauri/Electron) are explicit *later phases* that reuse this same backend; they are out of scope for v1.
2. **TODO store:** a single human-readable, git-friendly **markdown file** (`todos.md`) as the source of truth. SQLite is the swap-in if querying or scale ever matters; the store stays a thin module so that change is isolated.
3. **Input:** **text only** in v1 (typed, pasted, or a transcript file). Audio → text is a documented extension point, not part of v1.
4. **LLM provider:** a **Haiku-class API model** as the default, with a **local-model drop-in** (e.g. on the developer's own GPU via Ollama/vLLM) available through the provider abstraction with no other code changes.
5. **Safety:** **human confirmation is required** before anything is written to the store.
