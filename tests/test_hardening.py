"""v4 M19 hardening: rate limiting, secure cookies, session pepper, health,
config backend override. Key-free — no database needed."""

from __future__ import annotations

import meeting_notes_todos.web.app as web_app
from fastapi.testclient import TestClient

from meeting_notes_todos.auth import sessions
from meeting_notes_todos.config import StoreConfig, load_config
from meeting_notes_todos.web.app import app
from meeting_notes_todos.web.ratelimit import RateLimiter, bucket_for


# --- rate limiter logic --------------------------------------------------------


def test_rate_limiter_allows_then_blocks_then_resets():
    rl = RateLimiter()
    now = 1000.0
    assert all(rl.allow("k", limit=3, window=60, now=now) for _ in range(3))
    assert rl.allow("k", limit=3, window=60, now=now) is False  # 4th in window → blocked
    assert rl.allow("k", limit=3, window=60, now=now + 61) is True  # window elapsed → reset
    assert rl.allow("other", limit=3, window=60, now=now) is True  # separate key unaffected


def test_auth_paths_use_the_strict_bucket():
    assert bucket_for("/api/login") == "auth"
    assert bucket_for("/api/password") == "auth"
    assert bucket_for("/api/chat") == "api"


def test_middleware_enforces_limit_only_when_enabled(monkeypatch):
    web_app._rate_limiter.clear()
    monkeypatch.setattr(web_app, "RATE_LIMITS", {"auth": (2, 60), "api": (2, 60)})
    client = TestClient(app)

    monkeypatch.delenv("THROUGHLINE_RATE_LIMIT", raising=False)
    for _ in range(6):  # disabled → never throttled
        assert client.get("/api/me").status_code == 200

    web_app._rate_limiter.clear()
    monkeypatch.setenv("THROUGHLINE_RATE_LIMIT", "1")
    assert client.get("/api/me").status_code == 200
    assert client.get("/api/me").status_code == 200
    resp = client.get("/api/me")  # 3rd exceeds the (2, 60) api limit
    assert resp.status_code == 429 and "Too many" in resp.json()["detail"]
    web_app._rate_limiter.clear()


# --- secure cookies ------------------------------------------------------------


def test_secure_cookie_flag_follows_the_env(monkeypatch):
    monkeypatch.delenv("THROUGHLINE_SECURE_COOKIES", raising=False)
    assert web_app._secure_cookies() is False  # dev over http
    monkeypatch.setenv("THROUGHLINE_SECURE_COOKIES", "1")
    assert web_app._secure_cookies() is True  # prod over https


# --- session token pepper ------------------------------------------------------


def test_session_secret_keys_the_token_hash(monkeypatch):
    token = "some-opaque-session-token"
    monkeypatch.delenv(sessions.SESSION_SECRET_ENV, raising=False)
    plain = sessions._token_hash(token)

    monkeypatch.setenv(sessions.SESSION_SECRET_ENV, "prod-session-secret-A")
    keyed_a = sessions._token_hash(token)
    monkeypatch.setenv(sessions.SESSION_SECRET_ENV, "prod-session-secret-B")
    keyed_b = sessions._token_hash(token)

    # the secret genuinely changes the stored hash (so a DB dump alone is useless)
    assert keyed_a != plain and keyed_b != plain and keyed_a != keyed_b
    assert len(keyed_a) == 64  # HMAC-SHA256 hex


# --- health + config backend override -----------------------------------------


def test_healthz_is_public_and_ok():
    assert TestClient(app).get("/healthz").json() == {"status": "ok"}


def test_store_backend_env_override(monkeypatch, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[store]\nbackend = "markdown"\n')
    monkeypatch.delenv("THROUGHLINE_STORE_BACKEND", raising=False)
    assert load_config(cfg).store.backend == "markdown"  # file value

    monkeypatch.setenv("THROUGHLINE_STORE_BACKEND", "postgres")
    assert load_config(cfg).store.backend == "postgres"  # env wins in production
