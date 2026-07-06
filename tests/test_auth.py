"""Accounts & login tests (v4 M17).

Password hashing is tested without a database; the session/login flow runs
against the local Docker Postgres and is skipped when it isn't reachable.
The web tests exercise the real dependency chain (no store overrides): the
postgres backend is what switches authentication on.
"""

from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

import meeting_notes_todos.web.app as web_app
from meeting_notes_todos.auth import hash_password, provision_user, verify_password
from meeting_notes_todos.config import Config, StoreConfig
from meeting_notes_todos.db.connection import DEFAULT_DEV_URL, ensure_user, get_pool
from meeting_notes_todos.web.app import app, get_config

DB_URL = os.environ.get("TEST_DATABASE_URL", DEFAULT_DEV_URL)


def _db_available() -> bool:
    try:
        with psycopg.connect(DB_URL, connect_timeout=2):
            return True
    except Exception:
        return False


# --- password hashing (no database needed) -----------------------------------


def test_passwords_are_hashed_and_verified():
    h = hash_password("correct horse battery staple")
    assert "correct horse" not in h and h.startswith("$argon2")
    assert verify_password(h, "correct horse battery staple")
    assert not verify_password(h, "wrong password")
    assert hash_password("same input") != hash_password("same input")  # salted


# --- login flow against the real dependency chain ------------------------------

pytestmark_db = pytest.mark.skipif(
    not _db_available(),
    reason="no local Postgres — start it with: docker compose -f deploy/docker-compose.yml up -d",
)


def _pg_config() -> Config:
    return Config(store=StoreConfig(backend="postgres"))


@pytest.fixture()
def pg_mode():
    app.dependency_overrides[get_config] = _pg_config
    yield
    app.dependency_overrides.clear()
    web_app._current_tier = None


@pytest.fixture()
def account():
    """A provisioned throwaway account, removed (cascade) afterwards."""
    pool = get_pool(DB_URL)
    made = []

    def make():
        username, password = f"test-{uuid4().hex[:12]}", uuid4().hex
        user = provision_user(pool, username, password)
        made.append(user.id)
        return username, password

    yield make
    with pool.connection() as conn:
        conn.execute("DELETE FROM users WHERE id = ANY(%s)", (made,))


def _login(username: str, password: str) -> TestClient:
    client = TestClient(app)
    resp = client.post("/api/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return client


@pytestmark_db
def test_unauthenticated_requests_are_rejected(pg_mode):
    client = TestClient(app)
    assert client.get("/").status_code == 200  # the page (login overlay) is public
    assert client.get("/api/me").json() == {"auth_required": True, "username": None}
    for method, path in (("GET", "/api/store"), ("GET", "/api/profile"),
                         ("GET", "/api/model"), ("POST", "/api/chat/commit")):
        resp = client.request(method, path, json={"ops": []} if method == "POST" else None)
        assert resp.status_code == 401, (method, path, resp.status_code)


@pytestmark_db
def test_there_is_no_signup_path(pg_mode):
    client = TestClient(app)
    assert client.post("/api/signup", json={}).status_code in (404, 405)
    assert not any("signup" in r.path or "register" in r.path for r in app.routes)


@pytestmark_db
def test_login_logout_flow(pg_mode, account):
    username, password = account()

    bad = TestClient(app).post("/api/login", json={"username": username, "password": "nope"})
    assert bad.status_code == 401

    client = TestClient(app)
    resp = client.post("/api/login", json={"username": username, "password": password})
    assert resp.status_code == 200 and resp.json() == {"username": username}
    set_cookie = resp.headers["set-cookie"]
    assert "HttpOnly" in set_cookie and "throughline_session" in set_cookie

    assert client.get("/api/me").json()["username"] == username
    assert client.get("/api/store").json() == {"items": []}

    client.post("/api/logout")
    assert client.get("/api/store").status_code == 401  # revoked server-side


@pytestmark_db
def test_users_without_a_password_cannot_log_in(pg_mode):
    pool = get_pool(DB_URL)
    username = f"test-{uuid4().hex[:12]}"
    user_id = ensure_user(pool, username)  # the M16 default-user path: no credentials
    try:
        resp = TestClient(app).post("/api/login", json={"username": username, "password": ""})
        assert resp.status_code == 401
    finally:
        with get_pool(DB_URL).connection() as conn:
            conn.execute("DELETE FROM users WHERE id = %s", (user_id,))


@pytestmark_db
def test_two_logged_in_users_see_only_their_own_data(pg_mode, account):
    user_a, pass_a = account()
    user_b, pass_b = account()
    a, b = _login(user_a, pass_a), _login(user_b, pass_b)

    a.post("/api/chat/commit", json={"ops": [{"op": "new", "title": "A's private task"}]})
    a.put("/api/profile", json={"profile": "## Long-term goals\n\nA's goals."})

    assert b.get("/api/store").json()["items"] == []  # nothing of A's
    assert "A's goals" not in b.get("/api/profile").json()["profile"]

    b.post("/api/chat/commit", json={"ops": [{"op": "new", "title": "B's task"}]})
    assert [i["title"] for i in a.get("/api/store").json()["items"]] == ["A's private task"]
    assert [i["title"] for i in b.get("/api/store").json()["items"]] == ["B's task"]


@pytestmark_db
def test_model_tier_is_per_user(pg_mode, account):
    user_a, pass_a = account()
    user_b, pass_b = account()
    a, b = _login(user_a, pass_a), _login(user_b, pass_b)

    a.put("/api/model", json={"tier": "Sonnet 5"})
    assert a.get("/api/model").json()["tier"] == "Sonnet 5"
    assert b.get("/api/model").json()["tier"] is None  # B is unaffected


@pytestmark_db
def test_model_tier_survives_a_server_restart(pg_mode, account):
    """Regression: a user's model choice is persisted in the DB, so it isn't
    lost when the process restarts (or a Fly machine stops) between selecting
    the model and the next request — which used to revert them to the default
    model and wrongly demand the other provider's key."""
    username, password = account()
    _login(username, password).put("/api/model", json={"tier": "GPT 5.5"})

    # a brand-new client with no in-memory state = a fresh process/machine
    fresh = _login(username, password)
    assert fresh.get("/api/model").json()["tier"] == "GPT 5.5"  # still their choice


@pytestmark_db
def test_password_change_revokes_other_sessions(pg_mode, account):
    username, password = account()
    main, other = _login(username, password), _login(username, password)

    assert main.post("/api/password", json={
        "current_password": "wrong", "new_password": "a-new-password"}).status_code == 403

    resp = main.post("/api/password", json={
        "current_password": password, "new_password": "a-new-password"})
    assert resp.status_code == 200

    assert other.get("/api/store").status_code == 401  # old session revoked
    assert main.get("/api/store").status_code == 200  # the changing browser stays in
    assert TestClient(app).post("/api/login", json={
        "username": username, "password": password}).status_code == 401  # old password dead
    assert TestClient(app).post("/api/login", json={
        "username": username, "password": "a-new-password"}).status_code == 200
