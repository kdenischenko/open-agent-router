"""Route scorecard built from job history and ratings."""

from __future__ import annotations

from agent_router.route_report import AttemptSample, JobSample, build


def job(
    route: str | None = "a/main",
    *,
    tier: str = "standard",
    mode: str = "read",
    status: str = "completed",
    rating: str | None = None,
    duration_s: float | None = 10.0,
    tokens: int | None = 100,
    cost_usd: float | None = None,
    cost_estimated: bool = False,
) -> JobSample:
    return JobSample(
        route=route,
        tier=tier,  # type: ignore[arg-type]
        mode=mode,  # type: ignore[arg-type]
        status=status,
        rating=rating,
        duration_s=duration_s,
        tokens=tokens,
        cost_usd=cost_usd,
        cost_estimated=cost_estimated,
    )


def attempt(outcome: str, route: str = "a/main", tier: str = "standard", mode: str = "read"):
    return AttemptSample(route=route, tier=tier, mode=mode, outcome=outcome)  # type: ignore[arg-type]


def only_row(report: dict) -> dict:
    (row,) = report["rows"]
    return row


def test_empty_history_has_no_rows() -> None:
    report = build([], [], quality={"a/main": 4})

    assert report["rows"] == []
    assert report["min_jobs"] == 5


def test_row_counts_outcomes_attempts_and_ratings() -> None:
    jobs = [
        job(rating="good"),
        job(rating="good"),
        job(status="partial", rating="partial"),
        job(status="failed", rating="bad"),
        job(status="cancelled"),
    ]
    attempts = [attempt("ok"), attempt("limit"), attempt("error"), attempt("timeout")]

    row = only_row(build(jobs, attempts, quality={"a/main": 4}))

    assert (row["route"], row["tier"], row["mode"]) == ("a/main", "standard", "read")
    assert row["jobs"] == 5
    assert row["outcomes"] == {"completed": 2, "partial": 1, "failed": 1, "cancelled": 1}
    assert row["attempts"] == {"limit": 1, "error": 1, "timeout": 1}
    assert row["ratings"] == {"good": 2, "partial": 1, "bad": 1}
    assert row["rated"] == 4
    assert row["rated_share"] == 0.8
    assert row["good_share"] == 0.5
    assert row["quality"] == 4
    assert row["in_config"] is True
    assert row["ranked"] is True


def test_unrated_jobs_have_no_rating_share() -> None:
    row = only_row(build([job(), job()], [], quality={"a/main": 3}))

    assert row["rated"] == 0
    assert row["rated_share"] == 0.0
    assert row["good_share"] is None
    assert row["ratings"] == {"good": 0, "partial": 0, "bad": 0}


def test_rows_split_by_route_tier_and_mode() -> None:
    jobs = [job(), job(tier="strong"), job(mode="write"), job("b/main")]

    report = build(jobs, [], quality={"a/main": 3, "b/main": 2})

    keys = {(row["route"], row["tier"], row["mode"]) for row in report["rows"]}
    assert keys == {
        ("a/main", "standard", "read"),
        ("a/main", "strong", "read"),
        ("a/main", "standard", "write"),
        ("b/main", "standard", "read"),
    }


def test_duration_median_and_p90_and_token_median() -> None:
    jobs = [job(duration_s=float(n), tokens=n * 10) for n in range(1, 11)]

    row = only_row(build(jobs, [], quality={}))

    assert row["duration_s"] == {"median": 5.5, "p90": 9.0}
    assert row["tokens_median"] == 55.0


def test_cost_median_marks_estimated_values() -> None:
    jobs = [job(cost_usd=0.1), job(cost_usd=0.3, cost_estimated=True), job(cost_usd=None)]

    row = only_row(build(jobs, [], quality={}))

    assert row["cost_median"] == 0.2
    assert row["cost_estimated"] is True


def test_reported_costs_are_not_marked_estimated() -> None:
    row = only_row(build([job(cost_usd=0.1), job(cost_usd=0.3)], [], quality={}))

    assert row["cost_estimated"] is False


def test_jobs_without_figures_have_no_medians() -> None:
    row = only_row(build([job(duration_s=None, tokens=None)], [], quality={}))

    assert row["duration_s"] == {"median": None, "p90": None}
    assert row["tokens_median"] is None
    assert row["cost_median"] is None


def test_route_removed_from_config_keeps_its_stored_id() -> None:
    row = only_row(build([job("gone/model", rating="good")], [], quality={"a/main": 4}))

    assert row["route"] == "gone/model"
    assert row["in_config"] is False
    assert row["quality"] is None


def test_rows_below_the_minimum_sample_are_not_ranked() -> None:
    small = [job("b/main", rating="good")] * 4
    large = [job("a/main", rating="bad")] * 5 + [job("c/main", rating="good")] * 5

    report = build(small + large, [], quality={})

    assert [(row["route"], row["ranked"]) for row in report["rows"]] == [
        ("c/main", True),
        ("a/main", True),
        ("b/main", False),
    ]


def test_jobs_without_a_route_are_skipped() -> None:
    report = build([job(None, status="failed")], [], quality={})

    assert report["rows"] == []
