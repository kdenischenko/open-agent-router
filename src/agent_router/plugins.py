"""Discover third-party connectors via Python entry points; load only those named in config."""

from __future__ import annotations

from importlib import metadata
from typing import Any

from agent_router.config import ConfigError


def load_plugin(group: str, name: str) -> Any:
    """Entry point object `name` from group `group`; ConfigError if missing or fails to load."""
    found = {entry.name: entry for entry in metadata.entry_points(group=group)}
    if name not in found:
        available = ", ".join(sorted(found)) or "none"
        raise ConfigError([f'plugin "{name}" in group {group} not found; available: {available}'])
    try:
        return found[name].load()
    except Exception as exc:
        raise ConfigError(
            [f'plugin "{name}" in group {group} failed to load: {type(exc).__name__}: {exc}']
        ) from exc
