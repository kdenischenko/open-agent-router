from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent_router.auth import (
    PanelAuth,
    Tokens,
    ensure_tokens,
    generate_token,
    host_allowed,
    load_tokens,
    role_for,
)
from agent_router.clock import FakeClock
from agent_router.jobstore import migrate_jobs_schema
from agent_router.settings_store import SqliteSettings
from agent_router.store import SqliteStore


def test_generate_token_is_long_and_unique():
    first, second = generate_token(), generate_token()
    assert first != second
    assert len(first) >= 40


def test_ensure_tokens_creates_two_different_files(tmp_path):
    home = tmp_path / "dir with space"
    tokens = ensure_tokens(home)
    assert (home / "client.token").is_file() and (home / "admin.token").is_file()
    assert tokens.client != tokens.admin
    assert load_tokens(home) == tokens


def test_ensure_tokens_does_not_overwrite(tmp_path):
    first = ensure_tokens(tmp_path)
    before = {name: (tmp_path / name).read_bytes() for name in ("client.token", "admin.token")}
    second = ensure_tokens(tmp_path)
    after = {name: (tmp_path / name).read_bytes() for name in ("client.token", "admin.token")}
    assert first == second
    assert before == after


def test_ensure_tokens_creates_only_missing_file(tmp_path):
    first = ensure_tokens(tmp_path)
    (tmp_path / "admin.token").unlink()
    second = ensure_tokens(tmp_path)
    assert second.client == first.client
    assert second.admin != first.admin


def test_load_tokens_rejects_missing_and_empty(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_tokens(tmp_path)
    ensure_tokens(tmp_path)
    (tmp_path / "client.token").write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_tokens(tmp_path)


def test_tokens_repr_hides_values():
    tokens = Tokens(client="secret-client", admin="secret-admin")
    assert "secret" not in repr(tokens)
    assert "secret" not in str(tokens)


def test_role_for_distinguishes_roles():
    tokens = Tokens(client="c" * 43, admin="a" * 43)
    assert role_for("c" * 43, tokens) == "client"
    assert role_for("a" * 43, tokens) == "admin"


@pytest.mark.parametrize("junk", ["", "c", "c" * 42, "c" * 44, "junk", " " + "c" * 43, None, 5])
def test_role_for_rejects_junk(junk):
    assert role_for(junk, Tokens(client="c" * 43, admin="a" * 43)) is None


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "127.0.0.1:8787", "localhost", "localhost:8788", "LOCALHOST:80", " localhost "],
)
def test_host_allowed(host):
    assert host_allowed(host) is True


@pytest.mark.parametrize(
    "host",
    [
        None,
        "",
        "evil.example",
        "evil.example:8787",
        "localhost.evil.example",
        "127.0.0.1.evil.example",
        "evil.example:127.0.0.1",
        "localhost:abc",
        "localhost:",
        "127.0.0.2",
        "0.0.0.0",
        "[::1]:8787",
        "localhost:80@example.com",
        "localhost,evil.example",
    ],
)
def test_host_rejected(host):
    assert host_allowed(host) is False


T0 = datetime(2026, 11, 13, 9, tzinfo=UTC)
PANEL_TOKEN = "a" * 43


@pytest.fixture
def panel(tmp_path):
    store = SqliteStore(tmp_path / "agent-router.sqlite")
    migrate_jobs_schema(store)
    settings = SqliteSettings(store)
    clock = FakeClock(T0)
    yield PanelAuth(PANEL_TOKEN, settings, 30, clock), settings, clock
    store.close()


def test_settings_store_get_set_bump(panel):
    _, settings, _ = panel
    assert settings.get("panel_epoch") is None
    settings.set("panel_epoch", "7")
    assert settings.get("panel_epoch") == "7"
    assert settings.bump("panel_epoch") == 8
    assert settings.bump("panel_epoch") == 9
    assert settings.bump("other key") == 1
    assert settings.get("other key") == "1"


def test_cookie_survives_new_instance(panel):
    auth, settings, clock = panel
    cookie = auth.issue()
    assert cookie.startswith("v1.")
    again = PanelAuth(PANEL_TOKEN, settings, 30, clock)
    assert again.valid(cookie) is True
    assert again.csrf_ok(cookie, auth.csrf_for(cookie)) is True


def test_cookie_rejected_after_token_change(panel):
    auth, settings, clock = panel
    cookie = auth.issue()
    other = PanelAuth("b" * 43, settings, 30, clock)
    assert other.valid(cookie) is False
    assert other.csrf_ok(cookie, auth.csrf_for(cookie)) is False


def test_cookie_rejected_after_logout(panel):
    auth, _, _ = panel
    cookie = auth.issue()
    csrf = auth.csrf_for(cookie)
    auth.logout()
    assert auth.valid(cookie) is False
    assert auth.csrf_ok(cookie, csrf) is False
    fresh = auth.issue()
    assert auth.valid(fresh) is True
    assert fresh != cookie


def test_cookie_expires_after_days(panel):
    auth, _, clock = panel
    cookie = auth.issue()
    assert auth.valid(cookie) is True
    clock.advance(timedelta(days=31))
    assert auth.valid(cookie) is False


def test_cookie_issued_in_future_rejected(panel):
    auth, _, clock = panel
    cookie = auth.issue()
    clock.set(T0 - timedelta(seconds=1))
    assert auth.valid(cookie) is False


@pytest.mark.parametrize("part", [0, 1, 2, 3])
def test_tampered_signature_rejected(panel, part):
    auth, _, _ = panel
    pieces = auth.issue().split(".")
    replacement = "9" if pieces[part][0] != "9" else "8"
    pieces[part] = replacement + pieces[part][1:]
    assert auth.valid(".".join(pieces)) is False


@pytest.mark.parametrize(
    "cookie",
    ["", "v1", "v2.1.0.x", "v1.a.0.x", "v1.1.b.x", "v1.1.0", "v1.1.0.x.y", None],
)
def test_garbage_cookie_rejected(panel, cookie):
    auth, _, _ = panel
    assert auth.valid(cookie) is False
