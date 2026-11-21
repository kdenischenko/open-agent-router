"""Shared test fixtures."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from agent_router.api import create_app
from agent_router.auth import ensure_tokens
from agent_router.clock import FakeClock
from agent_router.events import EventBus
from helpers import BASE_URL, JOBSTORE_FACTORIES, STORE_FACTORIES, full_config_text, write_config


@pytest.fixture(params=sorted(STORE_FACTORIES))
def store(request, tmp_path):
    """A store for each backend in STORE_FACTORIES; closed after the test."""
    instance = STORE_FACTORIES[request.param](tmp_path)
    yield instance
    instance.close()


@pytest.fixture
def home(tmp_path):
    """Gateway home directory with a space and Cyrillic in its name."""
    return tmp_path / "дом шлюза"  # non-ascii on purpose


@pytest.fixture
def config(home):
    return write_config(home, full_config_text())


@pytest.fixture
def clock():
    return FakeClock(datetime(2026, 11, 13, 9, tzinfo=UTC))


@pytest.fixture
def tokens(home):
    return ensure_tokens(home)


@pytest.fixture
def bus(clock):
    instance = EventBus(clock)
    yield instance
    instance.close()


@pytest.fixture
def app(config, store, clock, tokens, home, bus):
    return create_app(config, store, clock, tokens, home=home, bus=bus, background=False)


@pytest.fixture
def client(app):
    with TestClient(app, base_url=BASE_URL) as instance:
        yield instance


@pytest.fixture(params=sorted(JOBSTORE_FACTORIES))
def jobstore(request, store):
    """A job store for each backend in JOBSTORE_FACTORIES, built on the store fixture."""
    return JOBSTORE_FACTORIES[request.param](store)


@pytest.fixture
def register_harness(monkeypatch):
    """Registers a stub harness in the registry for the test: register_harness(name, spec)."""
    from agent_router import registry

    def register(name, spec):
        monkeypatch.setitem(registry._OVERRIDES, name, spec)
        return spec

    return register
