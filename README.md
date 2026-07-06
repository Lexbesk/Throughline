# Meeting Notes → TODOs

Turn unstructured meeting notes into clean, deduplicated, structured action items
and merge them into a living TODO list. See `meeting-notes-to-todos-v1-plan.md`
for the full build brief and `meeting-notes-to-todos-v2-plan.md` for the v2
extension (M6–M10).

Status: **v1–v3 complete (M0–M15) · v4 in progress — M16–M17 done** (see
`throughline-v4-plan.md`: hosted, closed-group, per-user). M16 lays the
database foundation: the store and profile can now run on **Postgres with
every row scoped by `user_id`** (hard isolation, tested), behind the same thin
interfaces — select it with `[store] backend = "postgres"` plus a standard
`DATABASE_URL` (local dev: `docker compose -f deploy/docker-compose.yml up -d`;
production DB lands at M19). M17 adds **accounts & login** in postgres mode:
argon2id-hashed passwords, server-side sessions (opaque HttpOnly cookie, only
the token's hash stored, revocable), a login-only UI — **no signup path** —
and admin provisioning via `meeting-notes-todos user add|passwd|list`. Every
data route requires a session and operates on the logged-in user's rows; the
model-tier switch is per-user; `POST /api/password` changes one's own password
and revokes other sessions. The local markdown backend remains the default and
needs no login (single-user mode). Earlier: (see
`meeting-notes-to-todos-v3-plan.md`: the pivot from capture to planning) — one
conversational surface turns meeting notes and plans into a living task list,
behind an advisory accept/reject gate. M11 makes the profile the structured
north star: five must-have sections, rendered as markdown, updated mainly by
assistant proposals that target one named section at a time. M12 merges the
extraction box into the assistant: pasting notes is a chat turn that yields
item cards with the pasted source rendered beside them. M13 makes proposals
explicitly two-target: every card carries a `target` (`todo` | `profile`) and
a "→ Task list" / "→ Profile" destination label, and one input can feed both —
a stated goal updates the profile while its first concrete step lands on the
list. M14 adds the global model switch: a header selector (Haiku 4.5 through
Fable 5, plus GPT) that drives the next turn app-wide, resolved through the `[llm.tiers]`
map in `config.toml` — swapping the lineup is a config edit, not code. The
switch is session-scoped (a restart falls back to the config's startup model),
and the usage log records the resolved model per run. M15 broadens the input:
the assistant works on **any life input** — an idea, a person met, a situation —
extracting *stated* actions faithfully and proposing *inferred* possibilities
(clearly marked, filtered by the gate); and adds **gap analysis**: on request
(or via the "Review plan vs goals" button) it reviews the task list against
the profile's Long-term goals and Current focus, citing the specific goal
lines and flagging uncovered goals, drift, and imbalance — presented as its
reading, not ground truth. Swappable LLM provider (Anthropic, OpenAI, or a
local model) and per-run token logging included. v2 added: the **management view** (M6 — check-off,
cancel/restore, priority, manual ordering, plus Completed and Deleted lists),
the **reasoning display** (M7 — per-item rationale beside the review cards),
the **chat assistant** (M8 — every change staged as an accept/edit/reject
card), and the **user profile** (M9 — a short `data/profile.md` injected into
extraction and chat, with model-proposed updates gated behind your approval).
M10 ties it together with an end-to-end regression test (`tests/test_e2e.py`)
covering the full loop: notes → extract → review → list → chat-refine → done.

The web app is the intended interface (§12.1): `meeting-notes-todos serve`, then
open http://127.0.0.1:8000. The CLI below is the same pipeline for scripting/tests.

## Setup (conda)

```bash
conda env create -f environment.yml
conda activate meeting-notes-todos
```

This creates the `meeting-notes-todos` env and installs the package (editable)
plus dev tools.

## Configure the API key

The Anthropic SDK reads `ANTHROPIC_API_KEY` from the environment. Either export it,
or copy `.env.example` to `.env` and fill it in (`.env` is gitignored and loaded
automatically):

```bash
cp .env.example .env   # then edit .env and paste your key
```

## Commands

```bash
meeting-notes-todos serve                       # the web app: review + task list + chat + profile
meeting-notes-todos hello                       # round-trip check
meeting-notes-todos extract <file> [--json]     # extract & print (read-only)
meeting-notes-todos ingest  <file> [--commit]   # extract → reconcile → merge (CLI)
meeting-notes-todos list                        # print the store
meeting-notes-todos usage                       # token-usage totals
```

Pass `--date YYYY-MM-DD` to set the meeting date for resolving relative deadlines
(defaults to today). Notes can be a file path or piped on stdin.

### Ingest = the pipeline

`ingest` runs extraction, then **reconciles** each new item against what's already
in `data/todos.md`:

- A cheap fuzzy pre-filter finds candidate matches, then the LLM adjudicates only
  those candidates as **NEW**, **DUPLICATE** (skip), or **UPDATE** (the same task
  with new info — fills in an owner or a due date in place).
- Items already marked `done`/`cancelled` are never modified or re-added.

By default `ingest` prints a **dry-run preview** and writes nothing; pass
`--commit` to apply. (Full per-item accept/edit/reject review is M4.)

```bash
meeting-notes-todos ingest samples/standup.md            # preview only
meeting-notes-todos ingest samples/standup.md --commit   # apply
meeting-notes-todos ingest samples/standup.md --commit   # again → adds nothing
```

How deadlines work: the model copies the deadline *phrase* (e.g. "by Friday") and
the app resolves it to a real date in code (nearest upcoming occurrence); vague
phrases like "this week" are left unresolved rather than guessed.

## Web app (one conversational surface, v3 M12)

```bash
meeting-notes-todos serve            # then open http://127.0.0.1:8000
```

The page is a task list + profile on the left and **the assistant** on the
right — one box for everything. **Paste meeting notes (or any raw text)** and
the assistant extracts action items as NEW cards, rendered **beside your pasted
source** with each card carrying the exact snippet it came from — so misses
stay catchable, and anything the assistant deliberately skipped is called out
in its reply. Or just talk: planning questions get plain-text answers with no
cards. Nothing is written until you accept a card (the advisory gate). The
assistant checks pasted items against the live list, so re-pasting the same
notes fills gaps or gets a "already tracked" instead of duplicates. The
backend is FastAPI; everything runs on your machine
(`src/meeting_notes_todos/web/`).

Dates: the assistant copies deadline *phrases* ("by Tuesday") and the app
resolves them in code against **today**. For old, dated meeting notes, the CLI
`ingest --date` path is still the precise route. (The v2 `/api/review` +
`/api/commit` endpoints remain available for API use; the web UI no longer
uses them.)

Beside the assistant is the **task list** (v2 M6): every stored task with its
status, owner, due date, and priority. Check a task off, cancel/restore it, set a
priority (shown as `!high` / `!medium` / `!low` in the markdown line too), and
reorder with the ↑/↓ buttons. These are your own direct edits — not model
proposals — so they save to `data/todos.md` immediately, and the file stays
human-editable as before (line order is the list order; the checkbox glyph is the
status). The active list shows `todo`/`doing`/`cancelled` items; checking a task
off moves it to a **Completed** list below (with *uncomplete* to bring it back
and *delete* to soft-delete it into the Deleted list), and deleting moves it to
the **Deleted** list (restore / delete forever). Done, cancelled, and deleted
tasks are never resurrected by a later ingest/review.

## How the assistant works (v2 M8, advisory-first; unified in v3 M12)

The model gets seven proposal *tools* (`propose_new`, `propose_update`,
`propose_delete`, `propose_merge`, `reprioritize`, `propose_complete`,
`propose_profile_update`). The tools never execute — each call is staged as an
accept/edit/reject card, routed to one of two destinations (v3 M13): todo ops
carry `target: "todo"` and land in `todos.md`; profile updates carry
`target: "profile"` plus a section name and land in `profile.md`. Goals are
directions, not checkboxes — the prompt enforces that long-term goals go to
the profile, never onto the list. Every id a proposal references is validated against
the live list (hallucinated or stale ids are dropped and reported), the current
list and profile are re-injected every turn so the assistant never reasons
about a stale snapshot, and done/cancelled/deleted items are never modified.
Its explanations are its message text — stated reasoning, not a trace. The
conversation lasts for the page session (reload = fresh chat). The system
prompt, advisory contract, and extraction mode live in `prompts/assistant.md`.

Provider support: Anthropic uses native SDK tool use; `local`/`openai` providers
use OpenAI-compatible function calling through the same abstraction — switching
remains config-only. For local models whose tool calling is unreliable, the
documented fallback is a single structured JSON response (`{"message", 
"proposals"}`) parsed with the same repair discipline as extraction; only the
chat module would change.

## User profile (v2 M9, structured north star in v3 M11)

A human-editable `data/profile.md` living beside `todos.md` — the app's north
star. It carries five must-have sections (markdown `##` headers, seeded for new
profiles, user-extendable): **Long-term goals** (durable directions — not
checkboxes), **Current focus** (medium-term themes bridging todos and goals),
**Priorities & values** (how you weigh competing demands), **Working style &
personality** (how the assistant should tailor its delivery), and **Context &
constraints** (durable situational facts and hard limits). The Profile panel
renders it as markdown, with an Edit toggle for hand-editing the raw file. It
is injected in full into both **extraction** (so "I'll draft the rubric" gets
*you* as the owner) and **chat** (so planning advice is about you, not a
generic user).

The assistant proposes profile updates during chat, targeting **one named
section at a time** — when you state a durable fact ("my long-term goal
is…"), a PROFILE card appears naming the section, showing its current text and
the proposed replacement (editable). Nothing is written until you accept, and
accepting rewrites only that section — the approval gate remains the guard
against drift and junk accumulation. Section bodies proposed by the assistant
are length-capped to keep the profile tight, since it rides along on every
model call.

## Run the tests (no API key needed)

```bash
pytest
```

## Switching the LLM provider (config-only)

Routing is centralized in the provider factory and the **model string picks the
vendor**: any `claude-*` model → Anthropic, any `gpt-*` model → OpenAI. No code
changes to switch (acceptance §10.8):

- **Anthropic** (default) — Claude via the Anthropic SDK; reads `ANTHROPIC_API_KEY`.
- **OpenAI** — GPT models via the OpenAI SDK; reads `OPENAI_API_KEY` from `.env`
  (optional — only needed if you select a GPT model/tier). Uses native strict
  structured output (`chat.completions.parse`) and function calling; the
  pipeline's one-repair-retry stays as the safety net. Single-user/local only.
- **`local`** — any OpenAI-compatible endpoint (Ollama, vLLM). Point `base_url` at it:
  ```toml
  [llm]
  provider = "local"
  model = "llama3.1"
  base_url = "http://localhost:11434/v1"
  ```
  An explicit local setup always wins over model-name routing (so Ollama's
  `gpt-oss` stays local). Local models have no native structured output, so the
  prompts ask for JSON and the pipeline parses/repairs. Needs the endpoint
  running (e.g. `ollama serve`); the key comes from `LOCAL_API_KEY` if needed.

The global model switch's `[llm.tiers]` map includes both vendors — Haiku 4.5,
Sonnet 5, Opus 4.8, Fable 5 (Anthropic's Mythos-class tier above Opus),
GPT 4.1 mini, and GPT 5.5 — so the header selector swaps vendors mid-session
through the same abstraction. Tier names are display labels; each maps to a
concrete model string in config.

## Token usage

Every LLM run appends a line to `data/usage.jsonl`. See the totals with:

```bash
meeting-notes-todos usage
```

## Tuning prompts

Prompt behavior lives in `prompts/*.md` — edit those to tune the assistant; no code
change required. They use simple `{slot}` placeholders: `extract.md` takes
`{notes}`, `{meeting_date}`, and `{profile}`; `reconcile.md` takes `{new_item}`
and `{candidate_items}`; `assistant.md` (the unified assistant prompt:
conversation + extraction mode + advisory contract) takes `{task_list}`,
`{profile}`, and `{today}`. Which file each stage loads is set in `config.toml`
under `[prompts]`.
