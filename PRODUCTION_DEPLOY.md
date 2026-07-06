# Production Deploy Checklist — Throughline on Fly.io (v4 M19)

The deployment **artifacts** (`Dockerfile`, `fly.toml`, `.dockerignore`) and the
in-code hardening are done. This is the manual, hands-on part — **you** run these
steps. Nothing here has been executed for you.

Run everything from the repo root unless noted. Commands you must fill in are in
`<angle brackets>`.

---

## 0. One-time setup

- [ ] **Install flyctl** and sign in:
  ```bash
  brew install flyctl        # or: curl -L https://fly.io/install.sh | sh
  fly auth login
  ```
- [ ] Pick an app name and region. The name must be globally unique on Fly and
  must match `app = "…"` in `fly.toml` (edit it if you change the name).
  `fly platform regions` lists regions; `iad` (US-East) is the current default.

## 1. Create the app (does not deploy yet)

- [ ] ```bash
  fly apps create <your-app-name>
  ```
  Keep `fly.toml`'s `app` and `primary_region` in sync with what you chose.

## 2. Provision the production Postgres

Pick **one** (the app only needs a standard `DATABASE_URL`, so this is a
connection-string choice, not a code change):

- [ ] **Option A — Neon / external managed Postgres (recommended: cheapest,
  managed backups).** Create a project at neon.tech, create a database, and copy
  its pooled connection string (starts with `postgresql://…`, includes
  `?sslmode=require`).
- [ ] **Option B — Fly Managed Postgres** (`fly mpg`, ~$38/mo, all-on-Fly, HA +
  backups). Follow Fly's Managed Postgres docs to create a cluster and attach it;
  note the `DATABASE_URL` it gives you.
- [ ] **Do NOT** use unmanaged Fly Postgres for real user data — you'd own
  backups and disaster recovery.

Hold onto the resulting `DATABASE_URL` for step 3.

## 3. Generate and set the secrets

Generate the two app secrets locally (never commit these):

- [ ] Encryption master key (encrypts per-user API keys at rest):
  ```bash
  meeting-notes-todos keygen
  ```
- [ ] Session secret (pepper for session-token hashes) — any long random string:
  ```bash
  python -c "import secrets; print(secrets.token_urlsafe(48))"
  ```

Set all three secrets on Fly (injected as env vars at runtime — **never** in the
repo, `fly.toml`, or the database):

- [ ] ```bash
  fly secrets set \
    DATABASE_URL='<your-postgres-connection-string>' \
    THROUGHLINE_MASTER_KEY='<output of keygen>' \
    THROUGHLINE_SESSION_SECRET='<output of token_urlsafe>'
  ```
  Verify names (values stay hidden): `fly secrets list`.

> The non-secret settings (`THROUGHLINE_STORE_BACKEND=postgres`,
> `THROUGHLINE_SECURE_COOKIES=1`, `THROUGHLINE_RATE_LIMIT=1`) are already in
> `fly.toml`'s `[env]` — no action needed.

## 4. Deploy

- [ ] ```bash
  fly deploy
  ```
  This builds the image, runs the release command
  (`meeting-notes-todos initdb`, which creates the DB schema and fails the deploy
  if `DATABASE_URL` is wrong), then starts the app with automatic HTTPS.
- [ ] Watch it come up: `fly logs`. Confirm the health check passes
  (`fly status` shows the machine healthy).

## 5. Provision accounts on the production DB

There is **no signup** — you create every account. Run these against the
production database (they need the prod `DATABASE_URL`, so run them *in the
deployed machine*):

- [ ] Open a shell on the running app: `fly ssh console`
- [ ] Inside that shell:
  ```bash
  meeting-notes-todos user add <username>       # prompts for a password
  meeting-notes-todos user list                 # confirm the account exists
  ```
  Repeat `user add` for each invited person (including yourself). They can change
  their password after first login.

## 6. Smoke-test the live site

- [ ] Open `https://<your-app-name>.fly.dev` — you should see the **Sign in**
  overlay (no signup link).
- [ ] Log in with an account you provisioned. You start with an empty task list
  and the seeded five-section profile.
- [ ] Click **Keys**, add your own Anthropic and/or OpenAI key, then run a chat
  turn — it should succeed on *your* key.
- [ ] Log in as a second account in a private window; confirm it sees none of the
  first account's data or keys.

## 7. After it's live

- [ ] `fly secrets list` — confirm only the three secret **names** are present,
  no values anywhere in the repo.
- [ ] Back up the DB before the M20 one-time migration of your local data.
- [ ] To scale away cold starts, set `min_machines_running = 1` in `fly.toml`
  and redeploy.

---

## What the running app reads from the environment

| Variable | Secret? | Where it's read | Purpose |
|---|---|---|---|
| `DATABASE_URL` | **secret** | `db/connection.py` → `database_url()` | Postgres connection |
| `THROUGHLINE_MASTER_KEY` | **secret** | `keys/crypto.py` → `get_cipher()` | encrypts per-user API keys at rest |
| `THROUGHLINE_SESSION_SECRET` | **secret** | `auth/sessions.py` → `_token_hash()` | keyed hash (pepper) for session tokens |
| `THROUGHLINE_STORE_BACKEND` | no (in `fly.toml`) | `config.py` → `load_config()` | selects the Postgres backend |
| `THROUGHLINE_SECURE_COOKIES` | no (in `fly.toml`) | `web/app.py` → `_secure_cookies()` | `Secure` flag on session cookies |
| `THROUGHLINE_RATE_LIMIT` | no (in `fly.toml`) | `web/app.py` rate-limit middleware | enable throttling |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | — | env fallback only | **not used in hosted mode** — every request uses the user's stored key |
