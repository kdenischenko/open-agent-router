from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent_router.clock import format_local

MOMENT = datetime.fromisoformat("2026-11-14T12:00:00+00:00")
TZ = timezone(timedelta(hours=3))


@pytest.mark.parametrize("moment", [MOMENT, MOMENT.isoformat()])
def test_format_local_converts_datetime_and_iso_string(moment):
    assert format_local(moment, TZ) == "14.11 15:00"


@pytest.mark.parametrize("moment", [None, ""])
def test_format_local_uses_dash_for_missing_moment(moment):
    assert format_local(moment, TZ) == "—"  # non-ascii on purpose


@pytest.mark.parametrize("moment", [datetime(2026, 11, 14, 12), "2026-11-14T12:00:00"])
def test_format_local_rejects_naive_moment(moment):
    with pytest.raises(ValueError):
        format_local(moment, TZ)


def test_snapshot_clock_without_tz_follows_dst() -> None:
    from agent_router.policy import Snapshot, _clock

    for text in ("2026-01-15T12:00:00+00:00", "2026-07-15T12:00:00+00:00"):
        moment = datetime.fromisoformat(text)
        snapshot = Snapshot(now=moment, tz=None, pools={}, blocks={})
        assert _clock(moment, snapshot) == moment.astimezone().strftime("%H:%M")


def test_format_local_without_tz_uses_offset_of_that_date() -> None:
    for text in ("2026-01-15T12:00:00+00:00", "2026-07-15T12:00:00+00:00"):
        moment = datetime.fromisoformat(text)
        assert format_local(moment) == moment.astimezone().strftime("%d.%m %H:%M")


def test_parse_moment_without_tz_uses_offset_of_that_date() -> None:
    from agent_router.periods import parse_moment

    for month in (1, 7):
        parsed = parse_moment(f"2026-{month:02d}-15 12:00", None)
        assert parsed.utcoffset() == datetime(2026, month, 15, 12).astimezone().utcoffset()
