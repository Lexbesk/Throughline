# Throughline — v4 Build Plan (Hosted, Closed-Group)

v4 **extends** the shipped app (v1 = M0–M5, v2 = M6–M10, v3 = M11–M15, plus the standalone OpenAI/GPT provider work). It does not rebuild the core: the provider abstraction, the assistant, the extract/reconcile/merge pipeline, the profile-as-north-star, the reasoning panel, the advisory gate, gap analysis, and the global model switch all stay.

**The pivot:** every version so far quietly assumed four things — **local, single-user, one `.env`, files on disk**. v4 breaks all four at once to make the app a **hosted, invite-only web app for a small trusted group**, now named **Throughline**. This is the biggest architectural step of the four, because deployment and a database are territory the app has never touched. GPT support (already built) rides on top of it.

Milestones continue as **M16–M20**. This document contains no code.

---

## 1. What v4 is

- **Hosted.** Throughline moves from a local process at `localhost` to an always-on, deployed website reachable at a real HTTPS URL.
- **Closed group.** You provision the accounts. **Signup is disabled** — login only. No public registration, no abuse surface.
- **Per-user API keys.** Each user provides their **own** API key(s); requests run on that user's key. Yours included — your key is used only for you. This is deliberate isolation, chosen over a shared key so no one else's usage touches your API account.
- **Per-user data in a database.** Each person's todos and profile are isolated rows that can never cross accounts. The flat files finally become a real database.
- **Baseline hardening.** HTTPS, secure sessions, rate limiting, no cross-user leakage — lighter than a public SaaS, not zero, because it's on the open internet and notes/profiles are sensitive.

The right mental model: **multi-user for a trusted few**, the smallest honest version of multi-user — not a polished public product.

---

## 2. Scope and Non-Goals (v4)

**In scope:** the five items in §1.

**Explicit non-goals (deferred):**
- **Public signup** and its whole abuse/bot/verification surface — removed on purpose.
- **Payment / billing / metering** — none. Per-user keys mean each person pays their own provider directly.
- **Team sharing / collaboration** — data is strictly per-user; no shared lists or profiles.
- **Mobile apps, enterprise SSO, Mac App Store** — out.
- Still deferred: agentic auto-apply as default, embeddings-based dedup. (**Persisted chat sessions** is now cheap given a database exists — see §15 to decide whether to pull it in.)

---

## 3. Locked decisions (recommendations — each a one-line flip, see §15)

1. **Platform: Fly.io** *(decided — chosen deliberately as the most educational of the managed options)*. Fly runs the app as containers, with automatic HTTPS and secret management via `flyctl`. Deploy artifacts (Dockerfile, `fly.toml`) are written by Claude Code; the actual deploy (`fly` commands, app/DB provisioning, secrets) is run by you — that hands-on part is the point.
2. **Postgres for app data, not files.** Fly Machines have an **ephemeral root filesystem** (persistent Fly Volumes exist, but relational multi-user data belongs in Postgres, not on a single machine's disk). **Development uses a local Postgres in Docker** (free, offline, no Fly needed); the **production database is chosen at M19** (see §8 and §15) — not a prerequisite for starting M16.
3. **Login-only session auth, no signup.** Session-based auth with securely hashed passwords; you provision accounts; no registration flow. (Simpler and sufficient because the group is closed — see §15 for the managed-auth-service alternative.)
4. **Per-user, per-provider encrypted keys.** Keys are stored per user and per provider (Anthropic and/or OpenAI, since both providers now exist), encrypted at rest.
5. **You are a user too.** Your key is stored the same encrypted, per-user way; your requests use only your key. This is what makes "my Claude only for myself" hold by construction.

---

## 4. The Architecture Shift (the four assumptions that break)

This section is the framing for everything below.

- **Local → hosted.** A deployed, always-on service with a domain and HTTPS. The "server moment" flagged since the very first app-vs-webpage conversation finally arrives.
- **Single-user → multi-user.** Every piece of data and every request now belongs to a specific user; nothing may leak across accounts.
- **Files → database.** `todos.md` / `profile.md` become per-user rows in Postgres. The thin store/profile modules were built for exactly this swap.
- **One `.env` → per-user encrypted keys.** Instead of one key in one env file, each user's key is stored encrypted in the database and read for that user's requests.

The provider abstraction, assistant, and pipeline don't change — they just now run **on behalf of a logged-in user, with that user's key and that user's data.**

---

## 5. Accounts & Login (M17)

- **Session-based auth.** On login, verify a securely **hashed** password (argon2 or bcrypt — never store plaintext), establish a session (secure, http-only cookie over HTTPS).
- **No signup.** There is no public registration path. Accounts are **provisioned by you** via an admin seed/CLI command (username + initial password; user can change it after first login). This is the whole "disable signup so we don't get attacked" instinct, made concrete.
- **Every route is authenticated.** All app endpoints require a valid session and operate strictly on the logged-in user's data. Unauthenticated requests are rejected.

---

## 6. Per-User Data & the Database (M16)

- **Migrate the store and profile from flat files to Postgres**, with **every row scoped by `user_id`**.
- **Hard isolation.** Data access is always filtered by the current user — a query can only ever return the logged-in user's todos/profile. This is the cardinal rule; treat a cross-user leak as a critical failure.
- **The store/profile modules swap their backend** (files → DB) behind the same internal interface, so the pipeline, assistant, and UI logic are unchanged. This is the payoff of keeping those modules thin since v1.
- Per-user scoping applies to everything that was per-app before: todos (with their statuses and views), the profile and its sections, and — if included (§15) — saved chat sessions.

---

## 7. Per-User API Keys (the security-sensitive core)

This is the part you specifically want, and the part to build carefully.

- **Each user supplies their own key(s)**, per provider (Anthropic and/or OpenAI). The model selector + tier→model map determines which provider a turn uses, and the app reads **that user's key for that provider**.
- **Encrypted at rest.** Keys are stored encrypted with symmetric encryption; the **encryption master key lives in the platform's secret manager**, never in the repo or the database. Plaintext keys are never persisted.
- **Never exposed.** Keys are never written to logs, never returned to the client after entry, and shown only **masked** in the UI (e.g. last 4 characters). In transit they're protected by HTTPS.
- **Key-management UI.** Each user can add/update/remove their own key(s) in a settings panel; a user can only ever see or change **their own** keys.
- **No-key handling.** A user with no key for the selected model's provider is **prompted to add one and LLM actions are blocked gracefully** — never a raw error, never a silent fall-through to someone else's key.
- **Optional: validate on entry.** A cheap test call when a key is saved, to catch typos early. Nice-to-have.
- **The isolation guarantee.** Because every request uses the requesting user's key, your API account is used only by you, and no user's usage or content touches another's. This is "my Claude only for myself," enforced structurally.

**Honest note on responsibility:** storing other people's API keys means holding other people's secrets. Encryption-at-rest + platform-managed master key + no-logging + HTTPS is the correct baseline and is appropriate for a **small, trusted, invite-only group** — which is exactly the scope. Keep it that scope.

---

## 8. Deployment & Hardening (M19)

- **Deploy to Fly.io.** Claude Code writes the **Dockerfile** and **`fly.toml`**; you run the deployment with **`flyctl`** (create the app, deploy, provision the DB, set secrets). Fly provides **automatic HTTPS**. This milestone has hands-on CLI/dashboard work that only you can do — plan to walk through it together.
- **Production database (decide here).** Options, in order of lean for this scale: (a) **Neon** or another external managed Postgres — cheapest, managed backups, connect over the network (my lean; latency is irrelevant since LLM calls dominate); (b) **Fly Managed Postgres** (`fly mpg`) — all-on-Fly, HA + backups + pooling, ~$38/mo Basic; (c) **avoid unmanaged Fly Postgres for real user data** — you'd own backups/disaster recovery and Fly won't support it. Whichever you pick, the app connects via a standard `DATABASE_URL`, so the choice is a connection-string swap, not a code change.
- **Secrets via `fly secrets set`** — `DATABASE_URL`, encryption master key, session secret. Never in the repo, never in the database.
- **Baseline hardening:** secure session cookies; **rate limiting** on auth and API endpoints; input validation; strict per-user data filtering (no cross-user leakage); no secrets in logs; dependencies kept current.
- Config keeps the tier→model map (v3) so provider/model choice stays a config concern.

---

## 9. Onboarding & Data Migration (M20)

- **New-user first run:** a fresh account starts with the **must-have profile sections seeded** (the v3 template) and an empty todo list — an empty-state onboarding rather than a blank void.
- **Migrate your own data:** a one-time migration loads your existing local `todos.md` / `profile.md` into **your** account, so nothing you've built is lost when the app goes hosted.

---

## 10. Data Model / Architecture Additions

- **`users`** — id, username, hashed password, timestamps.
- **Per-user scoping** — a `user_id` foreign key on todos, profile (and its sections), and saved chats if included; all access filtered by it.
- **`user_api_keys`** — per user, per provider, **encrypted** key material (never plaintext), timestamps.
- **Sessions** — server-side/session-cookie mechanism for login state.
- **Store/profile backends** move from file I/O to DB queries behind the existing thin interfaces.

---

## 11. Project Layout Additions

```
src/throughline/                # (renamed package; see §15 note on rename timing)
├── db/                         # models, migrations, connection (Postgres)
├── auth/                       # session auth, password hashing, admin seed/CLI
├── keys/                       # per-user key storage: encrypt/decrypt, per-provider lookup
├── store/  profile/            # same interfaces, DB-backed now (was file-backed)
└── chat/  providers/           # unchanged, but run per-user with the user's key
web/
├── app.py                      # auth guards on all routes; scope everything to current user
└── static/                     # + login page, + API-key settings panel (masked)
Dockerfile                      # container recipe (Claude Code writes)
fly.toml                        # Fly app config (Claude Code writes)
deploy/                         # docker-compose for LOCAL Postgres in dev; secret templates (no real secrets)
```

---

## 12. Milestones (M16–M20)

- **M16 — Database + per-user data foundation.** Migrate store and profile from flat files to Postgres, every row scoped by `user_id`, with hard isolation. **Developed against a local Postgres in Docker — no Fly account or hosted DB needed to start.** Connect via a standard `DATABASE_URL` so the same code targets local Postgres now and the production DB at M19. *Foundation — nothing multi-user works without it.* *Acceptance:* todos and profile persist per user in the local DB; a query for one user can never return another user's data (tested).
- **M17 — Accounts & login (no signup).** Session auth with hashed passwords; login-only; an admin seed/CLI to provision accounts; all routes require a session and operate on that user's data. *Acceptance:* you can provision an account; a user logs in and sees only their own data; unauthenticated requests are rejected; there is no signup path.
- **M18 — Per-user encrypted API keys.** Per-user, per-provider key storage encrypted at rest (master key from platform secrets); a masked key-management UI; the provider reads the current user's key for the selected model's provider; graceful no-key handling. *Acceptance:* a user sets a key, it's stored encrypted (never plaintext, never logged, never returned to client); their requests use their key, another user's use theirs, yours use only yours (isolation verified); a keyless user is prompted, not errored.
- **M19 — Deploy to managed platform + hardening.** Deploy with managed Postgres and automatic HTTPS; secrets in platform config; baseline hardening (secure sessions, rate limiting, input validation, no cross-user leakage, no secrets in logs). *Acceptance:* the app is reachable at a real HTTPS URL; two accounts each see only their own data over the network; secrets live in platform config, not the repo; rate limiting is active.
- **M20 — Onboarding & data migration.** New-account first run seeds the must-have profile sections and an empty list; a one-time migration loads your existing local data into your account. *Acceptance:* a fresh account starts with the seeded profile template; your existing todos/profile appear under your account after migration.

---

## 13. Acceptance Criteria for v4 (definition of done)

1. Throughline runs as a hosted website at an HTTPS URL, reachable by invited users.
2. Users log in; there is no public signup; you can provision accounts.
3. Each user's todos and profile are isolated in the database; no cross-user data access is possible.
4. Each user supplies their own per-provider key(s), stored encrypted, shown only masked, never logged or returned to the client.
5. Every LLM request uses the requesting user's key; your API account is used only by you.
6. A user with no key for the chosen provider is prompted and blocked gracefully, never falling through to another key.
7. New accounts onboard with the seeded profile sections; your existing local data is migrated into your account.
8. Secrets (DB URL, encryption key, session secret) live in platform config, not the repo.
9. Baseline hardening is in place: secure sessions, rate limiting, input validation, no secrets in logs.
10. All prior behavior (assistant, pipeline, profile, gap analysis, model switch, both providers) still works — now per-user.

---

## 14. Risks & Edge Cases

- **Cross-user data leakage** — the cardinal sin. Every query filters by the current user; test explicitly that user A can never see user B's data.
- **API-key exposure** — encrypt at rest, master key in platform secrets, never log, never return to client, mask in UI, HTTPS in transit. Treat a leaked key as a critical incident.
- **Secrets in the repo** — never commit the encryption key, session secret, or DB URL; use platform secret management.
- **Ephemeral-filesystem trap** — do not use on-disk files/SQLite on the managed platform; Postgres only (this is why M16 is first).
- **Session security** — secure, http-only cookies; sensible expiry; protect auth/session endpoints.
- **Holding others' keys is a real responsibility** — acceptable with the baseline above **for a small trusted group**; keep the group closed and trusted.
- **Migration data loss** — back up your local `todos.md`/`profile.md` before the one-time migration.
- **Platform lock-in / cost** — trivial at this scale, but keep DB access standard (plain SQL/ORM) so you're not welded to one platform.

---

## 15. Open Decisions

1. **Platform** — ✅ **decided: Fly.io.** ~~Render / Railway~~. *Remaining sub-decision (defer to M19):* production database — **Neon / external managed Postgres** (lean: cheapest, managed backups) vs. **Fly Managed Postgres** (`fly mpg`, ~$38/mo, all-on-Fly) vs. avoid unmanaged Fly Postgres for real user data. Not needed to start M16 (local Postgres in dev).
2. **Auth approach** — roll simple session auth (**locked lean**: least integration, sufficient for closed invite-only) vs. a managed auth service (Clerk/Auth0/Supabase — offloads security work, adds a dependency). Confirm or flip.
3. **Key scope** — per-user keys for **both** Anthropic and OpenAI (**locked lean**, since GPT support exists) vs. Anthropic only for now. Confirm.
4. **Account provisioning mechanism** — admin **CLI/seed script** (**locked lean**, simplest) vs. a minimal admin UI. Confirm.
5. **Persisted chat sessions** — deferred through v3, but a database now exists, so it's cheap to add as per-user saved conversations. Include in v4, or keep deferred? Your call.
6. **Rename timing** — the package/app becomes **Throughline**. Do the rename as part of M16, or keep the codebase name and rename separately? (Mechanical either way; noted so it's not forgotten.)
