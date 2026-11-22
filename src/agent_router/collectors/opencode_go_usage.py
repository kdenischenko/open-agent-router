"""OpenCode Go plan usage: window meters read from the vendor usage endpoint."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime

import httpx

from agent_router.clock import to_utc
from agent_router.config_model import PoolUsage
from agent_router.usage import UsagePoint, UsageRead, UsageSourceSpec

USAGE_URL = "https://opencode.ai/inference/go/v1/usage"
DEFAULT_API_KEY_ENV = "OPENCODE_GO_API_KEY"
TIMEOUT_S = 10.0


def _reset_time(value: object) -> datetime | None:
    """UTC reset time from an ISO 8601 string; a malformed value raises ValueError."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("reset time is not a string")
    return to_utc(datetime.fromisoformat(value))


def _point(field: str, meter: object) -> UsagePoint | None:
    """One meter as a point; a malformed percent or reset time skips the meter."""
    if not isinstance(meter, Mapping):
        return None
    percent = meter.get("percent")
    if isinstance(percent, bool) or not isinstance(percent, int | float):
        return None
    if not math.isfinite(percent) or not 0 <= percent <= 100:
        return None
    try:
        resets_at = _reset_time(meter.get("resetsAt"))
    except ValueError:
        return None
    return UsagePoint(field=field, fraction=percent / 100, resets_at=resets_at)


def _points(body: object) -> tuple[UsagePoint, ...]:
    """Usable meters of the response body; a missing or malformed meter is skipped."""
    if not isinstance(body, Mapping):
        return ()
    usage = body.get("usage")
    if not isinstance(usage, Mapping):
        return ()
    points = []
    for field, meter in usage.items():
        point = _point(str(field), meter)
        if point is not None:
            points.append(point)
    return tuple(points)


class OpenCodeGoUsage:
    """Reads the plan's rolling, weekly and monthly meters from the OpenCode usage endpoint.

    The API key is looked up in the environment on every read; it is never stored on the
    instance, logged, or included in a reason code.
    """

    def __init__(
        self,
        env: Mapping[str, str],
        api_key_env: str = DEFAULT_API_KEY_ENV,
        client: httpx.Client | None = None,
    ) -> None:
        self._env = env
        self._api_key_env = api_key_env
        self._client = client  # injected in tests; otherwise one is created per read

    def read(self) -> UsageRead:
        key = self._env.get(self._api_key_env) or ""
        if not key:
            return UsageRead(reason="missing_api_key")
        try:
            response = self._get(key)
        except httpx.TimeoutException:
            return UsageRead(reason="timeout")
        except httpx.HTTPError:
            return UsageRead(reason="unavailable")
        if response.status_code == 401:
            return UsageRead(reason="unauthorized")
        if response.status_code == 403:
            return UsageRead(reason="forbidden")
        if not response.is_success:
            return UsageRead(reason="http_error")
        try:
            body = response.json()
        except ValueError:  # includes json.JSONDecodeError; the body is not logged
            return UsageRead(reason="malformed_response")
        points = _points(body)
        if not points:
            return UsageRead(reason="malformed_response")
        return UsageRead(points=points)

    def _get(self, key: str) -> httpx.Response:
        headers = {"Authorization": f"Bearer {key}"}
        if self._client is not None:
            return self._client.get(USAGE_URL, headers=headers, timeout=TIMEOUT_S)
        with httpx.Client(timeout=TIMEOUT_S) as client:
            return client.get(USAGE_URL, headers=headers)


def _factory(pool_usage: PoolUsage, env: Mapping[str, str]) -> OpenCodeGoUsage:
    return OpenCodeGoUsage(env, pool_usage.api_key_env or DEFAULT_API_KEY_ENV)


SOURCE = UsageSourceSpec(
    name="opencode-go",
    factory=_factory,
    windows=frozenset({"rolling", "weekly", "monthly"}),
    observation_source="opencode_go_usage",
)
