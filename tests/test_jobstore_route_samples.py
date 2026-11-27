"""Route scorecard samples: finished jobs and attempts read from the job store."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_router.jobs import Attempt, Job, JobRequest, Rating
from agent_router.route_report import AttemptSample, JobSample

T0 = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)


def make_job(
    job_id: str,
    *,
    status: str = "completed",
    route: str | None = "a/main",
    mode: str = "read",
    tier: str = "standard",
    finished_at: datetime | None = T0,
) -> Job:
    return Job(
        id=job_id,
        request=JobRequest("задание", Path("/work/проект"), mode=mode),  # non-ascii on purpose
        status=status,  # type: ignore[arg-type]
        tier=tier,  # type: ignore[arg-type]
        tier_source="default",
        repo_root=None,
        route=route,
        created_at=(finished_at or T0) - timedelta(minutes=1),
        finished_at=finished_at,
    )


def make_attempt(
    job_id: str,
    n: int,
    route: str = "a/main",
    *,
    outcome: str = "ok",
    finished_at: datetime | None = T0,
    duration_s: float | None = 5.0,
    tokens_in: int | None = 10,
    tokens_out: int | None = 20,
    cost_usd: float | None = None,
    cost_estimated: bool = False,
) -> Attempt:
    return Attempt(
        job_id=job_id,
        n=n,
        route=route,
        pool="pool",
        started_at=(finished_at or T0) - timedelta(seconds=duration_s or 0),
        finished_at=finished_at,
        outcome=outcome,
        duration_s=duration_s,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        cost_estimated=cost_estimated,
    )


def rate(jobstore, job_id: str, value: str, at: datetime = T0) -> None:
    jobstore.add_rating(
        Rating(
            job_id=job_id,
            value=value,  # type: ignore[arg-type]
            should_tier=None,
            by="test",
            note_sha256=None,
            note_chars=None,
            at=at,
        )
    )


def test_route_samples_empty_history(jobstore) -> None:
    assert jobstore.route_samples_since(T0) == ([], [])


def test_route_samples_job_totals_and_latest_rating(jobstore) -> None:
    jobstore.create(make_job("j_1"))
    jobstore.add_attempt(make_attempt("j_1", 1, duration_s=2.0, tokens_in=3, tokens_out=4))
    jobstore.add_attempt(
        make_attempt(
            "j_1",
            2,
            duration_s=3.0,
            tokens_in=None,
            tokens_out=None,
            cost_usd=0.5,
            cost_estimated=True,
        )
    )
    rate(jobstore, "j_1", "bad")
    rate(jobstore, "j_1", "good", at=T0 + timedelta(minutes=1))
    jobstore.create(make_job("j_2", status="failed", route=None))

    jobs, attempts = jobstore.route_samples_since(T0)

    assert jobs == [
        JobSample(
            route="a/main",
            tier="standard",
            mode="read",
            status="completed",
            rating="good",
            duration_s=5.0,
            tokens=7,
            cost_usd=0.5,
            cost_estimated=True,
        ),
        JobSample(
            route=None,
            tier="standard",
            mode="read",
            status="failed",
            rating=None,
            duration_s=None,
            tokens=None,
            cost_usd=None,
            cost_estimated=False,
        ),
    ]
    assert attempts == [
        AttemptSample(route="a/main", tier="standard", mode="read", outcome="ok"),
        AttemptSample(route="a/main", tier="standard", mode="read", outcome="ok"),
    ]


def test_route_samples_unrated_job_without_figures(jobstore) -> None:
    jobstore.create(make_job("j_1"))
    jobstore.add_attempt(make_attempt("j_1", 1, duration_s=None, tokens_in=None, tokens_out=None))

    jobs, _ = jobstore.route_samples_since(T0)

    (sample,) = jobs
    assert sample.rating is None
    assert sample.duration_s is None
    assert sample.tokens is None
    assert sample.cost_usd is None
    assert sample.cost_estimated is False


def test_route_samples_respect_the_since_moment(jobstore) -> None:
    old = T0 - timedelta(days=1)
    jobstore.create(make_job("j_old", finished_at=old))
    jobstore.add_attempt(make_attempt("j_old", 1, outcome="limit", finished_at=old))
    jobstore.create(make_job("j_new", mode="write"))
    jobstore.add_attempt(make_attempt("j_new", 1, outcome="timeout"))
    jobstore.create(make_job("j_queued", status="queued", finished_at=None))
    jobstore.add_attempt(make_attempt("j_queued", 1, outcome="error", finished_at=None))

    jobs, attempts = jobstore.route_samples_since(T0)

    assert [sample.route for sample in jobs] == ["a/main"]
    assert jobs[0].mode == "write"
    assert attempts == [
        AttemptSample(route="a/main", tier="standard", mode="write", outcome="timeout")
    ]


def test_route_samples_keep_route_tier_and_mode_of_each_job(jobstore) -> None:
    jobstore.create(make_job("j_1", route="a/main", tier="cheap"))
    jobstore.create(make_job("j_2", route="b/main", tier="strong", mode="write"))
    jobstore.add_attempt(make_attempt("j_1", 1, route="a/main", outcome="limit"))
    jobstore.add_attempt(make_attempt("j_2", 1, route="b/main", outcome="error"))

    jobs, attempts = jobstore.route_samples_since(T0)

    assert {(sample.route, sample.tier, sample.mode) for sample in jobs} == {
        ("a/main", "cheap", "read"),
        ("b/main", "strong", "write"),
    }
    assert attempts == [
        AttemptSample(route="a/main", tier="cheap", mode="read", outcome="limit"),
        AttemptSample(route="b/main", tier="strong", mode="write", outcome="error"),
    ]
