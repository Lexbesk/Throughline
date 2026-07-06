"""Per-user API key tests (v4 M18).

Crypto is tested without a database. The store/isolation/no-plaintext checks and
the web-level flow run against the local Docker Postgres and skip when it's
absent. A throwaway master key is set for the whole module so ``get_cipher``
(which reads the env each call) works without touching a real secret.
"""

from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

import meeting_notes_todos.web.app as web_app
from meeting_notes_todos.config import Config, LLMConfig, StoreConfig
from meeting_notes_todos.keys import (
    MASTER_KEY_ENV,
    ApiKeyStore,
    KeyCipher,
    generate_master_key,
    get_cipher,
)
from meeting_notes_todos.providers import build_provider

os.environ.setdefault(MASTER_KEY_ENV, generate_master_key())  # module-wide test key

DB_URL = os.environ.get("TEST_DATABASE_URL",
                        "postgresql://throughline:throughline@localhost:5432/throughline")


def _db_available() -> bool:
    try:
        with psycopg.connect(DB_URL, connect_timeout=2):
            return True
    except Exception:
        return False


# --- crypto (no database) ------------------------------------------------------


def test_cipher_round_trips_and_hides_the_plaintext():
    cipher = KeyCipher(generate_master_key())
    token = cipher.encrypt("sk-ant-secret-value-1234")
    assert "sk-ant-secret" not in token  # ciphertext reveals nothing
    assert cipher.decrypt(token) == "sk-ant-secret-value-1234"


def test_get_cipher_requires_a_master_key(monkeypatch):
    monkeypatch.delenv(MASTER_KEY_ENV, raising=False)
    with pytest.raises(RuntimeError):
        get_cipher()


def test_a_wrong_master_key_cannot_decrypt():
    token = KeyCipher(generate_master_key()).encrypt("sk-secret")
    from cryptography.fernet import InvalidToken

    with pytest.raises(InvalidToken):
        KeyCipher(generate_master_key()).decrypt(token)


# --- build_provider carries the passed key (no network) ------------------------


def test_build_provider_uses_the_passed_key_over_env():
    a = build_provider(LLMConfig(model="claude-haiku-4-5"), api_key="sk-ant-userA")
    b = build_provider(LLMConfig(model="claude-haiku-4-5"), api_key="sk-ant-userB")
    assert a._client.api_key == "sk-ant-userA"
    assert b._client.api_key == "sk-ant-userB"  # each provider carries its own key
    o = build_provider(LLMConfig(model="gpt-4.1-mini"), api_key="sk-openai-userA")
    assert o._client.api_key == "sk-openai-userA"


# --- DB-backed store + web flow -----------------------------------------------

pytestmark_db = pytest.mark.skipif(
    not _db_available(),
    reason="no local Postgres — start it with: docker compose -f deploy/docker-compose.yml up -d",
)


def _pg_config() -> Config:
    return Config(store=StoreConfig(backend="postgres"))


@pytest.fixture()
def pool():
    from meeting_notes_todos.db import get_pool

    return get_pool(DB_URL)


@pytest.fixture()
def two_users(pool):
    from meeting_notes_todos.db import ensure_user

    ids = [ensure_user(pool, f"test-{uuid4().hex[:12]}") for _ in range(2)]
    yield ids
    with pool.connection() as conn:
        conn.execute("DELETE FROM users WHERE id = ANY(%s)", (ids,))


@pytestmark_db
def test_store_encrypts_and_never_persists_plaintext(pool, two_users):
    user_a, _ = two_users
    store = ApiKeyStore(pool, user_a, get_cipher())
    secret = "sk-ant-super-secret-abcd"
    store.set_key("anthropic", secret)

    assert store.get_key("anthropic") == secret  # decrypts for actual use
    assert store.list_keys() == [
        {"provider": "anthropic", "last4": "abcd",
         "updated_at": store.list_keys()[0]["updated_at"]}
    ]  # masked view: last4 only, no plaintext

    with pool.connection() as conn:  # the raw column is ciphertext
        raw = conn.execute(
            "SELECT encrypted_key FROM user_api_keys WHERE user_id = %s", (user_a,)
        ).fetchone()[0]
    assert secret not in raw and "sk-ant" not in raw

    store.set_key("anthropic", "sk-ant-replaced-wxyz")  # upsert
    assert store.get_key("anthropic") == "sk-ant-replaced-wxyz"
    assert store.delete_key("anthropic") is True
    assert store.get_key("anthropic") is None


@pytestmark_db
def test_keys_are_isolated_per_user(pool, two_users):
    user_a, user_b = two_users
    a = ApiKeyStore(pool, user_a, get_cipher())
    b = ApiKeyStore(pool, user_b, get_cipher())
    a.set_key("anthropic", "sk-ant-A")
    b.set_key("anthropic", "sk-ant-B")
    assert a.get_key("anthropic") == "sk-ant-A"  # each sees only their own
    assert b.get_key("anthropic") == "sk-ant-B"
    a.delete_key("anthropic")
    assert b.get_key("anthropic") == "sk-ant-B"  # deleting A's leaves B's


@pytestmark_db
def test_unknown_provider_is_rejected(pool, two_users):
    store = ApiKeyStore(pool, two_users[0], get_cipher())
    with pytest.raises(ValueError):
        store.set_key("gemini", "whatever")


# --- web flow: masked responses, no-key gate, isolation via the API ------------


@pytest.fixture()
def pg_mode():
    from meeting_notes_todos.web.app import get_config

    app = web_app.app
    app.dependency_overrides[get_config] = _pg_config
    yield
    app.dependency_overrides.clear()
    web_app._tier_by_user.clear()


@pytest.fixture()
def account():
    from meeting_notes_todos.auth import provision_user
    from meeting_notes_todos.db import get_pool

    pool = get_pool(DB_URL)
    made = []

    def make():
        username, password = f"test-{uuid4().hex[:12]}", uuid4().hex
        provision_user(pool, username, password)
        made.append(username)
        return username, password

    yield make
    with pool.connection() as conn:
        conn.execute("DELETE FROM users WHERE username = ANY(%s)", (made,))


def _login(username, password):
    client = TestClient(web_app.app)
    assert client.post("/api/login", json={"username": username, "password": password}).status_code == 200
    return client


@pytestmark_db
def test_key_endpoints_mask_and_never_echo_the_key(pg_mode, account):
    username, password = account()
    client = _login(username, password)

    assert client.get("/api/keys").json() == {
        "providers": ["anthropic", "openai"], "keys": []
    }

    resp = client.put("/api/keys/anthropic", json={"api_key": "sk-ant-endpoint-key-6789"})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"provider": "anthropic", "last4": "6789"}  # masked, no plaintext
    assert "sk-ant-endpoint" not in resp.text

    listed = client.get("/api/keys").json()["keys"]
    assert listed[0]["provider"] == "anthropic" and listed[0]["last4"] == "6789"
    assert "sk-ant" not in client.get("/api/keys").text

    assert client.put("/api/keys/gemini", json={"api_key": "x"}).status_code == 400  # bad provider
    assert client.delete("/api/keys/anthropic").json() == {"removed": True}
    assert client.get("/api/keys").json()["keys"] == []


@pytestmark_db
def test_keyless_user_is_prompted_not_errored(pg_mode, account):
    username, password = account()
    client = _login(username, password)
    # no key set → chat is blocked gracefully with a clear, JSON message
    resp = client.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "anthropic" in detail.lower() and "key" in detail.lower()


@pytestmark_db
def test_key_management_is_isolated_between_logged_in_users(pg_mode, account):
    user_a, pass_a = account()
    user_b, pass_b = account()
    a, b = _login(user_a, pass_a), _login(user_b, pass_b)

    a.put("/api/keys/anthropic", json={"api_key": "sk-ant-only-A-1111"})
    assert b.get("/api/keys").json()["keys"] == []  # B can't see A's key
    assert b.delete("/api/keys/anthropic").json() == {"removed": False}  # nor delete it
    assert a.get("/api/keys").json()["keys"][0]["last4"] == "1111"  # A's is intact
