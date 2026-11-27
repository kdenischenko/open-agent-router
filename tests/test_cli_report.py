"""CLI report routes: scorecard text and JSON over a seeded sqlite store."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_router import cli
from agent_router.jobstore import SqliteJobStore
from agent_router.store import SqliteStore
from helpers import full_config_text, write_config
from test_jobstore_route_samples import make_attempt, make_job, rate

T0 = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)


@pytest.fixture
def report_home(home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    monkeypatch.delenv("AGENT_ROUTER_URL", raising=False)
    write_config(home, full_config_text())
    return home


def seed(
    home: Path,
    job_id: str,
    *,
    route: str | None = "claude-cli/sonnet",
    rating: str | None = "good",
    cost_usd: float | None = None,
    cost_estimated: bool = False,
    finished_at: datetime = T0,
) -> None:
    store = SqliteStore(home / "agent-router.sqlite")
    try:
        jobs = SqliteJobStore(store)
        jobs.create(make_job(job_id, route=route, finished_at=finished_at))
        jobs.add_attempt(
            make_attempt(
                job_id,
                1,
                route=route or "claude-cli/sonnet",
                finished_at=finished_at,
                cost_usd=cost_usd,
                cost_estimated=cost_estimated,
            )
        )
        if rating is not None:
            rate(jobs, job_id, rating, at=finished_at)
    finally:
        store.close()


def test_report_routes_empty_history(report_home: Path) -> None:
    result = CliRunner().invoke(cli.app, ["report", "routes"])
    assert result.exit_code == 0, result.output
    assert "no finished jobs" in result.output

    as_json = CliRunner().invoke(cli.app, ["report", "routes", "--json"])
    assert as_json.exit_code == 0, as_json.output
    assert json.loads(as_json.output) == {"min_jobs": 5, "rows": []}


def test_report_routes_json_row_for_rated_job(report_home: Path) -> None:
    seed(report_home, "j_1")
    result = CliRunner().invoke(cli.app, ["report", "routes", "--json"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["min_jobs"] == 5
    [row] = body["rows"]
    assert row["route"] == "claude-cli/sonnet"
    assert row["tier"] == "standard" and row["mode"] == "read"
    assert row["in_config"] is True and row["quality"] == 5
    assert row["jobs"] == 1
    assert row["outcomes"]["completed"] == 1
    assert row["ratings"]["good"] == 1
    assert row["rated"] == 1 and row["rated_share"] == 1.0
    assert row["good_share"] == 1.0
    assert row["ranked"] is False


def test_report_routes_text_marks_unranked_row_and_shares(report_home: Path) -> None:
    seed(report_home, "j_1")
    result = CliRunner().invoke(cli.app, ["report", "routes"])
    assert result.exit_code == 0, result.output
    assert "claude-cli/sonnet standard read" in result.output
    assert "jobs 1" in result.output
    assert "completed 1" in result.output
    assert "attempts limit 0/error 0/timeout 0" in result.output
    assert "duration median/p90 5.0s/5.0s" in result.output
    assert "tokens median 30" in result.output
    assert "rated 1/1 (100%)" in result.output
    assert "good 1/1 (100%)" in result.output
    assert "(fewer than 5 jobs)" in result.output
    assert "(not in config)" not in result.output


def test_report_routes_since_after_the_job_filters_it_out(report_home: Path) -> None:
    seed(report_home, "j_1")
    result = CliRunner().invoke(cli.app, ["report", "routes", "--since", "2026-11-14"])
    assert result.exit_code == 0, result.output
    assert "no finished jobs" in result.output

    as_json = CliRunner().invoke(cli.app, ["report", "routes", "--since", "2026-11-14", "--json"])
    assert as_json.exit_code == 0, as_json.output
    assert json.loads(as_json.output)["rows"] == []


def test_report_routes_marks_estimated_cost(report_home: Path) -> None:
    seed(report_home, "j_1", cost_usd=0.2, cost_estimated=True)
    result = CliRunner().invoke(cli.app, ["report", "routes"])
    assert result.exit_code == 0, result.output
    assert "cost ~$0.20" in result.output


def test_report_routes_marks_costless_route_without_marker(report_home: Path) -> None:
    seed(report_home, "j_1")
    result = CliRunner().invoke(cli.app, ["report", "routes"])
    assert result.exit_code == 0, result.output
    assert "cost -," in result.output
    assert "$" not in result.output.split("cost", 1)[1].split(",")[0]


def test_report_routes_marks_route_not_in_config(report_home: Path) -> None:
    seed(report_home, "j_1", route="ghost/main")
    result = CliRunner().invoke(cli.app, ["report", "routes"])
    assert result.exit_code == 0, result.output
    assert "ghost/main standard read" in result.output
    assert "(not in config)" in result.output

    as_json = CliRunner().invoke(cli.app, ["report", "routes", "--json"])
    assert as_json.exit_code == 0, as_json.output
    [row] = json.loads(as_json.output)["rows"]
    assert row["in_config"] is False
    assert row["quality"] is None
