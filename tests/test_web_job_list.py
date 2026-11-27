"""Job list page: filters, search, sorting, paging and the refresh fragment."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from agent_router import web as web_module
from test_web_dashboard import login
from test_web_jobs import WebRig


@pytest.fixture
def rig(home, config, store, jobstore, clock, tokens) -> WebRig:
    return WebRig(home, config, store, jobstore, clock, tokens)


@pytest.fixture
def web(rig) -> TestClient:
    with TestClient(rig.app, base_url="http://127.0.0.1:8787") as client:
        assert login(client, rig.tokens.admin).status_code == 303
        yield client


def listed(text: str) -> list[str]:
    """Job ids in list order."""
    return re.findall(r'<li class="job-row"[^>]*>\s*<a href="/jobs/([^"]+)"', text)


def finish(rig: WebRig, job, seconds: int, status: str = "completed"):
    from dataclasses import replace

    done = replace(job, status=status, finished_at=job.created_at + timedelta(seconds=seconds))
    rig.jobs.update(done)
    return done


def test_status_filter(web, rig) -> None:
    queued = rig.add_job("queued")
    failed = finish(rig, rig.add_job("running"), 5, "failed")

    assert listed(web.get("/jobs?status=failed").text) == [failed.id]
    assert set(listed(web.get("/jobs").text)) == {queued.id, failed.id}


def test_route_filter_matches_chosen_and_awaiting_route(web, rig, config) -> None:
    other = next(route.id for route in config.routes if route.id != "kimi-cli/kimi-for-coding")
    mine = rig.add_job("queued", route=other)
    rig.add_job("queued")

    assert listed(web.get("/jobs", params={"route": other}).text) == [mine.id]


def test_caller_filter(web, rig) -> None:
    mine = rig.add_job("queued", caller="claude-code")
    rig.add_job("queued", caller="opencode")

    assert listed(web.get("/jobs?caller=claude-code").text) == [mine.id]


def test_search_by_id_and_prompt_text(web, rig) -> None:
    first = rig.add_job("queued", prompt="Refactor the Parser module")
    second = rig.add_job("queued", prompt="write docs")

    assert listed(web.get("/jobs?q=parser").text) == [first.id]
    assert listed(web.get("/jobs", params={"q": second.id}).text) == [second.id]


def test_search_treats_like_wildcards_literally(web, rig) -> None:
    rig.add_job("queued", prompt="plain prompt")

    text = web.get("/jobs?q=%25").text

    assert listed(text) == []
    assert "No jobs match these filters" in text


@pytest.mark.parametrize(
    ("sort", "order"),
    [("newest", [2, 1, 0]), ("oldest", [0, 1, 2]), ("duration", [1, 0, 2])],
)
def test_sorting(web, rig, sort, order) -> None:
    jobs = [
        finish(rig, rig.add_job("running"), 30),
        finish(rig, rig.add_job("running"), 90),
        rig.add_job("queued"),
    ]

    assert listed(web.get(f"/jobs?sort={sort}").text) == [jobs[i].id for i in order]


def test_paging_keeps_filters_in_links(web, rig, monkeypatch) -> None:
    monkeypatch.setattr(web_module, "JOBS_PAGE_SIZE", 2)
    jobs = [rig.add_job("queued", caller="claude-code") for _ in range(5)]

    first = web.get("/jobs?caller=claude-code").text
    second = web.get("/jobs?caller=claude-code&page=2").text
    last = web.get("/jobs?caller=claude-code&page=99").text

    assert listed(first) == [jobs[4].id, jobs[3].id]
    assert 'href="/jobs?caller=claude-code&amp;page=2" rel="next"' in first
    assert "Page 1 of 3" in first
    assert listed(second) == [jobs[2].id, jobs[1].id]
    assert 'href="/jobs?caller=claude-code" rel="prev"' in second
    assert listed(last) == [jobs[0].id]
    assert "Page 3 of 3" in last
    assert "Jobs 5–5 of 5" in last


@pytest.mark.parametrize(
    "query",
    [
        "status=nope",
        "route=missing/route",
        "sort=sideways",
        "page=abc",
        "page=-1",
        "page=" + "9" * 5000,
        "q=" + "x" * 201,
    ],
)
def test_unknown_values_are_ignored(web, rig, query) -> None:
    job = rig.add_job("queued")

    response = web.get("/jobs?" + query)

    assert response.status_code == 200
    assert listed(response.text) == [job.id]


def test_empty_filtered_result_links_to_clear_filters(web, rig) -> None:
    rig.add_job("queued")

    text = web.get("/jobs?status=failed").text

    assert re.search(
        r'<p class="empty">No jobs match these filters\. <a href="/jobs">Clear filters</a>', text
    )


def test_filter_form_keeps_current_values(web, rig) -> None:
    rig.add_job("queued")

    text = web.get("/jobs?status=failed&q=a%22b&sort=oldest").text

    assert '<form method="get" action="/jobs"' in text
    assert '<option value="failed" selected>failed</option>' in text
    assert '<option value="oldest" selected>oldest</option>' in text
    assert 'value="a&#34;b"' in text or 'value="a&quot;b"' in text


def test_fragment_honours_the_query_string(web, rig) -> None:
    queued = rig.add_job("queued")
    finish(rig, rig.add_job("running"), 5, "failed")

    text = web.get("/fragments/jobs?status=queued").text

    assert 'id="jobs"' in text
    assert listed(text) == [queued.id]
    assert '<form method="get"' not in text


def test_rows_show_status_badge_and_bare_columns(web, rig) -> None:
    rig.add_job("queued", caller="claude-code")
    finish(rig, rig.add_job("running"), 5, "failed")

    text = web.get("/jobs").text

    assert '<span class="status-badge status-badge--danger">failed</span>' in text
    assert '<span class="status-badge status-badge--info">queued</span>' in text
    assert "<span>claude-code</span>" in text
    assert "<span>standard</span>" in text
    assert "<span>5 s</span>" in text
    for label in ("Route:", "Caller:", "Created:", "Took:"):
        assert label not in text, f"the dense row still labels its {label}"


def test_row_columns_read_id_status_route_tier_caller_age_duration(web, rig) -> None:
    job = finish(rig, rig.add_job("running", caller="claude-code"), 5, "failed")

    text = web.get("/jobs").text
    start = text.index(f'<a href="/jobs/{job.id}">')
    row = text[start : text.index("</li>", start)]

    columns = (
        f"<code>{job.id}</code>",
        '<span class="status-badge status-badge--danger">failed</span>',
        "<span>kimi-cli/kimi-for-coding</span>",
        "<span>standard</span>",
        "<span>claude-code</span>",
        "<time datetime=",
        "<span>5 s</span>",
    )
    positions = [row.index(column) for column in columns]
    assert positions == sorted(positions)
    # the reason stays a muted second line under the row, not another column
    assert '<span class="job-row__reason">Reason: route choice reason</span>' in row


def test_rows_keep_the_hotkey_cursor_hook(web, rig) -> None:
    job = rig.add_job("queued")

    text = web.get("/jobs").text

    # app.js moves its cursor over .job-row and reads the job id from the code inside,
    # so the row must open with the id link and keep data-status for the smoke tests
    assert re.search(
        r'<li class="job-row" data-status="queued">\s*'
        r'<a href="/jobs/' + job.id + r'"><code>' + job.id + r"</code></a>",
        text,
    )


def test_every_status_has_a_badge_kind() -> None:
    from agent_router.jobs import STATUSES

    assert set(web_module.STATUS_KIND) == set(STATUSES)
