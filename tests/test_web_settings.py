"""Read-only configuration and doctor findings on the settings page."""

from __future__ import annotations

import pytest

from agent_router import registry
from agent_router.usage import UsageSourceSpec
from helpers import full_config_text, write_config
from test_web_dashboard import authenticated_client

FAKE_SECRET = "settings-test-secret-do-not-render"


@pytest.fixture
def config(home, monkeypatch):
    spec = UsageSourceSpec(
        name="fake-usage",
        factory=lambda pool_usage, env: None,
        windows=frozenset({"monthly"}),
        observation_source="fake_usage",
    )
    monkeypatch.setitem(registry._USAGE_SOURCES, "fake-usage", spec)
    monkeypatch.setenv("SETTINGS_FAKE_API_KEY", FAKE_SECRET)
    text = full_config_text()
    text = text.replace(
        "[[routes]]",
        "[pools.usage]\n"
        'source = "fake-usage"\n'
        'api_key_env = "SETTINGS_FAKE_API_KEY"\n'
        'windows = { monthly = "month" }\n\n'
        "[[routes]]",
        1,
    )
    text = text.replace(
        'role = "pace"\n  limit_usd = 60',
        'role = "pace"\n  anchor = 2026-11-01T00:00:00Z\n  limit_usd = 60',
        1,
    )
    return write_config(home, text)


def test_settings_requires_login(client):
    response = client.get("/settings", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_settings_shows_configuration_and_doctor_hints(client, tokens):
    authenticated_client(client, tokens)

    response = client.get("/settings")

    assert response.status_code == 200
    text = response.text
    assert '<a href="/settings" class="nav-link" aria-current="page">Settings</a>' in text
    for expected in (
        "OpenCode Go",
        "opencode-go",
        "usd",
        "fake-usage",
        "auto",
        "api_key_env = SETTINGS_FAKE_API_KEY",
        "month",
        "1mo",
        "fixed",
        "pace",
        "2026-11-01",
        "cline/glm-5.3",
        "harness",
        "cline",
        "opencode-go/glm-5.3",
        "standard",
        "read",
        "write",
        "disabled",
        "claude-code",
        "claude-pro",
        "tasks.allowed_roots is empty, jobs not accepted",
        "add a working directory to tasks.allowed_roots",
    ):
        assert expected in text


def test_settings_never_renders_secret_or_session_values(client, tokens):
    authenticated_client(client, tokens)
    session = client.cookies.get("ar_session")
    assert session is not None

    response = client.get("/settings")

    assert response.status_code == 200
    assert FAKE_SECRET not in response.text
    assert tokens.admin not in response.text
    assert session not in response.text
