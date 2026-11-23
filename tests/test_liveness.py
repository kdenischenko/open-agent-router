"""Liveness: an attempt is stopped on silence past the idle limit, the hard cap wins."""

from __future__ import annotations

from agent_router.liveness import Liveness


def test_fresh_is_ok() -> None:
    assert Liveness(0, 100, 10).verdict(5) == "ok"


def test_silence_past_idle_is_idle() -> None:
    assert Liveness(0, 100, 10).verdict(10.0) == "idle"


def test_touch_resets_idle() -> None:
    live = Liveness(0, 100, 10)
    live.touch(8)
    assert live.verdict(17) == "ok"
    assert live.verdict(18) == "idle"


def test_hard_cap_wins_over_activity() -> None:
    live = Liveness(0, 100, 10)
    for moment in range(100):
        live.touch(float(moment))
    assert live.verdict(100) == "hard"


def test_hard_before_idle_when_both_due() -> None:
    assert Liveness(0, 5, 10).verdict(12) == "hard"


def test_idle_disabled() -> None:
    assert Liveness(0, 100, None).verdict(99) == "ok"


def test_touch_older_than_last_is_ignored() -> None:
    live = Liveness(0, 100, 10)
    live.touch(8)
    live.touch(3)
    assert live.verdict(17) == "ok"


def test_touch_records_the_signal() -> None:
    live = Liveness(0, 100, 10)
    live.touch(5, "output")
    assert live.last_signal == "output"
    assert live.last_signal_at == 5


def test_touch_without_a_signal_records_output() -> None:
    live = Liveness(0, 100, 10)
    live.touch(5)
    assert live.last_signal == "output"
    assert live.last_signal_at == 5


def test_the_newest_signal_wins() -> None:
    live = Liveness(0, 100, 10)
    live.touch(5, "output")
    live.touch(7, "cpu")
    live.touch(6, "files")  # an older moment never moves the mark back
    assert live.last_signal == "cpu"
    assert live.last_signal_at == 7


def test_verdict_is_unchanged_by_the_signal() -> None:
    live = Liveness(0, 100, 10)
    live.touch(5, "cpu")
    assert live.verdict(14) == "ok"
    assert live.verdict(15) == "idle"


def test_fresh_liveness_has_no_signal() -> None:
    live = Liveness(0, 100, 10)
    assert live.last_signal is None
    assert live.last_signal_at is None
