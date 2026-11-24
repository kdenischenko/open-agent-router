"""Grouped spend journal reads used by the dashboard history charts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from agent_router.spend import SpendBucket, SpendEntry

NOW = datetime(2026, 11, 13, 11, tzinfo=UTC)


def add_spend(
    jobstore,
    ref: str,
    at: datetime,
    *,
    pool: str = "opencode-go",
    cost: float = 1.0,
    tokens: int = 10,
    estimated: bool = False,
) -> None:
    assert jobstore.add_spend(
        SpendEntry(
            pool=pool,
            at=at,
            source="job",
            route=f"{pool}/model",
            tokens_in=tokens,
            tokens_out=0,
            cost_usd=cost,
            estimated=estimated,
            prices_version="v1" if estimated else None,
            ref_id=ref,
            ref_n=1,
        )
    )


def test_spend_buckets_use_start_inclusive_ranges_and_keep_pools_apart(jobstore) -> None:
    starts = [NOW - timedelta(hours=3), NOW - timedelta(hours=2), NOW - timedelta(hours=1)]
    add_spend(jobstore, "too-old", starts[0] - timedelta(microseconds=1), cost=100)
    add_spend(jobstore, "first-edge", starts[0], cost=1.0, tokens=10)
    add_spend(
        jobstore,
        "first-estimated",
        starts[1] - timedelta(microseconds=1),
        cost=2.0,
        tokens=20,
        estimated=True,
    )
    add_spend(jobstore, "second-edge", starts[1], cost=4.0, tokens=40)
    add_spend(jobstore, "other-pool", starts[1], pool="kimi-code", cost=100, tokens=100)
    add_spend(jobstore, "at-end", NOW, cost=100)

    assert jobstore.spend_buckets("opencode-go", starts, NOW) == [
        SpendBucket(starts[0], tokens=30, cost_usd=3.0, estimated=True),
        SpendBucket(starts[1], tokens=40, cost_usd=4.0, estimated=False),
    ]
    assert jobstore.spend_buckets("kimi-code", starts, NOW) == [
        SpendBucket(starts[1], tokens=100, cost_usd=100.0, estimated=False)
    ]


def test_spend_buckets_accept_local_day_boundaries(jobstore) -> None:
    tz = timezone(timedelta(hours=5))
    local_now = NOW.astimezone(tz)
    today = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    starts = [today - timedelta(days=1), today]
    add_spend(jobstore, "before-today", starts[1] - timedelta(microseconds=1), cost=1.0)
    add_spend(jobstore, "today", starts[1], cost=2.0)

    rows = jobstore.spend_buckets("opencode-go", starts, NOW)

    assert [(row.start, row.cost_usd) for row in rows] == [(starts[0], 1.0), (starts[1], 2.0)]
    assert starts[1].utcoffset() == timedelta(hours=5)
    assert starts[1].astimezone(UTC).hour == 19
