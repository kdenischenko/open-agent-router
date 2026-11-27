"""Stats page: range selector, summary cards and per-route charts over stored jobs."""

from __future__ import annotations

import re
from datetime import timedelta
from itertools import count

import pytest
from fastapi.testclient import TestClient

from agent_router.jobs import Attempt, Job, JobRequest, Rating
from agent_router.spend import SpendEntry
from helpers import BASE_URL
from test_web_dashboard import authenticated_client, login
from test_web_jobs import WebRig

HEX_COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b")
KIMI = "kimi-cli/kimi-for-coding"
CLAUDE = "claude-cli/sonnet"
NUMBERS = count(1)


@pytest.fixture
def rig(home, config, store, jobstore, clock, tokens) -> WebRig:
    return WebRig(home, config, store, jobstore, clock, tokens)


@pytest.fixture
def web(rig) -> TestClient:
    with TestClient(rig.app, base_url=BASE_URL) as client:
        assert login(client, rig.tokens.admin).status_code == 303
        yield client


def seed_job(
    rig: WebRig,
    *,
    status: str = "completed",
    route: str = KIMI,
    created_at=None,
    finished_at=None,
    rating: tuple[str, str | None] | None = None,
) -> Job:
    """A job straight into the store, so creation times can sit in any range."""
    now = rig.clock.now()
    job = Job(
        id=f"j_stats_{next(NUMBERS)}",
        request=JobRequest(prompt="stats seed", cwd=rig.home),
        status=status,
        tier="standard",
        tier_source="default",
        repo_root=None,
        route=route,
        created_at=now if created_at is None else created_at,
        finished_at=finished_at,
    )
    rig.jobs.create(job)
    if rating is not None:
        value, should_tier = rating
        rig.jobs.add_rating(
            Rating(
                job_id=job.id,
                value=value,
                should_tier=should_tier,
                by="test",
                note_sha256=None,
                note_chars=None,
                at=now,
            )
        )
    return job


def seed_attempt(
    rig: WebRig,
    job: Job,
    *,
    route: str = KIMI,
    tokens_in: int | None = 1000,
    tokens_out: int | None = 200,
    duration_s: float | None = 30.0,
    cost_usd: float | None = 0.01,
) -> None:
    now = rig.clock.now()
    rig.jobs.add_attempt(
        Attempt(
            job_id=job.id,
            n=1,
            route=route,
            pool="kimi-code",
            started_at=now - timedelta(seconds=duration_s or 0),
            finished_at=now,
            outcome="ok",
            duration_s=duration_s,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
        )
    )


def range_nav(text: str) -> str:
    found = re.search(r'<nav class="range-nav".*?</nav>', text, re.S)
    assert found is not None
    return found.group(0)


# --- empty store -----------------------------------------------------------------------------


def test_stats_without_data_offers_all_ranges_and_shows_empty_states(web) -> None:
    text = web.get("/stats").text

    assert '<div id="stats" data-range="24h">' in text
    for key, label in (("24h", "24 hours"), ("7d", "7 days"), ("30d", "30 days")):
        assert re.search(
            rf'<a href="/stats\?range={key}" class="range-link"(?: aria-current="true")?>'
            rf"{label}</a>",
            text,
        )
    for message in (
        "No tokens were used in this range.",
        "No finished jobs in this range.",
        "No completed, partial or failed jobs in this range.",
        "No ratings in this range.",
    ):
        assert message in text
    assert "no ratings yet" in text


@pytest.mark.parametrize("range_key", ["7d", "30d"])
def test_stats_range_is_carried_by_the_page_and_the_selector(web, range_key) -> None:
    text = web.get(f"/stats?range={range_key}").text

    assert f'<div id="stats" data-range="{range_key}">' in text
    nav = range_nav(text)
    assert nav.count('aria-current="true"') == 1
    assert re.search(
        rf'<a href="/stats\?range={range_key}" class="range-link" aria-current="true">', nav
    )


def test_unknown_range_falls_back_to_24_hours(web) -> None:
    text = web.get("/stats?range=forever").text

    assert '<div id="stats" data-range="24h">' in text
    assert range_nav(text).count('aria-current="true"') == 1


def test_stats_fragment_is_a_bare_fragment(web, rig) -> None:
    seed_job(rig)

    response = web.get("/fragments/stats?range=7d")

    assert response.status_code == 200
    assert '<div id="stats" data-range="7d">' in response.text
    assert KIMI in response.text
    assert "<html" not in response.text.lower()


# --- summary cards and legends ---------------------------------------------------------------


def test_stats_show_tokens_duration_and_outcomes_per_route(web, rig) -> None:
    now = rig.clock.now()
    job = seed_job(
        rig, created_at=now - timedelta(minutes=5), finished_at=now - timedelta(minutes=2)
    )
    seed_attempt(rig, job, tokens_in=45000, tokens_out=5000, duration_s=120.0, cost_usd=1.5)

    text = web.get("/stats").text

    assert "45k in / 5k out" in text
    assert "$1.50" in text
    assert "avg 3 min · p50 3 min · p95 3 min · 1 job" in text
    assert "1 completed · 0 partial · 0 failed" in text
    assert "hourly buckets · in + out tokens" in text
    legend = re.search(r"<code>kimi-cli/kimi-for-coding</code> — ([^<]+)</li>", text)
    assert legend is not None
    assert legend.group(1) == "50k tokens"


def test_tokens_chart_orders_routes_by_total(web, rig) -> None:
    big = seed_job(rig)
    seed_attempt(rig, big, tokens_in=50000, tokens_out=0)
    small = seed_job(rig, route=CLAUDE)
    seed_attempt(rig, small, route=CLAUDE, tokens_in=2000, tokens_out=0)

    text = web.get("/stats").text

    top = re.search(
        r'<li><span class="chart-swatch chart-color--1" aria-hidden="true"></span>'
        r"<code>([^<]+)</code> — ([^<]+)</li>",
        text,
    )
    assert top is not None
    assert top.group(1) == KIMI
    assert top.group(2) == "50k tokens"


def test_stats_show_ratings_and_should_tier_corrections(web, rig) -> None:
    now = rig.clock.now()
    seed_job(rig, created_at=now - timedelta(hours=1), finished_at=now, rating=("good", None))
    seed_job(
        rig,
        status="failed",
        route=CLAUDE,
        created_at=now - timedelta(hours=1),
        finished_at=now,
        rating=("bad", "strong"),
    )

    text = web.get("/stats").text

    assert "1 good · 0 partial · 0 bad" in text
    assert "0 good · 0 partial · 1 bad · 1 should-tier correction" in text
    assert "no ratings" not in text
    card = text[text.index('<p class="stat-card__label">ratings</p>') :][:400]
    assert '<p class="stat-card__value">2</p>' in card
    assert "1 should-tier correction" in card


def test_stats_count_proxy_requests_from_the_spend_ledger(web, rig) -> None:
    rig.jobs.add_spend(
        SpendEntry(
            pool="kimi-code",
            at=rig.clock.now(),
            source="proxy",
            route=KIMI,
            tokens_in=9000,
            tokens_out=1000,
            cost_usd=0.25,
            estimated=False,
            prices_version=None,
            ref_id="req_stats_1",
            ref_n=1,
        )
    )

    text = web.get("/stats").text

    assert "10k from 1 proxy request" in text
    assert "includes $0.2500 of proxy requests" in text
    assert "50k tokens" not in text  # the proxy tokens are not counted twice


# --- the time range actually filters ---------------------------------------------------------


def test_the_range_controls_which_jobs_are_counted(web, rig) -> None:
    now = rig.clock.now()
    seed_job(rig, created_at=now - timedelta(hours=2), finished_at=now)
    seed_job(
        rig,
        status="failed",
        created_at=now - timedelta(days=3),
        finished_at=now - timedelta(days=3),
    )

    day = web.get("/stats").text
    week = web.get("/stats?range=7d").text

    assert "1 completed · 0 partial · 0 failed" in day
    assert "· 2 jobs" not in day
    assert "1 completed · 0 partial · 1 failed" in week
    assert "· 2 jobs" in week


# --- charts stay theme-token coloured --------------------------------------------------------


def test_stats_charts_are_inline_svg_coloured_by_css_classes(web, rig) -> None:
    now = rig.clock.now()
    job = seed_job(rig, created_at=now - timedelta(minutes=5), finished_at=now)
    seed_attempt(rig, job, tokens_in=1000, tokens_out=200)

    text = web.get("/stats").text

    assert '<svg class="chart"' in text
    assert '<polyline class="chart-series chart-color--1"' in text
    assert '<rect class="chart-bar chart-bar--ok"' in text
    assert '<line class="chart-tick chart-tick--p95"' in text
    # colour values live only in the app.css token blocks, never in the markup
    assert "style=" not in text
    assert HEX_COLOUR.search(text) is None


# --- no dispatcher ----------------------------------------------------------------------------


def test_stats_without_dispatcher_shows_the_disabled_notice(client, tokens) -> None:
    authenticated_client(client, tokens)

    for path in ("/stats", "/fragments/stats"):
        text = client.get(path).text
        assert "Jobs are disabled: the job dispatcher is not running." in text
        assert 'id="stats"' in text
