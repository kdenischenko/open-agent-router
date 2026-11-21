"""Window observations from the rate_limits block of the Claude Code status line."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from agent_router.clock import to_utc
from agent_router.config import StatuslineSettings
from agent_router.store import Observation

SOURCE = "claude_statusline"
SAME_WINDOW = timedelta(minutes=10)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _observation(entry: Any, pool: str, window: str, now: datetime) -> Observation | None:
    if not isinstance(entry, Mapping):
        return None
    percent = _number(entry.get("used_percentage"))
    epoch = _number(entry.get("resets_at"))
    if percent is None or percent < 0 or epoch is None or epoch <= 0:
        return None
    try:
        resets_at = datetime.fromtimestamp(epoch, UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return Observation(pool, window, min(percent, 100.0) / 100.0, now, resets_at, SOURCE)


def observations_from_rate_limits(
    block: Any, now: datetime, settings: StatuslineSettings
) -> list[Observation]:
    """Window observations from the block; missing or unparseable windows are skipped."""
    if not isinstance(block, Mapping):
        return []
    now = to_utc(now)
    found = []
    for key, window in (("five_hour", settings.five_hour), ("seven_day", settings.seven_day)):
        obs = _observation(block.get(key), settings.pool, window, now)
        if obs is not None:
            found.append(obs)
    return found


def accept_statusline(obs: Observation, last: Observation | None, now: datetime) -> bool:
    """Stale-session rule: an idle Claude Code session keeps sending old figures."""
    if obs.resets_at is None or obs.resets_at <= to_utc(now):
        return False  # reset is already in the past
    if last is None or last.resets_at is None:
        return True
    if obs.resets_at < last.resets_at - SAME_WINDOW:
        return False  # earlier window
    same_window = abs(obs.resets_at - last.resets_at) <= SAME_WINDOW
    # usage does not decrease within one window
    return not (same_window and obs.fraction < last.fraction)
