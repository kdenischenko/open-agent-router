"""Harness adapters: command building and output parsing."""

from __future__ import annotations

from agent_router.adapters.base import (
    MAX_ARG_CHARS,
    Adapter,
    RunOutcome,
    looks_like_limit,
    with_preamble,
)

__all__ = [
    "MAX_ARG_CHARS",
    "Adapter",
    "RunOutcome",
    "looks_like_limit",
    "with_preamble",
]
