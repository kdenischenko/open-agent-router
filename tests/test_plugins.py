from __future__ import annotations

import pytest

from agent_router import plugins
from agent_router.config import ConfigError


class FakeEntry:
    def __init__(self, name, value=None, error=None):
        self.name = name
        self._value = value
        self._error = error

    def load(self):
        if self._error is not None:
            raise self._error
        return self._value


def patch_entries(monkeypatch, entries):
    seen = []

    def entry_points(*, group):
        seen.append(group)
        return list(entries)

    monkeypatch.setattr(plugins.metadata, "entry_points", entry_points)
    return seen


def test_load_plugin_returns_entry_object(monkeypatch):
    marker = object()
    seen = patch_entries(monkeypatch, [FakeEntry("alpha", marker), FakeEntry("beta", object())])
    assert plugins.load_plugin("agent_router.sinks", "alpha") is marker
    assert seen == ["agent_router.sinks"]


def test_load_plugin_unknown_name_lists_available(monkeypatch):
    patch_entries(monkeypatch, [FakeEntry("beta"), FakeEntry("alpha")])
    with pytest.raises(ConfigError) as caught:
        plugins.load_plugin("agent_router.storage", "gamma")
    message = str(caught.value)
    assert "gamma" in message and "agent_router.storage" in message
    assert "alpha, beta" in message


def test_load_plugin_unknown_name_without_any_plugins():
    with pytest.raises(ConfigError) as caught:
        plugins.load_plugin("agent_router.storage", "no-such-backend")
    assert "no-such-backend" in str(caught.value)


def test_load_plugin_import_failure(monkeypatch):
    boom = ImportError("no such module")
    patch_entries(monkeypatch, [FakeEntry("broken", error=boom)])
    with pytest.raises(ConfigError) as caught:
        plugins.load_plugin("agent_router.storage", "broken")
    message = str(caught.value)
    assert "broken" in message and "agent_router.storage" in message
    assert caught.value.__cause__ is boom
